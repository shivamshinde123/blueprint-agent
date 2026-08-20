"""Pinned browser sessions.

Two jobs, both of which the rest of the system depends on being done exactly
once, here:

**Pinning.** Layer 2 stores pixel coordinates, and pixels move for more reasons
than window size. Device pixel ratio scales them. Headless and headful Chromium
rasterize fonts differently and disagree about scrollbar width. Locale changes
text length, which reflows layout. CSS animations mean a screenshot taken
mid-transition disagrees with one taken after. Every one of those is pinned
from a single :class:`~src.artifact.schema.BrowserConfig`, recorded into the
artifact, and re-asserted at replay. See PLAN.md §11 C2.

**Not capturing secrets.** Playwright's tracing, video, and HAR recording all
persist raw page content to disk, including credential fields as they are
typed. Redaction cannot reach inside a video file. So none of them are ever
enabled — this module is the single place that decision is enforced, rather
than a rule someone has to remember at each call site. See PLAN.md §11 C12.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from src.artifact.schema import BrowserConfig

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Browser, BrowserContext, Page

log = logging.getLogger(__name__)

#: Default timeout for Playwright actions on a live page.
ACTION_TIMEOUT_MS = 15_000


class BrowserError(RuntimeError):
    """The browser could not be started in the required configuration."""


@dataclass(slots=True)
class Session:
    """A live browser session, pinned to a recorded configuration."""

    browser: Browser
    context: BrowserContext
    page: Page
    config: BrowserConfig

    async def viewport_report(self) -> dict[str, Any]:
        """What the live browser actually is, for the replay-time match check.

        Read from the page rather than from the config we asked for: the point
        is to detect a browser that did not honour the request.
        """
        metrics = await self.page.evaluate(
            """() => ({
                width: window.innerWidth,
                height: window.innerHeight,
                dpr: window.devicePixelRatio,
                locale: navigator.language,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            })"""
        )
        return {
            "viewport_width": metrics["width"],
            "viewport_height": metrics["height"],
            "device_scale_factor": int(metrics["dpr"]),
            "locale": metrics["locale"],
            "timezone_id": metrics["timezone"],
            "is_mobile": self.config.is_mobile,
            "headless": self.config.headless,
        }

    async def scroll_to(self, scroll_y: int) -> None:
        """Restore the scroll offset stored alongside Layer 2 coordinates.

        Stored coordinates are viewport-relative, so they only address the
        right element at the scroll position they were captured at.
        See PLAN.md §11 C1.
        """
        await self.page.evaluate(f"window.scrollTo(0, {int(scroll_y)})")

    async def screenshot(self) -> bytes:
        """A **viewport-only** PNG.

        Never ``full_page=True``. A full-page screenshot stitches the entire
        scrollable document, so coordinates derived from it do not map to
        ``mouse.click``, which works in viewport space — the click would land
        somewhere else entirely, silently. See PLAN.md §11 C1.
        """
        return await self.page.screenshot(full_page=False)


def _context_options(config: BrowserConfig) -> dict[str, Any]:
    """Everything that must be identical between discovery and replay."""
    return {
        "viewport": {
            "width": config.viewport.width,
            "height": config.viewport.height,
        },
        "device_scale_factor": config.device_scale_factor,
        "is_mobile": config.is_mobile,
        "locale": config.locale,
        "timezone_id": config.timezone_id,
        "reduced_motion": config.reduced_motion,
        # Deliberately absent, and deliberately not configurable:
        #   record_video_dir   - video captures credential fields being typed
        #   record_har_path    - HAR captures request bodies, including logins
        # Redaction cannot reach inside either format, so they are never on.
    }


@asynccontextmanager
async def browser_session(
    config: BrowserConfig | None = None,
    *,
    slow_mo_ms: int = 0,
) -> AsyncIterator[Session]:
    """Open a pinned browser session and guarantee it is closed.

    ``slow_mo_ms`` is a debugging aid for watching a discovery run; it does not
    affect what is recorded.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:  # pragma: no cover
        raise BrowserError(
            "playwright is not installed. Run: uv sync && uv run playwright "
            "install chromium"
        ) from exc

    config = config or BrowserConfig()

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(
                headless=config.headless,
                slow_mo=slow_mo_ms,
            )
        except Exception as exc:  # pragma: no cover - environment dependent
            raise BrowserError(
                f"could not launch Chromium: {exc}. If this is a fresh clone, "
                f"run: uv run playwright install chromium"
            ) from exc

        context = await browser.new_context(**_context_options(config))
        # Playwright actions (click, fill) use this; artifact conditions carry
        # their own timeouts. Deliberately generous: a single-page app completes
        # a click and then leaves a scheduled navigation pending for several
        # seconds, so a tight default fails actions that actually worked.
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        page = await context.new_page()
        session = Session(browser=browser, context=context, page=page, config=config)

        try:
            yield session
        finally:
            # Close in order; a failure closing one must not leak the others.
            for closer in (context.close, browser.close):
                try:
                    await closer()
                except Exception:  # pragma: no cover
                    log.warning("error while closing browser resources", exc_info=True)


@asynccontextmanager
async def handoff_session(
    config: BrowserConfig | None = None,
) -> AsyncIterator[Session]:
    """A session intended to survive a human handoff.

    Identical to :func:`browser_session` except that it is always headful: a
    human cannot take over a browser they cannot see. The escalation contract
    requires the operator to work in *this* window, at the exact state the
    automation left it — never a fresh one. See PLAN.md §8.
    """
    config = config or BrowserConfig()
    if config.headless:
        raise BrowserError(
            "a session that may be handed to a human must be headful; "
            "set browser.headless=false in the artifact's replay_config"
        )
    async with browser_session(config) as session:
        yield session
