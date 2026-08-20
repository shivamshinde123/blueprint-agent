"""Evaluating pre-conditions, post-conditions, and detectors.

Every condition waits on a *state*, never a clock. There is no `sleep` here and
none in the engine: a fixed wait is both slower than necessary when the page is
quick and unreliable when it is not. See PLAN.md §4.3.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.artifact.schema import Condition, ConditionType
from src.replay import locator as loc

if TYPE_CHECKING:  # pragma: no cover
    from src.session.browser import Session

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_MS = 8000
#: How often to re-test a condition while waiting for it.
POLL_INTERVAL_MS = 100


@dataclass(slots=True)
class Evaluation:
    passed: bool
    expected: str
    observed: str

    def __bool__(self) -> bool:  # pragma: no cover - convenience
        return self.passed


async def evaluate(
    session: Session,
    condition: Condition,
    params: dict[str, Any],
    *,
    default_timeout_ms: int = DEFAULT_TIMEOUT_MS,
    extracted: dict[str, Any] | None = None,
) -> Evaluation:
    """Wait for a condition to hold, up to its timeout."""
    timeout_ms = condition.timeout_ms or default_timeout_ms
    deadline = time.monotonic() + timeout_ms / 1000
    expected = describe(condition, params)
    observed = "(not evaluated)"

    while True:
        passed, observed = await _test_once(session, condition, params, extracted)
        if passed:
            return Evaluation(True, expected, observed)
        if time.monotonic() >= deadline:
            return Evaluation(False, expected, observed)
        await _sleep_ms(POLL_INTERVAL_MS)


async def probe(
    session: Session,
    condition: Condition,
    params: dict[str, Any],
    *,
    timeout_ms: int,
) -> bool:
    """Cheap one-shot check.

    Used for interstitial detection, which runs before *every* step. At the
    full action timeout, three interstitials across eight steps would add
    minutes of dead waiting to a flow that should take seconds, so this
    deliberately takes a short budget. See PLAN.md §11 C9.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        passed, _ = await _test_once(session, condition, params, None)
        if passed:
            return True
        if time.monotonic() >= deadline:
            return False
        await _sleep_ms(min(POLL_INTERVAL_MS, timeout_ms))


async def _test_once(
    session: Session,
    condition: Condition,
    params: dict[str, Any],
    extracted: dict[str, Any] | None,
) -> tuple[bool, str]:
    kind = condition.condition
    value = loc.substitute(condition.value, params) or ""

    try:
        if kind is ConditionType.URL_CONTAINS:
            url = session.page.url
            return value in url, f"url is {url!r}"

        if kind is ConditionType.URL_NOT_CONTAINS:
            url = session.page.url
            return value not in url, f"url is {url!r}"

        if kind is ConditionType.PAGE_CONTAINS_TEXT:
            body = await _body_text(session)
            return value.lower() in body.lower(), _excerpt(body, value)

        if kind is ConditionType.ELEMENT_VISIBLE:
            if condition.locators is None:  # pragma: no cover - schema-guarded
                return False, "condition has no locators"
            found = await loc.try_accessibility_chain(
                session.page, condition.locators, params, timeout_ms=250
            )
            return found is not None, (
                "element is visible" if found else "element not found"
            )

        if kind is ConditionType.ELEMENT_HAS_VALUE:
            if condition.locators is None:  # pragma: no cover - schema-guarded
                return False, "condition has no locators"
            found = await loc.try_accessibility_chain(
                session.page, condition.locators, params, timeout_ms=250
            )
            if found is None or found.locator is None:
                return False, "element not found"
            actual = (await found.locator.input_value()) or ""
            # Never echo the actual value: this condition is used on password
            # fields, and `observed` is written to the evidence log.
            return actual == value, (
                "field holds the expected value"
                if actual == value
                else f"field holds a different value ({len(actual)} chars)"
            )

        if kind is ConditionType.ALL_EXTRACTIONS_NON_EMPTY:
            values = extracted or {}
            if not values:
                return False, "nothing was extracted"
            empty = [k for k, v in values.items() if v is None or str(v).strip() == ""]
            return not empty, (
                "all extractions non-empty" if not empty else f"empty: {sorted(empty)}"
            )

    except Exception as exc:
        return False, f"error while evaluating: {exc}"

    return False, f"unsupported condition {kind}"  # pragma: no cover


async def _body_text(session: Session) -> str:
    try:
        return (await session.page.inner_text("body")) or ""
    except Exception:
        try:
            return (await session.page.content()) or ""
        except Exception:  # pragma: no cover
            return ""


def _excerpt(body: str, needle: str, width: int = 120) -> str:
    """A short window of the page, for the failure report."""
    flat = " ".join(body.split())
    if not flat:
        return "page body is empty"
    idx = flat.lower().find(needle.lower()[:20]) if needle else -1
    if idx >= 0:
        start = max(0, idx - width // 2)
        return f"...{flat[start : start + width]}..."
    return f"page begins: {flat[:width]}..."


def describe(condition: Condition, params: dict[str, Any]) -> str:
    """Human-readable statement of what the condition asserts."""
    value = loc.substitute(condition.value, params)
    kind = condition.condition
    if kind is ConditionType.URL_CONTAINS:
        return f"url contains {value!r}"
    if kind is ConditionType.URL_NOT_CONTAINS:
        return f"url does not contain {value!r}"
    if kind is ConditionType.PAGE_CONTAINS_TEXT:
        return f"page contains text {value!r}"
    if kind is ConditionType.ELEMENT_VISIBLE:
        return "target element is visible"
    if kind is ConditionType.ELEMENT_HAS_VALUE:
        return "target field holds the supplied value"
    if kind is ConditionType.ALL_EXTRACTIONS_NON_EMPTY:
        return "every extracted value is non-empty"
    return str(kind)  # pragma: no cover


async def _sleep_ms(ms: int) -> None:
    import asyncio

    await asyncio.sleep(ms / 1000)
