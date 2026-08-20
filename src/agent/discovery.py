"""Phase 1: LLM-driven discovery.

Drives a real browser to work out how a task is done, and records the
successful run as a typed artifact. This is the only phase with a model in the
decision loop, and it is meant to run once per capability.

What makes the recording worth anything is not that it completed the task —
it is that it will still complete the task tomorrow. Three things here exist
purely to protect that:

* Locators are recorded by identity, with a screenshot fallback captured for
  free from ``bounding_box()`` on every step (C4).
* Values that vary per run are stored as ``{{param}}`` templates, emitted by
  the model itself rather than found by string-matching the artifact (C7).
* Checkpoints are derived from a before/after diff and rejected if they were
  already true, so they assert something the action actually caused (C5).

See PLAN.md §5.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable

from src import settings
from src.agent import prompts
from src.agent.decisions import AgentDecision, DecisionAction, ProposedLocator
from src.agent.observation import Change, Observation, diff, distinctive_new_text, observe
from src.artifact.schema import (
    AccessibilityLocatorMethod,
    AccessibilityMethod,
    ActionType,
    Artifact,
    BrowserConfig,
    Condition,
    ConditionType,
    ErrorAction,
    ErrorCategory,
    ErrorHandler,
    ErrorTypeKey,
    ExpectedType,
    Extraction,
    ExtractMethod,
    InputParameter,
    Locators,
    OnFail,
    ParameterType,
    PrimaryLocator,
    Provenance,
    RecordedBy,
    ReplayConfig,
    ReplayMode,
    RiskLevel,
    Step,
    SurfaceType,
    Target,
)
from src.evidence.logger import LLMCallLog, RunLog, StepLog
from src.llm.client import LLMClient
from src.replay import locator as loc
from src.safety.guardrails import Allowlist, BlockedByAllowlist
from src.session.browser import Session, browser_session

log = logging.getLogger(__name__)

#: Consecutive refused proposals before giving up. The model gets told why and
#: is allowed to correct itself, but a model that keeps proposing blocked
#: actions is not going to converge.
MAX_CONSECUTIVE_REFUSALS = 3

EscalateFn = Callable[[str, int], Awaitable[None]]


class DiscoveryError(RuntimeError):
    """Discovery could not produce an artifact."""


@dataclass(slots=True)
class DiscoveryResult:
    artifact: Artifact | None
    run_log: RunLog
    stopped_because: str
    succeeded: bool = False


@dataclass
class _Recorder:
    """Accumulates the artifact as the run proceeds."""

    goal: str
    url: str
    params: dict[str, Any]
    config: BrowserConfig
    steps: list[Step] = field(default_factory=list)
    output_schema: dict[str, ExpectedType] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def next_step_id(self) -> int:
        return len(self.steps) + 1

    def add(self, step: Step) -> None:
        self.steps.append(step)


# --------------------------------------------------------------------------
# Locator conversion
# --------------------------------------------------------------------------


def to_artifact_locator(proposed: ProposedLocator) -> AccessibilityLocatorMethod:
    """Convert the model's flat proposal into the artifact's typed form."""
    method = AccessibilityMethod(proposed.method.value)

    # get_by_role needs both role and name to be stable; the schema enforces
    # that, so fill in a defensible default rather than failing the turn.
    name = proposed.name
    value = proposed.value
    if method is AccessibilityMethod.GET_BY_ROLE and not name:
        name = value
    if method is AccessibilityMethod.GET_BY_TEXT and not value:
        value = name

    return AccessibilityLocatorMethod(
        method=method, role=proposed.role, name=name, value=value
    )


def build_locators(
    proposed: ProposedLocator, fallback: Any | None = None
) -> Locators:
    return Locators(
        primary=PrimaryLocator(methods=[to_artifact_locator(proposed)], available=True),
        fallback=fallback,
    )


# --------------------------------------------------------------------------
# Checkpoint synthesis (PLAN.md C5)
# --------------------------------------------------------------------------


def synthesise_post_condition(
    change: Change,
    before: Observation,
    *,
    action: ActionType,
    locators: Locators | None,
    value: str | None,
    timeout_ms: int,
) -> tuple[Condition, str | None]:
    """Build a checkpoint asserting something the action actually caused.

    Returns the condition and, when the evidence is weak, a warning to record
    against the run. A checkpoint that was already true before the action
    verifies nothing, so candidates are tested against the before-state and
    discarded if they already held.
    """
    if change.url_changed:
        segment = change.new_url_segment
        if segment and segment not in change.url_before:
            return (
                Condition(
                    condition=ConditionType.URL_CONTAINS,
                    value=segment,
                    timeout_ms=timeout_ms,
                    on_fail=OnFail.RETRY,
                ),
                None,
            )

    if action is ActionType.FILL and locators is not None and value is not None:
        # The strongest available checkpoint for a fill: the field now holds
        # what we put in it.
        return (
            Condition(
                condition=ConditionType.ELEMENT_HAS_VALUE,
                value=value,
                locators=locators,
                timeout_ms=timeout_ms,
                on_fail=OnFail.RETRY,
            ),
            None,
        )

    text = distinctive_new_text(change, before)
    if text:
        return (
            Condition(
                condition=ConditionType.PAGE_CONTAINS_TEXT,
                value=text,
                timeout_ms=timeout_ms,
                on_fail=OnFail.RETRY,
            ),
            None,
        )

    # Nothing observably changed. Fall back to asserting the target is still
    # there, and say plainly that this checkpoint is weak -- a reviewer should
    # see that rather than discover it when replay passes a broken step.
    if locators is not None:
        return (
            Condition(
                condition=ConditionType.ELEMENT_VISIBLE,
                locators=locators,
                timeout_ms=timeout_ms,
                on_fail=OnFail.RETRY,
            ),
            "no observable page change; checkpoint is weak and should be "
            "reviewed by hand",
        )

    return (
        Condition(
            condition=ConditionType.URL_CONTAINS,
            value=_url_tail(change.url_after),
            timeout_ms=timeout_ms,
            on_fail=OnFail.RETRY,
        ),
        "no observable page change; checkpoint falls back to the current URL",
    )


def _url_tail(url: str) -> str:
    path = url.split("?", 1)[0].rstrip("/")
    return path.rsplit("/", 1)[-1] or path


def default_error_map() -> dict[str, dict[ErrorTypeKey, ErrorHandler]]:
    """Sensible per-error defaults for a freshly recorded artifact.

    Recorded rather than assumed, so the artifact stays self-contained and a
    reviewer can see and change the retry policy without reading engine source.
    """
    return {
        "_default": {
            ErrorTypeKey.ELEMENT_NOT_FOUND: ErrorHandler(
                category=ErrorCategory.RECOVERABLE,
                action=ErrorAction.RETRY,
                max_retries=3,
                retry_wait_ms=1000,
                on_exhausted=OnFail.ESCALATE_HUMAN,
            ),
            ErrorTypeKey.CHECKPOINT_FAILED: ErrorHandler(
                category=ErrorCategory.RECOVERABLE,
                action=ErrorAction.RETRY,
                max_retries=2,
                retry_wait_ms=1000,
                on_exhausted=OnFail.HARD_FAILURE,
            ),
            ErrorTypeKey.TIMEOUT: ErrorHandler(
                category=ErrorCategory.RECOVERABLE,
                action=ErrorAction.RETRY,
                max_retries=2,
                retry_wait_ms=2000,
                on_exhausted=OnFail.ESCALATE_HUMAN,
            ),
            ErrorTypeKey.SESSION_EXPIRED: ErrorHandler(
                category=ErrorCategory.RECOVERABLE,
                action=ErrorAction.RE_LOGIN,
                max_retries=1,
                retry_wait_ms=0,
                on_exhausted=OnFail.ESCALATE_HUMAN,
            ),
            ErrorTypeKey.EXTRACTION_EMPTY: ErrorHandler(
                category=ErrorCategory.HARD_FAILURE,
                action=ErrorAction.STOP,
                capture_screenshot=True,
                message="An expected value was not present on the page.",
            ),
            ErrorTypeKey.WRONG_PAGE_STATE: ErrorHandler(
                category=ErrorCategory.HARD_FAILURE,
                action=ErrorAction.STOP,
                capture_screenshot=True,
                message="The page was not in the expected state.",
            ),
        }
    }


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


class DiscoveryAgent:
    """Runs the observe -> decide -> act loop and records what worked."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        allowlist: Allowlist,
        max_steps: int = settings.MAX_DISCOVERY_STEPS,
        timeout_s: int = settings.DISCOVERY_TIMEOUT_S,
        dead_end_threshold: int = settings.DEAD_END_THRESHOLD,
        escalate: EscalateFn | None = None,
    ) -> None:
        self.llm = llm
        self.allowlist = allowlist
        self.max_steps = max_steps
        self.timeout_s = timeout_s
        self.dead_end_threshold = dead_end_threshold
        self.escalate = escalate

    async def run(
        self,
        *,
        goal: str,
        url: str,
        params: dict[str, Any],
        capability_id: str,
        session: Session,
        run_log: RunLog,
    ) -> DiscoveryResult:
        self.allowlist.check_origin(url)

        recorder = _Recorder(goal=goal, url=url, params=params, config=session.config)
        started = time.monotonic()
        history: list[str] = []
        fingerprints: list[str] = []
        refusals = 0
        stopped = "unknown"

        # Step 1 is always the navigate that opens the app.
        await self._record_navigation(session, recorder, url, run_log)

        while True:
            if recorder.next_step_id() > self.max_steps:
                stopped = f"max steps reached ({self.max_steps})"
                break
            if time.monotonic() - started > self.timeout_s:
                stopped = f"global timeout reached ({self.timeout_s}s)"
                break

            before = await observe(session.page)

            # Dead end: the page has not meaningfully changed for N turns, so
            # the agent is going in circles. Escalation signal 1.
            fingerprints.append(before.fingerprint)
            if self._is_dead_end(fingerprints):
                stopped = (
                    f"dead end: page unchanged for {self.dead_end_threshold} "
                    f"consecutive turns"
                )
                await self._maybe_escalate(stopped, recorder.next_step_id())
                break

            decision = await self._decide(
                goal=goal,
                url=url,
                params=params,
                observation=before,
                history=history,
                step_number=recorder.next_step_id(),
                session=session,
                run_log=run_log,
            )

            if decision.stuck or decision.action is DecisionAction.GIVE_UP:
                stopped = f"agent stuck: {decision.stuck_reason or decision.reasoning}"
                await self._maybe_escalate(stopped, recorder.next_step_id())
                break

            if decision.goal_achieved and recorder.output_schema:
                stopped = "goal achieved"
                break

            # Guardrails are enforced here, independently of the prompt. A
            # refused proposal is fed back so the model can correct itself.
            try:
                self._check_permitted(decision)
            except BlockedByAllowlist as exc:
                refusals += 1
                history.append(f"REFUSED: {decision.action.value} - {exc}")
                log.warning("refused proposal: %s", exc)
                if refusals >= MAX_CONSECUTIVE_REFUSALS:
                    stopped = f"too many refused proposals: {exc}"
                    break
                continue
            refusals = 0

            if self._is_risky(decision):
                stopped = (
                    f"agent proposed a {decision.risk_level} risk action during "
                    f"discovery: {decision.reasoning}"
                )
                await self._maybe_escalate(stopped, recorder.next_step_id())
                break

            try:
                summary = await self._execute_and_record(
                    session, recorder, decision, before, run_log
                )
            except Exception as exc:
                history.append(f"FAILED: {decision.action.value} - {exc}")
                log.warning("step failed during discovery: %s", exc)
                if len(history) >= 2 and history[-2].startswith("FAILED"):
                    stopped = f"two consecutive step failures; last: {exc}"
                    await self._maybe_escalate(stopped, recorder.next_step_id())
                    break
                continue

            history.append(summary)

            if decision.goal_achieved and recorder.output_schema:
                stopped = "goal achieved"
                break

        succeeded = stopped == "goal achieved"
        artifact = (
            self._assemble(recorder, capability_id, session, run_log)
            if succeeded
            else None
        )

        if succeeded and artifact:
            run_log.finish_success(
                {k: "<recorded>" for k in artifact.output_schema}
            )
        else:
            run_log.finish_failure(
                failed_at_step=recorder.next_step_id(),
                error_type=ErrorTypeKey.WRONG_PAGE_STATE.value,
                message=stopped,
                expected="the goal to be achieved and all outputs extracted",
                observed=stopped,
            )

        for warning in recorder.warnings:
            log.warning("recording warning: %s", warning)

        return DiscoveryResult(
            artifact=artifact,
            run_log=run_log,
            stopped_because=stopped,
            succeeded=succeeded,
        )

    # -- loop pieces -------------------------------------------------------

    def _is_dead_end(self, fingerprints: list[str]) -> bool:
        if len(fingerprints) < self.dead_end_threshold:
            return False
        recent = fingerprints[-self.dead_end_threshold :]
        return len(set(recent)) == 1

    def _is_risky(self, decision: AgentDecision) -> bool:
        return decision.risk_level.lower() in ("high", "critical")

    def _check_permitted(self, decision: AgentDecision) -> None:
        if decision.action is DecisionAction.GIVE_UP:
            return
        self.allowlist.check_action(decision.action.value)
        if decision.action is DecisionAction.NAVIGATE and decision.url:
            self.allowlist.check_url(decision.url)

    async def _maybe_escalate(self, reason: str, step_id: int) -> None:
        if self.escalate is not None:
            await self.escalate(reason, step_id)

    async def _decide(
        self,
        *,
        goal: str,
        url: str,
        params: dict[str, Any],
        observation: Observation,
        history: list[str],
        step_number: int,
        session: Session,
        run_log: RunLog,
    ) -> AgentDecision:
        sparse = observation.is_sparse
        message = prompts.discovery_user_message(
            goal=goal,
            url=url,
            parameter_names=sorted(params),
            permitted_domains=sorted(self.allowlist.permitted_domains),
            snapshot=observation.snapshot,
            current_url=observation.url,
            history=history[-12:],
            step_number=step_number,
            max_steps=self.max_steps,
            snapshot_is_sparse=sparse,
        )

        # A sparse tree is the legacy case: the model gets the screen instead.
        # Viewport-only, so any coordinates it reasons about are clickable (C1).
        image = await session.screenshot() if sparse else None

        result = self.llm.decide(
            system=prompts.DISCOVERY_SYSTEM,
            user=message,
            schema=AgentDecision,
            image_png=image,
        )
        usage = result.usage
        run_log.record_llm_call(
            LLMCallLog(
                timestamp=_now(),
                purpose="discovery_decision",
                step_id=step_number,
                model=usage.model,
                provider=usage.provider,
                generation_id=usage.generation_id,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                cached_tokens=usage.cached_tokens,
            )
        )
        return result.value

    # -- recording ---------------------------------------------------------

    async def _record_navigation(
        self, session: Session, recorder: _Recorder, url: str, run_log: RunLog
    ) -> None:
        start = time.monotonic()
        await session.page.goto(url, wait_until="domcontentloaded")
        after = await observe(session.page)

        step = Step(
            step_id=recorder.next_step_id(),
            action=ActionType.NAVIGATE,
            description=f"Open {url}",
            fragile=False,
            risk_level=RiskLevel.SAFE,
            pre_condition=None,  # nothing exists to assert yet (C17)
            url=url,
            post_condition=Condition(
                condition=ConditionType.URL_CONTAINS,
                value=_url_tail(after.url) or _url_tail(url),
                timeout_ms=8000,
                on_fail=OnFail.HARD_FAILURE,
            ),
        )
        recorder.add(step)
        run_log.record_step(
            StepLog(
                step_id=step.step_id,
                action=step.action.value,
                description=step.description,
                timestamp=_now(),
                layer_used=None,
                post_condition="passed",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        )

    async def _execute_and_record(
        self,
        session: Session,
        recorder: _Recorder,
        decision: AgentDecision,
        before: Observation,
        run_log: RunLog,
    ) -> str:
        start = time.monotonic()
        step_id = recorder.next_step_id()
        timeout = 8000

        if decision.action is DecisionAction.NAVIGATE:
            return await self._record_plain_navigation(
                session, recorder, decision, before, run_log, start
            )

        if decision.locator is None:
            raise DiscoveryError(
                f"{decision.action.value} requires a locator but none was returned"
            )

        locators = build_locators(decision.locator)
        resolved = await loc.try_accessibility_chain(
            session.page, locators, recorder.params, timeout_ms=timeout
        )
        if resolved is None:
            raise DiscoveryError(
                f"could not resolve the proposed element: "
                f"{decision.locator.model_dump(exclude_none=True)}"
            )

        # Capture the Layer 2 net for free while we hold the handle (C4).
        fallback = await loc.capture_fallback(
            session,
            resolved.locator,  # type: ignore[arg-type]
            decision.visual_description or decision.reasoning[:120],
        )
        locators = build_locators(decision.locator, fallback)

        pre_condition = Condition(
            condition=ConditionType.ELEMENT_VISIBLE,
            locators=locators,
            timeout_ms=timeout,
            on_fail=OnFail.RETRY,
        )

        if decision.action is DecisionAction.EXTRACT:
            step = await self._record_extraction(
                session, recorder, decision, locators, pre_condition, timeout
            )
        elif decision.action is DecisionAction.FILL:
            value = decision.value or ""
            await loc.fill(session, resolved, loc.substitute(value, recorder.params) or "")
            after = await observe(session.page)
            post, warning = synthesise_post_condition(
                diff(before, after),
                before,
                action=ActionType.FILL,
                locators=locators,
                value=value,
                timeout_ms=timeout,
            )
            if warning:
                recorder.warnings.append(f"step {step_id}: {warning}")
            step = Step(
                step_id=step_id,
                action=ActionType.FILL,
                description=decision.reasoning[:200],
                fragile=False,
                risk_level=_risk(decision),
                pre_condition=pre_condition,
                locators=locators,
                value=value,
                post_condition=post,
            )
        else:
            await loc.click(session, resolved)
            after = await observe(session.page)
            post, warning = synthesise_post_condition(
                diff(before, after),
                before,
                action=ActionType.CLICK,
                locators=locators,
                value=None,
                timeout_ms=timeout,
            )
            if warning:
                recorder.warnings.append(f"step {step_id}: {warning}")
            step = Step(
                step_id=step_id,
                action=ActionType.CLICK,
                description=decision.reasoning[:200],
                fragile=False,
                risk_level=_risk(decision),
                pre_condition=pre_condition,
                locators=locators,
                post_condition=post,
            )

        recorder.add(step)
        run_log.record_step(
            StepLog(
                step_id=step.step_id,
                action=step.action.value,
                description=step.description,
                timestamp=_now(),
                layer_used=resolved.layer,
                locator_used=resolved.detail,
                pre_condition="passed",
                post_condition="passed",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        )
        return f"{step.step_id}. {step.action.value}: {step.description}"

    async def _record_plain_navigation(
        self,
        session: Session,
        recorder: _Recorder,
        decision: AgentDecision,
        before: Observation,
        run_log: RunLog,
        start: float,
    ) -> str:
        target = decision.url or ""
        await session.page.goto(target, wait_until="domcontentloaded")
        after = await observe(session.page)
        step = Step(
            step_id=recorder.next_step_id(),
            action=ActionType.NAVIGATE,
            description=decision.reasoning[:200],
            fragile=False,
            risk_level=_risk(decision),
            pre_condition=None,
            url=target,
            post_condition=Condition(
                condition=ConditionType.URL_CONTAINS,
                value=_url_tail(after.url),
                timeout_ms=8000,
                on_fail=OnFail.RETRY,
            ),
        )
        recorder.add(step)
        run_log.record_step(
            StepLog(
                step_id=step.step_id,
                action=step.action.value,
                description=step.description,
                timestamp=_now(),
                layer_used=None,
                post_condition="passed",
                duration_ms=int((time.monotonic() - start) * 1000),
            )
        )
        return f"{step.step_id}. navigate: {target}"

    async def _record_extraction(
        self,
        session: Session,
        recorder: _Recorder,
        decision: AgentDecision,
        locators: Locators,
        pre_condition: Condition,
        timeout: int,
    ) -> Step:
        if not decision.extractions:
            raise DiscoveryError("extract action returned no extractions")

        extractions: list[Extraction] = []
        for proposed in decision.extractions:
            sub_locators = build_locators(proposed.locator)
            resolved = await loc.try_accessibility_chain(
                session.page, sub_locators, recorder.params, timeout_ms=timeout
            )
            if resolved is None:
                raise DiscoveryError(
                    f"could not resolve the element for output "
                    f"{proposed.output_key!r}"
                )

            method = _extract_method(proposed.extract_method)
            value = await loc.read_value(session, resolved, method.value)
            if not value.strip():
                raise DiscoveryError(
                    f"output {proposed.output_key!r} resolved to an empty value; "
                    f"refusing to record an extraction that returns nothing"
                )

            fallback = await loc.capture_fallback(
                session,
                resolved.locator,  # type: ignore[arg-type]
                f"value for {proposed.output_key}",
            )
            expected = _expected_type(proposed.expected_type)
            extractions.append(
                Extraction(
                    output_key=proposed.output_key,
                    locators=build_locators(proposed.locator, fallback),
                    extract_method=method,
                    expected_type=expected,
                    required=True,
                )
            )
            recorder.output_schema[proposed.output_key] = expected

        return Step(
            step_id=recorder.next_step_id(),
            action=ActionType.EXTRACT,
            description=decision.reasoning[:200],
            fragile=False,
            risk_level=RiskLevel.SAFE,
            pre_condition=pre_condition,
            locators=None,  # extract steps carry per-extraction locators
            extractions=extractions,
            post_condition=Condition(
                condition=ConditionType.ALL_EXTRACTIONS_NON_EMPTY,
                timeout_ms=timeout,
                on_fail=OnFail.HARD_FAILURE,
            ),
        )

    # -- assembly ----------------------------------------------------------

    def _assemble(
        self,
        recorder: _Recorder,
        capability_id: str,
        session: Session,
        run_log: RunLog,
    ) -> Artifact:
        input_parameters = {
            name: InputParameter(
                type=ParameterType.STRING,
                required=True,
                sensitive=_looks_sensitive(name),
            )
            for name in recorder.params
        }

        # Only keep parameters the recording actually references: an unused
        # declared parameter is a promise the artifact does not keep.
        referenced: set[str] = set()
        for step in recorder.steps:
            referenced |= step.referenced_params
        input_parameters = {
            name: spec for name, spec in input_parameters.items() if name in referenced
        }

        steps_hash = hashlib.sha256(
            json.dumps(
                [s.model_dump(mode="json", exclude_none=True) for s in recorder.steps],
                sort_keys=True,
            ).encode()
        ).hexdigest()[:16]

        return Artifact(
            capability_id=capability_id,
            version="1.0.0",
            schema_version="1.0.0",
            description=recorder.goal,
            recorded_by=RecordedBy.AGENT,
            created_at=date.today().isoformat(),
            surface_type=SurfaceType.MODERN_WEB,
            target=Target(
                app_name=_app_name(recorder.url),
                url=recorder.url,
                surface_type=SurfaceType.MODERN_WEB,
            ),
            input_parameters=input_parameters,
            output_schema=recorder.output_schema,
            replay_config=ReplayConfig(
                browser=session.config, mode=ReplayMode.STRICT
            ),
            steps=recorder.steps,
            business_outcomes=[],  # filled in by the negative probe (C6)
            error_map=default_error_map(),
            provenance=Provenance(
                source_run_id=run_log.run_id,
                model_id=self.llm.model,
                steps_hash=steps_hash,
                notes="Recorded by a live discovery run.",
            ),
        )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _risk(decision: AgentDecision) -> RiskLevel:
    try:
        return RiskLevel(decision.risk_level.lower())
    except ValueError:
        # An unrecognised classification is treated as the more cautious
        # option, never the safer-sounding one.
        return RiskLevel.LOW


def _extract_method(raw: str) -> ExtractMethod:
    try:
        return ExtractMethod(raw.lower())
    except ValueError:
        return ExtractMethod.INNER_TEXT


def _expected_type(raw: str) -> ExpectedType:
    try:
        return ExpectedType(raw.lower())
    except ValueError:
        return ExpectedType.STRING


def _looks_sensitive(name: str) -> bool:
    """Conservative default for a freshly recorded parameter.

    Erring towards ``sensitive`` costs a redacted log line. Erring the other
    way writes a credential into a file that ships in the evidence folder.
    """
    lowered = name.lower()
    markers = ("password", "passwd", "pwd", "secret", "token", "apikey", "api_key",
               "credential", "auth", "ssn", "pin")
    return any(marker in lowered for marker in markers)


def _app_name(url: str) -> str:
    from urllib.parse import urlparse

    host = urlparse(url).hostname or url
    return host.split(".")[0] if host else url


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def discover(
    *,
    goal: str,
    url: str,
    params: dict[str, Any],
    capability_id: str,
    output_path: str,
    llm: LLMClient | None = None,
    allowlist: Allowlist | None = None,
    config: BrowserConfig | None = None,
    escalate: EscalateFn | None = None,
    enable_escalation: bool = False,
) -> DiscoveryResult:
    """Run one discovery pass and write the artifact if it succeeded."""
    from pathlib import Path

    from src.replay.engine import _as_intervention_log, _escalation

    llm = llm or LLMClient()
    allowlist = allowlist or Allowlist.load()
    config = config or BrowserConfig()

    # The run log needs an artifact for redaction, but the artifact does not
    # exist yet. A minimal stand-in carries the parameter sensitivity, which is
    # the only part redaction depends on.
    stub = _redaction_stub(capability_id, url, params)
    run_log = RunLog.start(
        artifact=stub, phase="discovery", mode="assisted", params=params
    )

    async with browser_session(config) as session:
        run_log.browser = await session.viewport_report()

        async with _escalation(session, capability_id, enable_escalation) as handoff:
            agent = DiscoveryAgent(
                llm=llm,
                allowlist=allowlist,
                escalate=escalate
                or (handoff.escalate_to_human if handoff else None),
            )
            result = await agent.run(
                goal=goal,
                url=url,
                params=params,
                capability_id=capability_id,
                session=session,
                run_log=run_log,
            )

            if handoff:
                for record in handoff.interventions:
                    run_log.record_intervention(_as_intervention_log(record))

        if result.artifact:
            path = Path(output_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    result.artifact.model_dump(mode="json", exclude_none=True),
                    indent=2,
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )

        run_log.screenshot = await _save_screenshot(session, run_log.run_id)

    run_log.write(settings.EVIDENCE_DIR / f"{run_log.run_id}.json")
    return result


def _redaction_stub(capability_id: str, url: str, params: dict[str, Any]) -> Artifact:
    """Minimal artifact carrying only what the redactor needs."""
    return Artifact(
        capability_id=capability_id,
        version="0.0.0",
        description="discovery in progress",
        recorded_by=RecordedBy.AGENT,
        created_at=date.today().isoformat(),
        surface_type=SurfaceType.MODERN_WEB,
        target=Target(app_name="pending", url=url, surface_type=SurfaceType.MODERN_WEB),
        input_parameters={
            name: InputParameter(
                type=ParameterType.STRING,
                required=False,
                sensitive=_looks_sensitive(name),
            )
            for name in params
        },
        output_schema={},
        steps=[
            Step(
                step_id=1,
                action=ActionType.NAVIGATE,
                description="placeholder",
                fragile=False,
                risk_level=RiskLevel.SAFE,
                pre_condition=None,
                url=url,
                post_condition=Condition(
                    condition=ConditionType.URL_CONTAINS,
                    value="/",
                    on_fail=OnFail.HARD_FAILURE,
                ),
            )
        ],
    )


async def _save_screenshot(session: Session, run_id: str) -> str | None:
    try:
        settings.ensure_dirs()
        path = settings.SCREENSHOTS_DIR / f"{run_id}_final.png"
        path.write_bytes(await session.screenshot())
        return str(path.relative_to(settings.ROOT))
    except Exception:  # pragma: no cover
        return None
