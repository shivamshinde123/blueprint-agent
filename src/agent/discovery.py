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

import contextlib
import hashlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, date
from typing import Any

from src import settings
from src.agent import prompts
from src.agent.decisions import AgentDecision, DecisionAction, ProposedLocator
from src.agent.observation import Change, Observation, diff, distinctive_new_text, observe
from src.artifact.reusability import assert_reusable, embeds
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

#: Grace period after the network goes quiet, for the framework to re-render.
RENDER_SETTLE_MS = 700

EscalateFn = Callable[[str, int], Awaitable[None]]


class DiscoveryError(RuntimeError):
    """Discovery could not produce an artifact."""


class AlreadyExtracted(DiscoveryError):
    """Every output the model asked for has already been recorded.

    Not a failure: it means the run is finished and the model simply restated
    its results. Handled by telling it so, rather than counting a step failure.
    """


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
    #: What the extractions actually returned this run. Held in memory only --
    #: never written to the artifact, since these are real record values.
    recorded_outputs: dict[str, str] = field(default_factory=dict)
    #: Did the most recent step visibly change the page? A step that executed
    #: cleanly but changed nothing is not progress -- see the dead-end note in
    #: DiscoveryAgent.run.
    last_step_changed: bool = True

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
        method=method,
        role=proposed.role,
        name=name,
        value=value,
        nth=proposed.nth,
        pattern=proposed.pattern,
    )


def candidate_methods(proposed: ProposedLocator) -> list[AccessibilityLocatorMethod]:
    """The proposed method first, then the other ways of asking for the same thing.

    The model picks *a* method, and it can pick a plausible-but-wrong one. On
    OrangeHRM's login page the accessibility tree exposes ``textbox "Password"``
    but there is no ``<label for>``, so a ``get_by_label("Password")`` proposal
    resolves to nothing even though the element is trivially findable by role.

    Rather than lose the step, derive the alternatives from the same name and
    try them in priority order. The artifact's ``methods`` list is built for
    exactly this — recording only the single proposed method threw away the
    resilience the schema was designed to carry.
    """
    primary = to_artifact_locator(proposed)
    label = proposed.name or proposed.value
    methods = [primary]

    # A shape-addressed locator identifies itself; there is no name to build
    # alternatives from, and treating the regex as one would be nonsense.
    if proposed.pattern or not label:
        return methods

    alternatives = [
        AccessibilityLocatorMethod(
            method=AccessibilityMethod.GET_BY_ROLE,
            role=proposed.role or "textbox",
            name=label,
            nth=proposed.nth,
        ),
        AccessibilityLocatorMethod(
            method=AccessibilityMethod.GET_BY_LABEL, name=label
        ),
        AccessibilityLocatorMethod(
            method=AccessibilityMethod.GET_BY_PLACEHOLDER, value=label
        ),
        AccessibilityLocatorMethod(
            method=AccessibilityMethod.GET_BY_TEXT, value=label
        ),
        # Last resort: the control sitting beside a caption the markup never
        # wired up. Without it, OrangeHRM's "Date of Birth" is unreachable by
        # every accessible-name method even though a person reads it at once.
        AccessibilityLocatorMethod(
            method=AccessibilityMethod.GET_BY_FIELD_LABEL, name=label
        ),
    ]
    for alternative in alternatives:
        if not any(_same_method(alternative, existing) for existing in methods):
            methods.append(alternative)
    return methods


def _same_method(a: AccessibilityLocatorMethod, b: AccessibilityLocatorMethod) -> bool:
    return (a.method, a.role, a.name, a.value) == (b.method, b.role, b.name, b.value)


