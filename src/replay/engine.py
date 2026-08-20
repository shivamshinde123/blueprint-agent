"""Phase 2: deterministic replay.

Reads an artifact and executes it mechanically. In strict mode — the default,
and the mode used for evidence runs — no model is contacted at all.

The per-step order is deliberate and is the heart of the contract:

    session recovery -> dismiss interstitials -> pre-condition -> risk gate
    -> resolve element -> act -> CHECK BUSINESS OUTCOMES -> post-condition -> log

Business outcomes are checked *before* anything is treated as a failure. "No
Records Found" is an answer the caller asked for, not a crash, and a system
that confuses the two is useless to the agent invoking it.

See PLAN.md §6.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC
from decimal import Decimal, InvalidOperation
from typing import Any

from src import settings
from src.artifact.schema import (
    ActionType,
    Artifact,
    BusinessOutcome,
    ErrorTypeKey,
    ExpectedType,
    Extraction,
    Interstitial,
    ReplayMode,
    ResultType,
    Step,
)
from src.artifact.validator import check_browser_matches, preflight_replay
from src.evidence.logger import RunLog, StepLog
from src.llm.client import LLMClient
from src.replay import conditions as cond
from src.replay import locator as loc
from src.replay.error_handler import (
    Disposition,
    ErrorTracker,
    EscalateToHuman,
    ReplayFailure,
    classify_exception,
)
from src.safety.guardrails import Allowlist, describe_risk, requires_approval
from src.session.browser import Session, browser_session

log = logging.getLogger(__name__)

#: Called as ``await escalate(reason, step_id)``. Blocks until a human resumes.
EscalateFn = Callable[[str, int], Awaitable[None]]


@dataclass(slots=True)
class ReplayResult:
    """One of exactly three shapes. See PLAN.md §4.13."""

    result_type: ResultType
    capability_id: str
    outputs: dict[str, Any] | None
    steps_completed: int
    total_steps: int
    layer2_used: bool
    llm_calls: int
    duration_ms: int
    evidence: dict[str, Any] = field(default_factory=dict)
    outcome_code: str | None = None
    outcome_message: str | None = None
    is_error: bool | None = None
    failure: dict[str, Any] | None = None

    @property
    def succeeded(self) -> bool:
        return self.result_type is ResultType.SUCCESS

    def summary(self) -> str:
        if self.result_type is ResultType.SUCCESS:
            return f"success: {self.outputs}"
        if self.result_type is ResultType.BUSINESS_OUTCOME:
            return f"{self.outcome_code}: {self.outcome_message}"
        detail = self.failure or {}
        return (
            f"failure at step {detail.get('failed_at_step')} "
            f"({detail.get('error_type')}): {detail.get('message')}"
        )


class ReplayEngine:
    """Executes an artifact. Holds no state between runs."""

    def __init__(
        self,
        artifact: Artifact,
        *,
        mode: ReplayMode | None = None,
        llm: LLMClient | None = None,
        escalate: EscalateFn | None = None,
    ) -> None:
        self.artifact = artifact
        self.mode = mode or artifact.replay_config.mode
        self.llm = llm
        self.escalate = escalate
        self.tracker = ErrorTracker(artifact)
        self.llm_budget = artifact.replay_config.max_llm_calls_per_replay
        self.layer2_used = False
        self.llm_calls = 0
        self._recovery_attempts = 0

    # -- main loop ---------------------------------------------------------

    async def run(
        self, session: Session, params: dict[str, Any], run_log: RunLog
    ) -> ReplayResult:
        started = time.monotonic()
        outputs: dict[str, Any] = {}
        completed = 0

        config = self.artifact.replay_config
        check_browser_matches(config.browser, await session.viewport_report())

        try:
            index = 0
            while index < len(self.artifact.steps):
                step = self.artifact.steps[index]
                outcome = await self._run_step(session, step, params, outputs, run_log)

                if isinstance(outcome, BusinessOutcome):
                    return self._business_outcome(
                        outcome, completed, started, run_log
                    )
                if outcome == "restart":
                    # Session recovery re-ran the current step from the top.
                    continue

                completed += 1
                index += 1

        except ReplayFailure as failure:
            return await self._failure(failure, completed, started, session, run_log)
        except EscalateToHuman as escalation:
            handled = await self._handle_escalation(escalation, session, run_log)
            if not handled:
                failure = ReplayFailure(
                    step_id=escalation.step_id,
                    error_type=ErrorTypeKey.WRONG_PAGE_STATE,
                    message=escalation.reason,
                    expected="a human to take over and resume the session",
                    observed="no escalation handler was configured",
                )
                return await self._failure(failure, completed, started, session, run_log)
            # A resumed handoff means the human completed the stuck step, so
            # the engine continues from the next one. See PLAN.md §8.5.
            return await self._resume_after_handoff(
                session, params, outputs, run_log, escalation.step_id, started
            )

        run_log.finish_success(outputs)
        return ReplayResult(
            result_type=ResultType.SUCCESS,
            capability_id=self.artifact.capability_id,
            outputs=outputs,
            steps_completed=completed,
            total_steps=len(self.artifact.steps),
            layer2_used=self.layer2_used,
            llm_calls=self.llm_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _resume_after_handoff(
        self,
        session: Session,
        params: dict[str, Any],
        outputs: dict[str, Any],
        run_log: RunLog,
        stuck_step_id: int,
        started: float,
    ) -> ReplayResult:
        """Continue from the step after the one the human completed."""
        remaining = [s for s in self.artifact.steps if s.step_id > stuck_step_id]
        completed = stuck_step_id
        try:
            for step in remaining:
                outcome = await self._run_step(session, step, params, outputs, run_log)
                if isinstance(outcome, BusinessOutcome):
                    return self._business_outcome(outcome, completed, started, run_log)
                completed += 1
        except ReplayFailure as failure:
            return await self._failure(failure, completed, started, session, run_log)
        except EscalateToHuman as exc:  # pragma: no cover - second handoff
            failure = ReplayFailure(
                step_id=exc.step_id,
                error_type=ErrorTypeKey.WRONG_PAGE_STATE,
                message=exc.reason,
                expected="the flow to complete after the handoff",
                observed="a second escalation was raised",
            )
            return await self._failure(failure, completed, started, session, run_log)

        run_log.finish_success(outputs)
        return ReplayResult(
            result_type=ResultType.SUCCESS,
            capability_id=self.artifact.capability_id,
            outputs=outputs,
            steps_completed=completed,
            total_steps=len(self.artifact.steps),
            layer2_used=self.layer2_used,
            llm_calls=self.llm_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    # -- one step ----------------------------------------------------------

    async def _run_step(
        self,
        session: Session,
        step: Step,
        params: dict[str, Any],
        outputs: dict[str, Any],
        run_log: RunLog,
    ) -> BusinessOutcome | str | None:
        config = self.artifact.replay_config
        started = time.monotonic()
        entry = StepLog(
            step_id=step.step_id,
            action=step.action.value,
            description=step.description,
            timestamp=_now(),
        )

        while True:
            # 1. Session recovery, before anything else.
            if await self._recover_session_if_needed(session, params):
                entry.notes = "session recovered; step retried"
                return "restart"

            # 2. Interstitials, probed cheaply (C9).
            await self._dismiss_interstitials(session, params)

            # 3. Pre-condition.
            if step.pre_condition is not None:
                result = await cond.evaluate(
                    session,
                    step.pre_condition,
                    params,
                    default_timeout_ms=config.default_timeout_ms,
                )
                entry.pre_condition = "passed" if result.passed else "failed"
                if not result.passed:
                    verdict = self.tracker.record(
                        step.step_id, ErrorTypeKey.WRONG_PAGE_STATE
                    )
                    if await self._apply(verdict, step, session, result.expected, result.observed):
                        entry.retries += 1
                        continue

            # 4. Risk gate — always, no override.
            if requires_approval(step):
                await self._require_approval(step, session, run_log)

            # 5-6. Resolve and act.
            try:
                resolved = await self._act(session, step, params, outputs)
            except ReplayFailure:
                # Already classified, and the artifact may declare a specific
                # handler for that error type. Re-classifying it here would
                # silently route an extraction_empty to the wrong_page_state
                # handler instead.
                raise
            except Exception as exc:
                error_type = classify_exception(exc)
                verdict = self.tracker.record(step.step_id, error_type)
                if await self._apply(
                    verdict, step, session, "the element to be resolvable", str(exc)
                ):
                    entry.retries += 1
                    continue
                raise  # pragma: no cover - _apply always raises or returns True

            if resolved is not None:
                entry.layer_used = resolved.layer
                entry.locator_used = resolved.detail
                if resolved.layer == "screenshot":
                    self.layer2_used = True

            # 7. Business outcomes, BEFORE any failure handling.
            outcome = await self._detect_business_outcome(session, step, params)
            if outcome is not None:
                entry.outcome = f"business_outcome:{outcome.outcome_code}"
                entry.duration_ms = int((time.monotonic() - started) * 1000)
                run_log.record_step(entry)
                return outcome

            # 8. Post-condition.
            result = await cond.evaluate(
                session,
                step.post_condition,
                params,
                default_timeout_ms=config.default_timeout_ms,
                extracted=outputs if step.action is ActionType.EXTRACT else None,
            )
            entry.post_condition = "passed" if result.passed else "failed"
            if not result.passed:
                verdict = self.tracker.record(
                    step.step_id, ErrorTypeKey.CHECKPOINT_FAILED
                )
                if await self._apply(
                    verdict, step, session, result.expected, result.observed
                ):
                    entry.retries += 1
                    continue

            # 9. Settle, only if the artifact asks for it.
            if step.step_wait_ms:
                await cond._sleep_ms(step.step_wait_ms)

            self.tracker.reset_step(step.step_id)
            entry.duration_ms = int((time.monotonic() - started) * 1000)
            run_log.record_step(entry)
            return None

    async def _act(
        self,
        session: Session,
        step: Step,
        params: dict[str, Any],
        outputs: dict[str, Any],
    ) -> loc.Resolved | None:
        if step.action is ActionType.NAVIGATE:
            await session.page.goto(step.url or "", wait_until="domcontentloaded")
            return None

        if step.action is ActionType.EXTRACT:
            await self._extract(session, step, params, outputs)
            return None

        if step.locators is None:  # pragma: no cover - schema-guarded
            raise ReplayFailure(
                step_id=step.step_id,
                error_type=ErrorTypeKey.ELEMENT_NOT_FOUND,
                message="step has no locators",
                expected="a locator",
                observed="none",
            )

        resolved = await self._resolve(session, step.locators, params, step)

        if step.action is ActionType.CLICK:
            await loc.click(session, resolved)
        elif step.action is ActionType.FILL:
            value = loc.substitute(step.value, params) or ""
            await loc.fill(session, resolved, value)

        return resolved

    async def _resolve(
        self,
        session: Session,
        locators: Any,
        params: dict[str, Any],
        step: Step,
    ) -> loc.Resolved:
        config = self.artifact.replay_config
        budget = max(0, self.llm_budget - self.llm_calls)
        resolved = await loc.resolve(
            session,
            locators,
            params,
            mode=self.mode,
            timeout_ms=config.default_timeout_ms,
            fragile=step.fragile,
            llm=self.llm,
            budget_remaining=budget,
            step_id=step.step_id,
        )
        if resolved.layer == "screenshot":
            self.llm_calls += 1
        return resolved

    async def _extract(
        self,
        session: Session,
        step: Step,
        params: dict[str, Any],
        outputs: dict[str, Any],
    ) -> None:
        timeout_ms = self.artifact.replay_config.default_timeout_ms
        for extraction in step.extractions or []:
            resolved = await self._resolve(session, extraction.locators, params, step)
            raw = await loc.read_value(
                session, resolved, extraction.extract_method.value
            )
            value = apply_pattern(raw, extraction, step.step_id)

            # A framework populates field values after the route has already
            # changed, so an empty read moments after navigation usually means
            # "not rendered yet" rather than "no value". Wait for it, rather
            # than failing a run that would have succeeded a heartbeat later.
            if not value and extraction.required:
                deadline = time.monotonic() + timeout_ms / 1000
                while not value and time.monotonic() < deadline:
                    await cond._sleep_ms(cond.POLL_INTERVAL_MS)
                    raw = await loc.read_value(
                        session, resolved, extraction.extract_method.value
                    )
                    value = apply_pattern(raw, extraction, step.step_id)

            if not value and extraction.required:
                raise ReplayFailure(
                    step_id=step.step_id,
                    error_type=ErrorTypeKey.EXTRACTION_EMPTY,
                    message=f"required output {extraction.output_key!r} was empty",
                    expected=f"a non-empty {extraction.expected_type.value}",
                    observed="empty string",
                )

            outputs[extraction.output_key] = coerce(
                value, extraction.expected_type, step.step_id, extraction.output_key
            )

    # -- supporting machinery ---------------------------------------------

    async def _detect_business_outcome(
        self, session: Session, step: Step, params: dict[str, Any]
    ) -> BusinessOutcome | None:
        for outcome in self.artifact.outcomes_for_step(step.step_id):
            if await cond.probe(
                session,
                outcome.detect,
                params,
                timeout_ms=outcome.detect.timeout_ms or 1000,
            ):
                return outcome
        return None

    async def _dismiss_interstitials(
        self, session: Session, params: dict[str, Any]
    ) -> None:
        probe_timeout = self.artifact.replay_config.interstitial_probe_timeout_ms
        for interstitial in self.artifact.known_interstitials:
            try:
                if not await cond.probe(
                    session, interstitial.detect, params, timeout_ms=probe_timeout
                ):
                    continue
                await self._dismiss(session, interstitial, params)
            except Exception:
                log.debug(
                    "interstitial %s could not be handled", interstitial.name,
                    exc_info=True,
                )

    async def _dismiss(
        self, session: Session, interstitial: Interstitial, params: dict[str, Any]
    ) -> None:
        from src.artifact.schema import DismissAction

        dismiss = interstitial.dismiss
        timeout = dismiss.timeout_ms or self.artifact.replay_config.default_timeout_ms

        if dismiss.dismiss_action is DismissAction.CLICK:
            found = await loc.try_accessibility_chain(
                session.page, dismiss.locators, params, timeout_ms=timeout  # type: ignore[arg-type]
            )
            if found:
                await loc.click(session, found)
            return

        # wait_for_hidden: a spinner that clears on its own.
        deadline = time.monotonic() + timeout / 1000
        while time.monotonic() < deadline:
            still_there = await cond.probe(
                session, interstitial.detect, params, timeout_ms=100
            )
            if not still_there:
                return
        from src.artifact.schema import OnTimeout

        if dismiss.on_timeout is OnTimeout.HARD_FAILURE:
            raise ReplayFailure(
                step_id=0,
                error_type=ErrorTypeKey.TIMEOUT,
                message=f"interstitial {interstitial.name!r} never cleared",
                expected=f"{interstitial.name} to disappear within {timeout}ms",
                observed="it is still on screen",
            )

    async def _recover_session_if_needed(
        self, session: Session, params: dict[str, Any]
    ) -> bool:
        recovery = self.artifact.session_recovery
        if recovery is None:
            return False

        expired = False
        for detector in recovery.detect_conditions:
            if await cond.probe(session, detector, params, timeout_ms=200):
                expired = True
                break
        if not expired:
            return False

        if self._recovery_attempts >= recovery.max_recovery_attempts:
            raise EscalateToHuman(
                "the session expired and re-login has already been attempted "
                f"{self._recovery_attempts} time(s)",
                0,
            )
        self._recovery_attempts += 1
        log.info("session expired; running recovery steps")

        for recovery_step in recovery.recovery_steps:
            if recovery_step.action is ActionType.NAVIGATE:
                await session.page.goto(
                    loc.substitute(recovery_step.url, params) or "",
                    wait_until="domcontentloaded",
                )
                continue
            if recovery_step.locators is None:  # pragma: no cover
                continue
            found = await loc.try_accessibility_chain(
                session.page,
                recovery_step.locators,
                params,
                timeout_ms=self.artifact.replay_config.default_timeout_ms,
            )
            if found is None:
                raise EscalateToHuman(
                    "session recovery could not find an element it needed", 0
                )
            if recovery_step.action is ActionType.CLICK:
                await loc.click(session, found)
            elif recovery_step.action is ActionType.FILL:
                await loc.fill(
                    session, found, loc.substitute(recovery_step.value, params) or ""
                )

        result = await cond.evaluate(
            session,
            recovery.recovery_post_condition,
            params,
            default_timeout_ms=self.artifact.replay_config.default_timeout_ms,
        )
        if not result.passed:
            raise EscalateToHuman(
                f"session recovery failed: expected {result.expected}, "
                f"observed {result.observed}",
                0,
            )
        return True

    async def _require_approval(
        self, step: Step, session: Session, run_log: RunLog
    ) -> None:
        """Pause before a high/critical step. The human authorises; the
        automation then performs the action itself. See PLAN.md §8.5."""
        reason = describe_risk(step)
        if self.escalate is None:
            raise ReplayFailure(
                step_id=step.step_id,
                error_type=ErrorTypeKey.WRONG_PAGE_STATE,
                message=reason,
                expected="a configured escalation handler to obtain authorisation",
                observed="none was configured, so the step was not executed",
            )
        await self.escalate(reason, step.step_id)

    async def _apply(
        self,
        verdict: Any,
        step: Step,
        session: Session,
        expected: str,
        observed: str,
    ) -> bool:
        """Act on a verdict. Returns True to retry the step, else raises."""
        if verdict.disposition is Disposition.RETRY:
            if verdict.wait_ms:
                await cond._sleep_ms(verdict.wait_ms)
            log.info(
                "retrying step %s (%s, attempt %s/%s)",
                step.step_id,
                verdict.error_type.value,
                verdict.attempt,
                verdict.max_retries,
            )
            return True

        if verdict.disposition is Disposition.RE_LOGIN:
            self._recovery_attempts = 0
            return True

        if verdict.disposition is Disposition.ESCALATE:
            raise EscalateToHuman(verdict.message, step.step_id)

        raise ReplayFailure(
            step_id=step.step_id,
            error_type=verdict.error_type,
            message=verdict.message,
            expected=expected,
            observed=observed,
        )

    async def _handle_escalation(
        self, escalation: EscalateToHuman, session: Session, run_log: RunLog
    ) -> bool:
        if self.escalate is None:
            return False
        await self.escalate(escalation.reason, escalation.step_id)
        return True

    # -- results -----------------------------------------------------------

    def _business_outcome(
        self,
        outcome: BusinessOutcome,
        completed: int,
        started: float,
        run_log: RunLog,
    ) -> ReplayResult:
        run_log.finish_business_outcome(
            outcome.outcome_code, outcome.outcome_message, outcome.return_value
        )
        return ReplayResult(
            result_type=ResultType.BUSINESS_OUTCOME,
            capability_id=self.artifact.capability_id,
            outputs=dict(outcome.return_value),
            steps_completed=completed,
            total_steps=len(self.artifact.steps),
            layer2_used=self.layer2_used,
            llm_calls=self.llm_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
            outcome_code=outcome.outcome_code,
            outcome_message=outcome.outcome_message,
            is_error=False,
        )

    async def _failure(
        self,
        failure: ReplayFailure,
        completed: int,
        started: float,
        session: Session,
        run_log: RunLog,
    ) -> ReplayResult:
        evidence = await self._capture_failure_evidence(session, run_log.run_id)
        run_log.screenshot = evidence.get("screenshot")
        run_log.dom_snapshot = evidence.get("dom_snapshot")
        run_log.finish_failure(
            failed_at_step=failure.step_id,
            error_type=failure.error_type.value,
            message=failure.message,
            expected=failure.expected,
            observed=failure.observed,
        )
        return ReplayResult(
            result_type=ResultType.FAILURE,
            capability_id=self.artifact.capability_id,
            outputs=None,
            steps_completed=completed,
            total_steps=len(self.artifact.steps),
            layer2_used=self.layer2_used,
            llm_calls=self.llm_calls,
            duration_ms=int((time.monotonic() - started) * 1000),
            evidence=evidence,
            failure=failure.as_dict(),
        )

    async def _capture_failure_evidence(
        self, session: Session, run_id: str
    ) -> dict[str, Any]:
        """Screenshot and DOM at the moment of failure.

        Note the residual exposure: a failure screenshot captures whatever is
        on screen, which may include PII. Redaction cannot reach inside a PNG.
        Stated as a limit rather than implied away. See PLAN.md §11 C12.
        """
        evidence: dict[str, Any] = {}
        settings.ensure_dirs()
        try:
            path = settings.SCREENSHOTS_DIR / f"{run_id}_failure.png"
            path.write_bytes(await session.screenshot())
            evidence["screenshot"] = str(path.relative_to(settings.ROOT))
        except Exception:  # pragma: no cover
            pass
        try:
            path = settings.SCREENSHOTS_DIR / f"{run_id}_failure.html"
            path.write_text(await session.page.content(), encoding="utf-8")
            evidence["dom_snapshot"] = str(path.relative_to(settings.ROOT))
        except Exception:  # pragma: no cover
            pass
        return evidence


# --------------------------------------------------------------------------
# Type coercion
# --------------------------------------------------------------------------


def apply_pattern(raw: str, extraction: Extraction, step_id: int) -> str:
    """Isolate the value inside an element's text.

    Without a pattern this is just a trim. With one, it is how a value gets
    extracted from an element that holds several -- a product node carrying
    name, description and price together, say. Deterministic either way: a
    regex needs no model, so strict replay stays free of model calls.
    """
    text = (raw or "").strip()
    if not extraction.pattern or not text:
        return text

    match = re.search(extraction.pattern, text, re.DOTALL)
    if match is None:
        if not extraction.required:
            return ""
        raise ReplayFailure(
            step_id=step_id,
            error_type=ErrorTypeKey.EXTRACTION_EMPTY,
            message=(
                f"the pattern for {extraction.output_key!r} matched nothing in "
                f"the element's text"
            ),
            expected=f"text matching {extraction.pattern!r}",
            observed=f"{text[:160]!r}",
        )
    return (match.group(1) if match.re.groups else match.group(0)).strip()


def coerce(
    value: str, expected: ExpectedType, step_id: int, key: str
) -> Any:
    """Normalise an extracted string to its declared type.

    ``currency`` is the one that needs a real contract: a balance reads as
    ``"$12,480.55"``, and without normalisation the declared type is decorative
    and the caller gets an unparseable string. See PLAN.md §11 C14.
    """
    if expected is ExpectedType.STRING:
        return value

    if expected is ExpectedType.INTEGER:
        cleaned = value.replace(",", "").replace(" ", "").strip()
        try:
            return int(cleaned)
        except ValueError as exc:
            raise ReplayFailure(
                step_id=step_id,
                error_type=ErrorTypeKey.EXTRACTION_EMPTY,
                message=f"output {key!r} is declared integer but read {value!r}",
                expected="an integer",
                observed=value,
            ) from exc

    if expected is ExpectedType.CURRENCY:
        return {"raw": value, "normalized": _parse_currency(value, step_id, key)}

    if expected is ExpectedType.BOOLEAN:
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "y", "1", "on", "enabled"):
            return True
        if lowered in ("false", "no", "n", "0", "off", "disabled"):
            return False
        raise ReplayFailure(
            step_id=step_id,
            error_type=ErrorTypeKey.EXTRACTION_EMPTY,
            message=f"output {key!r} is declared boolean but read {value!r}",
            expected="a boolean-like value",
            observed=value,
        )

    return value  # pragma: no cover


#: Characters stripped before parsing a currency amount.
_CURRENCY_NOISE = "$£€¥₹,    "


def _parse_currency(value: str, step_id: int, key: str) -> str:
    cleaned = value.strip()
    for ch in _CURRENCY_NOISE:
        cleaned = cleaned.replace(ch, "")
    # Trailing/leading currency codes: "12480.55 USD", "USD 12480.55".
    cleaned = "".join(c for c in cleaned if c.isdigit() or c in ".-")
    negative = value.strip().startswith("(") and value.strip().endswith(")")

    try:
        amount = Decimal(cleaned)
    except (InvalidOperation, ValueError) as exc:
        raise ReplayFailure(
            step_id=step_id,
            error_type=ErrorTypeKey.EXTRACTION_EMPTY,
            message=(
                f"output {key!r} is declared currency but {value!r} could not "
                f"be parsed as an amount"
            ),
            expected="a parseable currency amount",
            observed=value,
        ) from exc

    if negative:
        amount = -amount
    return str(amount)


def _now() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


async def replay(
    artifact: Artifact,
    params: dict[str, Any],
    *,
    mode: ReplayMode | None = None,
    allowlist: Allowlist | None = None,
    llm: LLMClient | None = None,
    escalate: EscalateFn | None = None,
    enable_escalation: bool = False,
) -> tuple[ReplayResult, RunLog]:
    """Validate, execute, and write the evidence log.

    ``enable_escalation`` starts the operator console and wires a handoff
    manager to the live session. It is a flag rather than a caller-supplied
    callable because the manager needs the browser, which does not exist until
    this function opens it.
    """
    allowlist = allowlist or Allowlist.load()

    preflight = preflight_replay(
        artifact,
        params,
        allowlist=allowlist,
        mode=mode,
        has_handoff=escalate is not None or enable_escalation,
    )
    resolved_mode = preflight.mode

    run_log = RunLog.start(
        artifact=artifact,
        phase="replay",
        mode=resolved_mode.value,
        params=params,
    )

    async with browser_session(artifact.replay_config.browser) as session:
        run_log.browser = await session.viewport_report()

        async with _escalation(
            session, artifact.capability_id, enable_escalation
        ) as handoff:
            # Strict mode contacts no model, so it must not hold a client at
            # all -- that is what makes "zero LLM calls" structural rather than
            # a promise a later edit could break.
            engine = ReplayEngine(
                artifact,
                mode=resolved_mode,
                llm=None if resolved_mode is ReplayMode.STRICT else llm,
                escalate=escalate or (handoff.escalate_to_human if handoff else None),
            )
            result = await engine.run(session, params, run_log)

            if handoff:
                for record in handoff.interventions:
                    run_log.record_intervention(_as_intervention_log(record))

    run_log.write(settings.EVIDENCE_DIR / f"{run_log.run_id}.json")
    return result, run_log


@asynccontextmanager
async def _escalation(session: Session, capability_id: str, enabled: bool):
    """Run the operator console for the lifetime of a flow, if asked."""
    if not enabled:
        yield None
        return

    from src.escalation.console import ConsoleServer
    from src.escalation.handoff import SessionHandoffManager

    manager = SessionHandoffManager(session=session, capability_id=capability_id)
    async with ConsoleServer():
        log.info("operator console ready; session %s", manager.session_id)
        yield manager


def _as_intervention_log(record: Any) -> Any:
    from src.evidence.logger import InterventionLog

    return InterventionLog(
        session_id=record.session_id,
        step_id=record.step_id,
        reason=record.reason,
        started_at=record.started_at,
        resumed_at=record.resumed_at,
        duration_s=record.duration_s,
        screenshot_before=record.screenshot_before,
        screenshot_after=record.screenshot_after,
        url_before=record.url_before,
        url_after=record.url_after,
    )
