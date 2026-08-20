"""The three error categories.

Getting this trichotomy visibly right is the difference between a system that
reports "member not found" as an answer and one that reports it as a crash:

* **Expected business outcome** — "No Records Found". A valid result. Returned
  with ``is_error: false``. Not a failure.
* **Recoverable** — a session timeout, a slow load. Retried per the artifact's
  error map, or recovered and resumed.
* **Hard failure** — the step is genuinely broken. Stop, capture evidence, and
  report *expected* versus *observed*.

Ordering matters as much as classification: business outcomes are checked
**before** anything is treated as a failure. See PLAN.md §6.4.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum

from src.artifact.schema import (
    Artifact,
    ErrorAction,
    ErrorCategory,
    ErrorHandler,
    ErrorTypeKey,
    OnFail,
)

log = logging.getLogger(__name__)


class Disposition(str, Enum):
    """What the engine should do next."""

    RETRY = "retry"
    RE_LOGIN = "re_login"
    ESCALATE = "escalate"
    FAIL = "fail"
    RETURN_OUTCOME = "return_outcome"


@dataclass(slots=True)
class Verdict:
    disposition: Disposition
    error_type: ErrorTypeKey
    attempt: int
    max_retries: int
    wait_ms: int = 0
    capture_screenshot: bool = False
    message: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.disposition in (Disposition.FAIL, Disposition.ESCALATE)


class ReplayFailure(RuntimeError):
    """A hard failure, carrying everything needed to report it."""

    def __init__(
        self,
        *,
        step_id: int,
        error_type: ErrorTypeKey,
        message: str,
        expected: str,
        observed: str,
    ) -> None:
        super().__init__(message)
        self.step_id = step_id
        self.error_type = error_type
        self.message = message
        self.expected = expected
        self.observed = observed

    def as_dict(self) -> dict[str, object]:
        return {
            "failed_at_step": self.step_id,
            "error_type": self.error_type.value,
            "message": self.message,
            "expected": self.expected,
            "observed": self.observed,
        }


class EscalateToHuman(RuntimeError):
    """The engine cannot proceed safely and needs a person."""

    def __init__(self, reason: str, step_id: int) -> None:
        super().__init__(reason)
        self.reason = reason
        self.step_id = step_id


@dataclass
class ErrorTracker:
    """Counts attempts per (step, error type) and decides what happens next."""

    artifact: Artifact
    _attempts: dict[tuple[int, ErrorTypeKey], int] = field(default_factory=dict)

    def attempts_for(self, step_id: int, error_type: ErrorTypeKey) -> int:
        return self._attempts.get((step_id, error_type), 0)

    def reset_step(self, step_id: int) -> None:
        """Clear counters once a step succeeds.

        Without this, a flaky step that recovers on retry would carry its
        history into a later re-entry (after session recovery, say) and
        exhaust its budget having just succeeded.
        """
        for key in [k for k in self._attempts if k[0] == step_id]:
            del self._attempts[key]

    def record(self, step_id: int, error_type: ErrorTypeKey) -> Verdict:
        """Register one occurrence and decide the disposition."""
        key = (step_id, error_type)
        attempt = self._attempts.get(key, 0) + 1
        self._attempts[key] = attempt

        handler = self.artifact.error_handler(step_id, error_type)
        if handler is None:
            # No policy at all. Failing is the safe reading: silently
            # continuing past an unclassified error is how a replay "succeeds"
            # having done the wrong thing.
            return Verdict(
                disposition=Disposition.FAIL,
                error_type=error_type,
                attempt=attempt,
                max_retries=0,
                capture_screenshot=True,
                message=(
                    f"no error_map entry for {error_type.value} at step "
                    f"{step_id}, and no _default. Refusing to continue past an "
                    f"unclassified error."
                ),
            )

        return self._verdict_from(handler, error_type, step_id, attempt)

    def _verdict_from(
        self,
        handler: ErrorHandler,
        error_type: ErrorTypeKey,
        step_id: int,
        attempt: int,
    ) -> Verdict:
        max_retries = (
            handler.max_retries
            if handler.max_retries is not None
            else self.artifact.replay_config.max_retries_per_step
        )
        wait_ms = (
            handler.retry_wait_ms
            if handler.retry_wait_ms is not None
            else self.artifact.replay_config.retry_wait_ms
        )

        if handler.category is ErrorCategory.BUSINESS_OUTCOME:
            return Verdict(
                disposition=Disposition.RETURN_OUTCOME,
                error_type=error_type,
                attempt=attempt,
                max_retries=max_retries,
                message=handler.message or "",
            )

        if handler.category is ErrorCategory.HARD_FAILURE:
            return Verdict(
                disposition=Disposition.FAIL,
                error_type=error_type,
                attempt=attempt,
                max_retries=max_retries,
                capture_screenshot=handler.capture_screenshot,
                message=handler.message or f"hard failure: {error_type.value}",
            )

        # Recoverable.
        if handler.action is ErrorAction.RE_LOGIN and attempt <= max_retries:
            return Verdict(
                disposition=Disposition.RE_LOGIN,
                error_type=error_type,
                attempt=attempt,
                max_retries=max_retries,
                wait_ms=wait_ms,
                message=handler.message or "",
            )

        if attempt <= max_retries:
            return Verdict(
                disposition=Disposition.RETRY,
                error_type=error_type,
                attempt=attempt,
                max_retries=max_retries,
                wait_ms=wait_ms,
                message=handler.message or "",
            )

        # Retries exhausted. Escalation signal 2.
        exhausted = handler.on_exhausted or OnFail.HARD_FAILURE
        disposition = (
            Disposition.ESCALATE
            if exhausted is OnFail.ESCALATE_HUMAN
            else Disposition.FAIL
        )
        return Verdict(
            disposition=disposition,
            error_type=error_type,
            attempt=attempt,
            max_retries=max_retries,
            capture_screenshot=True,
            message=(
                f"{error_type.value} at step {step_id} after {max_retries} "
                f"retries: {handler.message or 'no further detail'}"
            ),
        )


def classify_exception(exc: Exception) -> ErrorTypeKey:
    """Map a raised exception onto an artifact error type."""
    from src.replay.locator import ElementNotFound, LayerBudgetExhausted

    if isinstance(exc, LayerBudgetExhausted):
        return ErrorTypeKey.ELEMENT_NOT_FOUND
    if isinstance(exc, ElementNotFound):
        return ErrorTypeKey.ELEMENT_NOT_FOUND

    name = type(exc).__name__.lower()
    text = str(exc).lower()
    if "timeout" in name or "timeout" in text:
        return ErrorTypeKey.TIMEOUT
    return ErrorTypeKey.WRONG_PAGE_STATE
