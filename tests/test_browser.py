"""Browser session — pinning, and the premise behind the legacy mock.

These launch a real Chromium but touch no network: pages are loaded via
`set_content`. Skipped automatically if the browser binary is not installed,
so the rest of the suite still runs on a bare clone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.artifact.schema import BrowserConfig
from src.artifact.validator import check_browser_matches
from src.session.browser import BrowserError, _context_options, browser_session

MOCK_PAGE = Path(__file__).parent.parent / "mock" / "legacy_bank.html"

MODERN_PAGE = """
<!doctype html><html><body>
  <label for="u">Member ID</label>
  <input id="u" placeholder="Enter member ID">
  <button type="button">Search Members</button>
  <h1>Account Summary</h1>
</body></html>
"""


def _chromium_available() -> bool:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return False
    try:
        with sync_playwright() as pw:
            return Path(pw.chromium.executable_path).exists()
    except Exception:
        return False


needs_chromium = pytest.mark.skipif(
    not _chromium_available(),
    reason="chromium not installed; run: uv run playwright install chromium",
)


# --------------------------------------------------------------------------
# Configuration (no browser needed)
# --------------------------------------------------------------------------


def test_context_options_pin_everything_that_moves_pixels():
    opts = _context_options(BrowserConfig())
    assert opts["viewport"] == {"width": 1280, "height": 720}
    assert opts["device_scale_factor"] == 1
    assert opts["is_mobile"] is False
    assert opts["locale"] == "en-US"
    assert opts["timezone_id"] == "UTC"
    assert opts["reduced_motion"] == "reduce"


def test_recording_options_are_never_enabled():
    """Video and HAR persist raw page content, including credential fields as
    they are typed. Redaction cannot reach inside either (PLAN.md C12)."""
    opts = _context_options(BrowserConfig())
    assert "record_video_dir" not in opts
    assert "record_har_path" not in opts
    assert "record_video_size" not in opts


async def test_handoff_session_refuses_headless():
    """A human cannot take over a browser they cannot see."""
    from src.session.browser import handoff_session

    with pytest.raises(BrowserError, match="headful"):
        async with handoff_session(BrowserConfig(headless=True)):
            pass  # pragma: no cover


# --------------------------------------------------------------------------
# Live browser
# --------------------------------------------------------------------------


@needs_chromium
async def test_session_reports_the_configuration_it_actually_got():
    config = BrowserConfig(headless=True)
    async with browser_session(config) as session:
        report = await session.viewport_report()

    assert report["viewport_width"] == 1280
    assert report["viewport_height"] == 720
    assert report["device_scale_factor"] == 1
    assert report["timezone_id"] == "UTC"
    # The report is measured from the page, so it can be fed straight back
    # into the replay-time match check.
    check_browser_matches(config, report)


@needs_chromium
async def test_screenshot_is_viewport_only():
    """A full-page screenshot's y-coordinates do not map to mouse.click, which
    works in viewport space -- the click would land elsewhere (PLAN.md C1)."""
    tall = "<html><body style='height:5000px'><p>top</p></body></html>"
    config = BrowserConfig(headless=True)
    async with browser_session(config) as session:
        await session.page.set_content(tall)
        png = await session.screenshot()

    # PNG dimensions live at bytes 16..24 of the IHDR chunk.
    height = int.from_bytes(png[20:24], "big")
    assert height == 720, "screenshot must be viewport-height, not page-height"


@needs_chromium
async def test_scroll_offset_can_be_restored():
    config = BrowserConfig(headless=True)
    async with browser_session(config) as session:
        await session.page.set_content("<html><body style='height:5000px'></body></html>")
        await session.scroll_to(1500)
        offset = await session.page.evaluate("window.scrollY")
    assert offset == 1500


# --------------------------------------------------------------------------
# The premise behind the legacy mock (PLAN.md C11)
# --------------------------------------------------------------------------


@needs_chromium
async def test_modern_page_is_resolvable_by_accessibility_tree():
    """Control case: Layer 1 works on a well-marked-up page."""
    async with browser_session(BrowserConfig(headless=True)) as session:
        page = session.page
        await page.set_content(MODERN_PAGE)

        assert await page.get_by_role("button", name="Search Members").count() == 1
        assert await page.get_by_label("Member ID").count() == 1
        assert await page.get_by_placeholder("Enter member ID").count() == 1


@needs_chromium
async def test_legacy_mock_defeats_the_accessibility_tree():
    """The mock only earns its place if Layer 1 genuinely finds nothing.

    If this ever starts passing by finding elements, the mock has drifted into
    being accessible and no longer exercises the screenshot fallback.
    """
    async with browser_session(BrowserConfig(headless=True)) as session:
        page = session.page
        await page.set_content(MOCK_PAGE.read_text(encoding="utf-8"))

        # No real buttons: the controls are <td onclick>, which expose no role.
        assert await page.get_by_role("button").count() == 0
        # Inputs carry no label, no aria-label, and no placeholder.
        assert await page.get_by_label("User ID").count() == 0
        assert await page.get_by_label("Password").count() == 0
        assert await page.get_by_placeholder("User ID").count() == 0
        # Yet the fields are really there, and really typeable.
        assert await page.locator("input[name='uid']").count() == 1


@needs_chromium
async def test_legacy_mock_has_a_real_not_found_state():
    """The business-outcome path needs something genuine to detect."""
    async with browser_session(BrowserConfig(headless=True)) as session:
        page = session.page
        await page.set_content(MOCK_PAGE.read_text(encoding="utf-8"))

        await page.evaluate("show('p2')")
        await page.fill("input[name='acct']", "999999")
        await page.evaluate("doLookup()")
        assert "No Records Found" in await page.inner_text("#err")

        await page.fill("input[name='acct']", "100501")
        await page.evaluate("doLookup()")
        assert "Peter Anderson" in await page.inner_text("#r_name")
        assert "$12,480.55" in await page.inner_text("#r_bal")
