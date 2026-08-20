"""Element resolution: the accessibility chain, then the screenshot fallback.

Used by both phases. Discovery calls it to act on the model's proposal and to
capture a fallback for free; replay calls it to re-find the same element
tomorrow.

The important asymmetry: **Layer 2 in replay is locator resolution, not
decision-making.** The step sequence comes entirely from the artifact either
way — the model is only ever asked "where is this element", never "what should
happen next". That distinction is what lets assisted mode coexist with the
requirement that no model sits in the decision loop. In strict mode the
question is not asked at all. See PLAN.md §11 C10.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.artifact.schema import (
    AccessibilityLocatorMethod,
    AccessibilityMethod,
    Locators,
    ReplayMode,
    ScreenshotLocator,
    Viewport,
)

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Locator, Page

    from src.llm.client import LLMClient
    from src.session.browser import Session

log = logging.getLogger(__name__)


class ElementNotFound(RuntimeError):
    """No layer could resolve the element."""


class LayerBudgetExhausted(RuntimeError):
    """Assisted mode ran out of permitted vision calls."""


@dataclass(slots=True)
class Resolved:
    """A located element, and how it was found."""

    #: "accessibility_tree" or "screenshot".
    layer: str
    #: Present when Layer 1 resolved it. Absent for coordinate clicks.
    locator: Locator | None = None
    #: Present when Layer 2 resolved it, in viewport pixels.
    point: tuple[int, int] | None = None
    #: Which method matched, for the evidence log.
    detail: dict[str, Any] | None = None

    @property
    def by_coordinates(self) -> bool:
        return self.locator is None and self.point is not None


def substitute(text: str | None, params: dict[str, Any]) -> str | None:
    """Replace ``{{param}}`` templates with runtime values.

    Unknown templates are left intact rather than blanked: the artifact
    validator already rejects undeclared parameters, so a template surviving to
    here means something is wrong, and typing literal braces into a field is a
    far more visible failure than silently typing nothing.
    """
    if not text:
        return text
    out = text
    for name, value in params.items():
        if value is not None:
            out = out.replace(f"{{{{{name}}}}}", str(value))
    return out


def build_playwright_locator(
    page: Page, method: AccessibilityLocatorMethod, params: dict[str, Any]
) -> Locator:
    """Turn one artifact locator method into a Playwright locator."""
    name = substitute(method.name, params)
    value = substitute(method.value, params)

    if method.method is AccessibilityMethod.GET_BY_ROLE:
        return page.get_by_role(method.role, name=name, exact=False)  # type: ignore[arg-type]
    if method.method is AccessibilityMethod.GET_BY_LABEL:
        return page.get_by_label(name or value or "", exact=False)
    if method.method is AccessibilityMethod.GET_BY_PLACEHOLDER:
        return page.get_by_placeholder(value or name or "", exact=False)
    if method.method is AccessibilityMethod.GET_BY_TEXT:
        return page.get_by_text(value or name or "", exact=False)
    raise ElementNotFound(f"unsupported locator method: {method.method}")  # pragma: no cover


async def try_accessibility_chain(
    page: Page,
    locators: Locators,
    params: dict[str, Any],
    *,
    timeout_ms: int,
) -> Resolved | None:
    """Try each accessibility method in priority order.

    Returns ``None`` rather than raising: "Layer 1 found nothing" is an
    expected outcome on a legacy surface, not an error.
    """
    if not locators.primary.available:
        return None

    for method in locators.primary.methods:
        try:
            candidate = build_playwright_locator(page, method, params)
            count = await candidate.count()
            if count == 0:
                continue
            if count > 1:
                # Ambiguity is a correctness risk: clicking "a Submit button"
                # when three exist is how the wrong form gets submitted. Take
                # the first only if it is the one that is actually visible.
                visible = [
                    i for i in range(min(count, 10)) if await candidate.nth(i).is_visible()
                ]
                if len(visible) != 1:
                    log.debug(
                        "locator %s matched %d elements (%d visible); skipping",
                        method.method.value,
                        count,
                        len(visible),
                    )
                    continue
                candidate = candidate.nth(visible[0])
            else:
                candidate = candidate.first

            await candidate.wait_for(state="visible", timeout=timeout_ms)
            return Resolved(
                layer="accessibility_tree",
                locator=candidate,
                detail={
                    "method": method.method.value,
                    "role": method.role,
                    "name": substitute(method.name, params),
                    "value": substitute(method.value, params),
                },
            )
        except Exception:
            continue

    return None


async def capture_fallback(
    session: Session, locator: Locator, visual_description: str
) -> ScreenshotLocator | None:
    """Record a Layer 2 fallback for an element Layer 1 already resolved.

    This costs nothing: Playwright is holding the element handle, so its centre
    comes straight from ``bounding_box()`` with no vision call. Recording it for
    every step — not only the ones that needed it — is what gives the artifact a
    complete safety net. See PLAN.md §11 C4.
    """
    try:
        box = await locator.bounding_box()
        if not box:
            return None
        scroll_y = int(await session.page.evaluate("window.scrollY") or 0)
        return ScreenshotLocator(
            coordinates={  # type: ignore[arg-type]
                "x": int(box["x"] + box["width"] / 2),
                "y": int(box["y"] + box["height"] / 2),
            },
            scroll_y=scroll_y,
            viewport=Viewport(
                width=session.config.viewport.width,
                height=session.config.viewport.height,
            ),
            visual_description=visual_description,
        )
    except Exception:  # pragma: no cover - element detached mid-capture
        return None


async def resolve_by_vision(
    session: Session,
    fallback: ScreenshotLocator,
    llm: LLMClient,
    *,
    step_id: int | None = None,
) -> Resolved:
    """Layer 2: ask where the element is, in viewport pixels.

    The stored coordinates are used as a hint via the visual description; the
    model is asked to look at the *current* screen rather than trusting them,
    because a layout shift is exactly the situation this path exists for.
    """
    from src.agent.prompts import VISION_LOCATE_SYSTEM, vision_locate_user_message
    from src.agent.decisions import VisionLocate

    # Restore the scroll offset the coordinates were captured at, so a stored
    # point and a fresh screenshot describe the same frame (PLAN.md C1).
    await session.scroll_to(fallback.scroll_y)
    png = await session.screenshot()

    decision = llm.decide(
        system=VISION_LOCATE_SYSTEM,
        user=vision_locate_user_message(
            visual_description=fallback.visual_description,
            viewport_width=session.config.viewport.width,
            viewport_height=session.config.viewport.height,
        ),
        schema=VisionLocate,
        image_png=png,
    ).value

    if not decision.found:
        raise ElementNotFound(
            f"vision fallback could not see {fallback.visual_description!r} "
            f"on screen: {decision.reasoning or 'no reason given'}"
        )

    point = _clamp_to_viewport(
        decision.x,
        decision.y,
        session.config.viewport.width,
        session.config.viewport.height,
    )
    return Resolved(
        layer="screenshot",
        point=point,
        detail={
            "visual_description": fallback.visual_description,
            "coordinates": {"x": point[0], "y": point[1]},
            "confidence": decision.confidence,
            "step_id": step_id,
        },
    )


def _clamp_to_viewport(x: int, y: int, width: int, height: int) -> tuple[int, int]:
    """Keep a returned point inside the frame.

    A coordinate outside the viewport cannot be clicked; clamping turns a
    hallucinated point into a click that lands somewhere harmless and fails the
    checkpoint, instead of a Playwright error that reads as a crash.
    """
    return (max(0, min(x, width - 1)), max(0, min(y, height - 1)))


async def resolve(
    session: Session,
    locators: Locators,
    params: dict[str, Any],
    *,
    mode: ReplayMode,
    timeout_ms: int,
    fragile: bool = False,
    llm: LLMClient | None = None,
    budget_remaining: int = 0,
    step_id: int | None = None,
) -> Resolved:
    """Resolve an element through the full chain.

    Order: fragile steps skip Layer 1 entirely (discovery already proved it
    useless there, so trying costs a timeout for nothing); otherwise the
    accessibility chain runs first, and Layer 2 is reached only if it finds
    nothing *and* the mode permits it.
    """
    if not fragile:
        found = await try_accessibility_chain(
            session.page, locators, params, timeout_ms=timeout_ms
        )
        if found:
            return found

    fallback = locators.fallback
    if fallback is None:
        raise ElementNotFound(
            "accessibility chain found nothing and the step has no screenshot "
            "fallback recorded"
        )

    if mode is ReplayMode.STRICT:
        raise ElementNotFound(
            "accessibility chain found nothing. Replay is in strict mode, which "
            "makes no model calls, so this escalates to a human rather than "
            "guessing at the element's position."
        )

    if llm is None:
        raise ElementNotFound(
            "assisted mode requires a model client, but none was supplied"
        )
    if budget_remaining <= 0:
        raise LayerBudgetExhausted(
            "assisted mode has used its whole vision-call budget for this run"
        )

    return await resolve_by_vision(session, fallback, llm, step_id=step_id)


# --------------------------------------------------------------------------
# Acting on a resolved element
# --------------------------------------------------------------------------


async def click(session: Session, resolved: Resolved) -> None:
    if resolved.locator is not None:
        await resolved.locator.click()
    elif resolved.point is not None:
        await session.page.mouse.click(*resolved.point)
    else:  # pragma: no cover
        raise ElementNotFound("resolved element carries neither a locator nor a point")


async def fill(session: Session, resolved: Resolved, value: str) -> None:
    if resolved.locator is not None:
        await resolved.locator.fill(value)
        return
    if resolved.point is None:  # pragma: no cover
        raise ElementNotFound("resolved element carries neither a locator nor a point")
    # Coordinate path: focus the field, clear it, then type. `fill` is not
    # available without an element handle.
    await session.page.mouse.click(*resolved.point)
    await session.page.keyboard.press("Control+A")
    await session.page.keyboard.press("Delete")
    await session.page.keyboard.type(value)


async def read_value(session: Session, resolved: Resolved, method: str) -> str:
    """Read text out of a resolved element."""
    if resolved.locator is None:
        raise ElementNotFound(
            "extraction requires an element handle; a coordinate-only match "
            "cannot be read from"
        )
    if method == "get_value":
        return (await resolved.locator.input_value()) or ""
    if method == "text_content":
        return (await resolved.locator.text_content()) or ""
    return (await resolved.locator.inner_text()) or ""