def build_locators(
    proposed: ProposedLocator,
    fallback: Any | None = None,
    *,
    methods: list[AccessibilityLocatorMethod] | None = None,
) -> Locators:
    return Locators(
        primary=PrimaryLocator(
            methods=methods or [to_artifact_locator(proposed)], available=True
        ),
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
    avoid: set[str] | None = None,
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

    text = distinctive_new_text(change, before, avoid)
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
            (
                "no observable page change; checkpoint is weak and should be "
                "reviewed by hand"
            ),
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


async def settle(session: Session, timeout_ms: int = 5000) -> None:
    """Let a single-page app finish rendering before observing it.

    `domcontentloaded` fires before a Vue or React app has drawn anything, so
    a snapshot taken immediately after navigation shows an empty shell and the
    first locator finds nothing. Waiting for the network to go quiet is
    best-effort: a page with long-polling never reaches idle, and that is not
    a reason to fail the step.
    """
    with contextlib.suppress(Exception):
        await session.page.wait_for_load_state("networkidle", timeout=timeout_ms)
    # Network-quiet is not render-complete: a framework re-renders a beat after
    # its XHR resolves. OrangeHRM's PIM search goes from 342 named nodes to 57
    # in that gap, so a snapshot taken at networkidle shows the *old* table and
    # the step reads as "nothing changed" -- which sent the agent into clicking
    # Search over and over.
    #
    # This is a render settle before *observing*, not a wait for success. Every
    # actual success check is still a condition with a timeout; the replay
    # engine sleeps nowhere.
    with contextlib.suppress(Exception):
        await session.page.wait_for_timeout(RENDER_SETTLE_MS)


def _action_type(action: DecisionAction) -> ActionType | None:
    try:
        return ActionType(action.value)
    except ValueError:
        return None


def _locator_echoes_value(
    methods: list[AccessibilityLocatorMethod], value: str
) -> bool:
    """Does this locator find the element by the very text it is reading?

    Delegates to the shared predicate so the rule has one definition. This is
    the fast-feedback half: catching it here lets the model pick a different
    locator while it still has the page in front of it, rather than failing the
    whole recording at assembly.
    """
    return any(
        embeds(text, value)
        for method in methods
        for text in (method.name, method.value)
    )


def _apply_pattern(raw: str, pattern: str | None, output_key: str) -> str:
    """Isolate the value the pattern describes, or explain why it did not.

    Mirrors the replay engine, so a pattern that works while recording works
    identically on every replay -- there is no point recording one that only
    the recorder can satisfy.
    """
    import re as _re

    text = (raw or "").strip()
    if not pattern or not text:
        return text
    try:
        match = _re.search(pattern, text, _re.DOTALL)
    except _re.error as exc:
        raise DiscoveryError(
            f"the pattern for {output_key!r} is not a valid regular "
            f"expression: {exc}"
        ) from exc
    if match is None:
        raise DiscoveryError(
            f"the pattern for {output_key!r} matched nothing in the element's "
            f"text ({text[:120]!r}). The pattern must describe the shape of "
            f"the value as this page renders it."
        )
    return (match.group(1) if match.re.groups else match.group(0)).strip()


def _param_values(params: dict[str, Any]) -> set[str]:
    """Runtime values that must never end up inside a checkpoint."""
    return {str(v) for v in params.values() if v is not None and str(v).strip()}


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
                # An irreversible write -- creating a record, submitting a
                # transaction. Recording such a flow is legitimate and often
                # the point (that is what a capability *is*), but a person has
                # to authorise it before it happens for real.
                reason = (
                    f"discovery wants to perform a {decision.risk_level} risk "
                    f"action at step {recorder.next_step_id()}: "
                    f"{decision.reasoning}"
                )
                if self.escalate is None:
                    stopped = (
                        f"{reason}. No escalation handler is configured, so it "
                        f"was not performed. Re-run with --escalate to "
                        f"authorise it."
                    )
                    break
                # Blocks until the operator clicks Resume, then proceeds and
                # records the step with its risk level intact, so replay will
                # pause at the same point.
                await self.escalate(reason, recorder.next_step_id())
                log.info("human authorised the %s risk step", decision.risk_level)

            try:
                summary = await self._execute_and_record(
                    session, recorder, decision, before, run_log
                )
            except AlreadyExtracted as done:
                # Not a failure -- the outputs are in hand and the model is
                # restating them. Tell it so and let it close out the run.
                history.append(f"NOTE: {done}. The goal is complete.")
                if recorder.output_schema:
                    stopped = "goal achieved"
                    break
                continue
            except Exception as exc:
                history.append(f"FAILED: {decision.action.value} - {exc}")
                log.warning("step failed during discovery: %s", exc)
                if len(history) >= 2 and history[-2].startswith("FAILED"):
                    stopped = f"two consecutive step failures; last: {exc}"
                    await self._maybe_escalate(stopped, recorder.next_step_id())
                    break
                continue

            # Say plainly when a step achieved nothing. Otherwise the history
            # reads as an unbroken run of successes and the model has no signal
            # that it is repeating itself -- it clicked one Search button four
            # times in a row while its own history told it all four worked.
            if not recorder.last_step_changed:
                summary += "  <- this changed nothing on the page; try something else"
            history.append(summary)
            # Restart the dead-end window only on *productive* steps.
            #
            # Two failure modes sit either side of this line. Clearing on every
            # recorded step lets the agent click one button forever -- it once
            # pressed Search nineteen times, each click "succeeding" and
            # changing nothing. Never clearing reads a login form as a loop,
            # because typing into a field leaves every (role, name) pair in the
            # accessibility tree untouched. Progress therefore means the page
            # actually changed, with a fill counting on its own.
            if recorder.last_step_changed:
                fingerprints.clear()

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
        """True when several *unproductive* turns left the page identical.

        The window is cleared whenever a step is recorded, so this measures
        turns that achieved nothing rather than turns that merely looked
        similar. See the note where `fingerprints.clear()` is called.
        """
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

    async def _working_methods(
        self,
        session: Session,
        candidates: list[AccessibilityLocatorMethod],
        params: dict[str, Any],
        timeout: int,
        action: ActionType | None = None,
        extract_method: str | None = None,
        pattern: str | None = None,
    ) -> list[AccessibilityLocatorMethod]:
        """Keep the candidates that resolve *and* suit the action.

        Resolvability alone is not enough. ``get_by_text("Employee Name")``
        happily resolves OrangeHRM's ``<label>``, which is a perfectly real
        element that simply cannot be typed into -- Playwright fails with
        "Element is not an <input>". Recording that locator would bake the
        failure into the artifact, so the element is checked against what the
        step will actually do to it.
        """
        working: list[AccessibilityLocatorMethod] = []
        for method in candidates:
            probe = Locators(
                primary=PrimaryLocator(methods=[method], available=True), fallback=None
            )
            resolved = await loc.try_accessibility_chain(
                session.page, probe, params, timeout_ms=min(timeout, 2000)
            )
            if resolved is None or resolved.locator is None:
                continue
            if not await self._suits_action(resolved.locator, action, extract_method):
                continue

            # When a pattern says what shape the value has, use it to pick
            # between elements that share a name. A product page exposes the
            # product's name twice -- once as an image's alt text, once inside
            # the block that also holds the price -- and only one of them can
            # satisfy the pattern. Selecting by shape is not selecting by this
            # run's data, so the reusability rule still holds.
            if pattern:
                index = await self._index_matching_pattern(
                    session, method, params, pattern, extract_method, timeout
                )
                if index is None:
                    continue
                if index > 0:
                    method = method.model_copy(update={"nth": index})
            # Bake in which match was taken when the name was ambiguous, so
            # replay picks the same element rather than re-guessing.
            index = (resolved.detail or {}).get("nth")
            if index is not None:
                method = method.model_copy(update={"nth": index})
            working.append(method)
        return working

    async def _index_matching_pattern(
        self,
        session: Session,
        method: AccessibilityLocatorMethod,
        params: dict[str, Any],
        pattern: str,
        extract_method: str | None,
        timeout: int,
    ) -> int | None:
        """Which of the matching elements actually contains the value's shape?"""
        import re as _re

        try:
            candidate = loc.build_playwright_locator(session.page, method, params)
            count = min(await candidate.count(), 10)
        except Exception:
            return None

        for index in range(count):
            try:
                element = candidate.nth(index)
                if not await element.is_visible():
                    continue
                text = (
                    await element.input_value()
                    if extract_method == ExtractMethod.GET_VALUE.value
                    else await element.inner_text()
                )
                if _re.search(pattern, text or "", _re.DOTALL):
                    return index
            except Exception:
                continue
        return None

    @staticmethod
    async def _suits_action(
        locator: Any, action: ActionType | None, extract_method: str | None = None
    ) -> bool:
        """Can this element actually take the action we are about to record?

        Resolvability is not enough. A ``<label>`` resolves perfectly well and
        then fails at use: `fill` reports "Element is not an <input>", and
        `input_value()` reports "Node is not an <input>". Worse, for a text
        extraction a label *succeeds* and returns the field's caption -- "Date
        of Birth" instead of the date -- which is a silently wrong artifact.
        """
        try:
            tag = str(await locator.evaluate("el => el.tagName")).upper()

            if action is ActionType.FILL:
                return await locator.is_editable()

            if action is ActionType.CLICK:
                if not await locator.is_enabled():
                    return False
                # Clicking a label only proxies focus to its input.
                return tag != "LABEL"

            if action is ActionType.EXTRACT:
                # A label holds the caption, never the value.
                if tag == "LABEL":
                    return False
                if extract_method == ExtractMethod.GET_VALUE.value:
                    return tag in ("INPUT", "TEXTAREA", "SELECT")
                return True
        except Exception:
            return False
        return True

    async def _record_navigation(
        self, session: Session, recorder: _Recorder, url: str, run_log: RunLog
    ) -> None:
        start = time.monotonic()
        await session.page.goto(url, wait_until="domcontentloaded")
        await settle(session)
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

        # Extract steps carry per-extraction locators rather than one top-level
        # locator, because a single step can read several fields from different
        # places on the page. Requiring a top-level locator here rejected a
        # perfectly well-formed extract decision.
        if decision.action is DecisionAction.EXTRACT:
            step = await self._record_extraction(
                session, recorder, decision, None, None, timeout
            )
            recorder.add(step)
            run_log.record_step(
                StepLog(
                    step_id=step.step_id,
                    action=step.action.value,
                    description=step.description,
                    timestamp=_now(),
                    layer_used="accessibility_tree",
                    pre_condition="skipped",
                    post_condition="passed",
                    duration_ms=int((time.monotonic() - start) * 1000),
                )
            )
            return f"{step.step_id}. extract: {sorted(recorder.output_schema)}"

        if decision.locator is None:
            raise DiscoveryError(
                f"{decision.action.value} requires a locator but none was returned"
            )

        # Try the model's method, then the other ways of asking for the same
        # element, and keep only the ones that genuinely resolve.
        working = await self._working_methods(
            session,
            candidate_methods(decision.locator),
            recorder.params,
            timeout,
            action=_action_type(decision.action),
        )
        if not working:
            raise DiscoveryError(
                f"could not resolve the proposed element by any accessibility "
                f"method: {decision.locator.model_dump(exclude_none=True)}"
            )

        locators = build_locators(decision.locator, methods=working)
        resolved = await loc.try_accessibility_chain(
            session.page, locators, recorder.params, timeout_ms=timeout
        )
        if resolved is None:  # pragma: no cover - just verified above
            raise DiscoveryError("element vanished between probe and use")

        # Capture the Layer 2 net for free while we hold the handle (C4).
        fallback = await loc.capture_fallback(
            session,
            resolved.locator,  # type: ignore[arg-type]
            decision.visual_description or decision.reasoning[:120],
        )
        locators = build_locators(decision.locator, fallback, methods=working)

        pre_condition = Condition(
            condition=ConditionType.ELEMENT_VISIBLE,
            locators=locators,
            timeout_ms=timeout,
            on_fail=OnFail.RETRY,
        )

        if decision.action is DecisionAction.FILL:
            value = decision.value or ""
            await loc.fill(session, resolved, loc.substitute(value, recorder.params) or "")
            await settle(session, 2000)
            after = await observe(session.page)
            change = diff(before, after)
            recorder.last_step_changed = True  # a fill is progress by itself
            post, warning = synthesise_post_condition(
                change,
                before,
                action=ActionType.FILL,
                locators=locators,
                value=value,
                timeout_ms=timeout,
                avoid=_param_values(recorder.params),
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
            # A single-page app re-renders on click without navigating, so the
            # new view does not exist yet. Observing immediately shows an empty
            # shell: OrangeHRM's PIM page reports 0 interactive nodes right
            # after the click and 24 once the network goes quiet.
            await settle(session)
            after = await observe(session.page)
            change = diff(before, after)
            recorder.last_step_changed = change.anything_changed
            post, warning = synthesise_post_condition(
                change,
                before,
                action=ActionType.CLICK,
                locators=locators,
                value=None,
                timeout_ms=timeout,
                avoid=_param_values(recorder.params),
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
        await settle(session)
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
        locators: Locators | None,
        pre_condition: Condition | None,
        timeout: int,
    ) -> Step:
        if not decision.extractions:
            raise DiscoveryError("extract action returned no extractions")

        # The model can propose the same field twice -- re-extracting after a
        # page settles, or restating outputs it already has. Recording both
        # produces an artifact the validator rejects for duplicate output keys,
        # which throws away an otherwise complete run at the last moment.
        fresh = [
            e for e in decision.extractions
            if e.output_key not in recorder.output_schema
        ]
        if not fresh:
            raise AlreadyExtracted(
                f"every requested output is already recorded: "
                f"{sorted(recorder.output_schema)}"
            )

        extractions: list[Extraction] = []
        # Staged, not written straight into the recorder: if a later extraction
        # in this same step fails, the step is never committed, and a schema
        # already carrying the earlier keys would declare outputs that nothing
        # produces. The artifact validator catches that -- at the very end of an
        # otherwise complete run.
        staged_schema: dict[str, ExpectedType] = {}
        staged_outputs: dict[str, str] = {}
        for proposed in fresh:
            working = await self._working_methods(
                session,
                candidate_methods(proposed.locator),
                recorder.params,
                timeout,
                action=ActionType.EXTRACT,
                extract_method=proposed.extract_method,
                pattern=proposed.pattern,
            )
            if not working:
                raise DiscoveryError(
                    f"could not resolve the element for output "
                    f"{proposed.output_key!r}"
                )
            sub_locators = build_locators(proposed.locator, methods=working)
            resolved = await loc.try_accessibility_chain(
                session.page, sub_locators, recorder.params, timeout_ms=timeout
            )
            if resolved is None:  # pragma: no cover - just verified above
                raise DiscoveryError(
                    f"element for {proposed.output_key!r} vanished between "
                    f"probe and read"
                )

            method = _extract_method(proposed.extract_method)
            raw = await loc.read_value(session, resolved, method.value)
            value = _apply_pattern(raw, proposed.pattern, proposed.output_key)
            if not value.strip():
                raise DiscoveryError(
                    f"output {proposed.output_key!r} resolved to an empty value; "
                    f"refusing to record an extraction that returns nothing"
                )

            # An extraction locator must not be identified by the value it
            # reads. That is circular -- it only finds the element when the
            # answer is already known -- so the artifact returns this run's
            # value for every future input. A real run recorded
            # `get_by_text("$29.99")` for a product price, which would have
            # reported $29.99 for every product in the catalogue.
            if _locator_echoes_value(working, value):
                raise DiscoveryError(
                    f"the locator for {proposed.output_key!r} is identified by "
                    f"the value it reads, so it would only ever find this run's "
                    f"answer. Locate the element by its label, its role, or its "
                    f"position on the page instead -- never by its contents."
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
                    locators=build_locators(
                        proposed.locator, fallback, methods=working
                    ),
                    extract_method=method,
                    expected_type=expected,
                    required=True,
                    pattern=proposed.pattern,
                )
            )
            staged_schema[proposed.output_key] = expected
            staged_outputs[proposed.output_key] = value

        # Every extraction resolved and returned a value: commit the schema.
        recorder.output_schema.update(staged_schema)
        recorder.recorded_outputs.update(staged_outputs)

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

        artifact = Artifact(
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

        # The backstop. Everything above can be individually valid while the
        # recording as a whole only works for the input it just saw, and
        # nothing else in the system would notice -- replay would succeed and
        # return this run's answer forever. Checked here with the values still
        # in memory, so none of them has to be written down.
        assert_reusable(
            artifact,
            {**recorder.params, **recorder.recorded_outputs},
            sensitive=artifact.sensitive_parameters(),
        )
        return artifact


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds")


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
