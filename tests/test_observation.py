"""Page observation: snapshots, the sparseness switch, and diffing."""

from __future__ import annotations

from pathlib import Path

from src.agent.observation import (
    SPARSE_INTERACTIVE_THRESHOLD,
    Observation,
    diff,
    distinctive_new_text,
    observe,
)
from src.artifact.schema import BrowserConfig
from src.session.browser import browser_session
from tests.test_browser import MODERN_PAGE, needs_chromium

MOCK_PAGE = Path(__file__).parent.parent / "mock" / "legacy_bank.html"


def make(url: str, snapshot: str) -> Observation:
    from src.agent.observation import NAMED_NODE

    return Observation(
        url=url, title="", snapshot=snapshot, named_nodes=NAMED_NODE.findall(snapshot)
    )


# --------------------------------------------------------------------------
# Sparseness — the Layer 1 / Layer 2 switch
# --------------------------------------------------------------------------


def test_rich_snapshot_is_not_sparse():
    obs = make(
        "http://x/",
        '- textbox "Username"\n- textbox "Password"\n- button "Login"\n- heading "Sign in"',
    )
    assert obs.named_node_count == 4
    assert not obs.is_sparse


def test_empty_snapshot_is_sparse():
    assert make("http://x/", "").is_sparse


def test_unnamed_nodes_do_not_count():
    """A tree full of roles with no accessible names identifies nothing."""
    obs = make("http://x/", "- generic\n- generic\n- table\n- row\n- cell")
    assert obs.named_node_count == 0
    assert obs.is_sparse


def test_named_table_cells_do_not_make_a_page_driveable():
    """The C20 case.

    A table-based page produces plenty of named nodes -- `cell` and `row`
    inherit an accessible name from their text content -- while exposing
    nothing you can click or type into. Counting every named node reports a
    rich tree here, and the screenshot fallback would never fire.
    """
    obs = make(
        "http://x/",
        '- cell "User ID :"\n- cell "Password :"\n- cell "LOGIN"\n'
        '- row "LOGIN"\n- cell "Balance Enquiry"\n- textbox\n- textbox',
    )
    assert obs.named_node_count == 5      # plenty of names...
    assert obs.interactive_count == 0     # ...none of them actionable
    assert obs.is_sparse


def test_interactive_nodes_must_also_be_named():
    """An unnamed textbox cannot be addressed by identity."""
    obs = make("http://x/", "- textbox\n- textbox\n- button")
    assert obs.interactive_count == 0
    assert obs.is_sparse


def test_threshold_boundary():
    below = "\n".join(
        f'- button "b{i}"' for i in range(SPARSE_INTERACTIVE_THRESHOLD - 1)
    )
    at = "\n".join(f'- button "b{i}"' for i in range(SPARSE_INTERACTIVE_THRESHOLD))
    assert make("http://x/", below).is_sparse
    assert not make("http://x/", at).is_sparse


# --------------------------------------------------------------------------
# Fingerprinting — dead-end detection
# --------------------------------------------------------------------------


def test_identical_pages_share_a_fingerprint():
    a = make("http://x/a", '- button "Go"\n- textbox "Name"')
    b = make("http://x/a", '- textbox "Name"\n- button "Go"')  # different order
    assert a.fingerprint == b.fingerprint


def test_url_change_changes_the_fingerprint():
    a = make("http://x/a", '- button "Go"')
    b = make("http://x/b", '- button "Go"')
    assert a.fingerprint != b.fingerprint


def test_cosmetic_churn_does_not_read_as_progress():
    """A spinner frame or a clock tick must not defeat dead-end detection."""
    a = make("http://x/a", '- button "Go"\n- generic')
    b = make("http://x/a", '- button "Go"\n- generic\n- generic')
    assert a.fingerprint == b.fingerprint


def test_new_named_element_does_change_the_fingerprint():
    a = make("http://x/a", '- button "Go"')
    b = make("http://x/a", '- button "Go"\n- heading "Results"')
    assert a.fingerprint != b.fingerprint


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------


def test_diff_detects_appearance_and_disappearance():
    before = make("http://x/a", '- button "Search"\n- textbox "Name"')
    after = make("http://x/a", '- button "Search"\n- heading "Results"')
    change = diff(before, after)

    assert not change.url_changed
    assert ("heading", "Results") in change.appeared
    assert ("textbox", "Name") in change.disappeared
    assert change.anything_changed


def test_diff_reports_no_change():
    before = make("http://x/a", '- button "Search"')
    after = make("http://x/a", '- button "Search"')
    assert not diff(before, after).anything_changed


def test_new_url_segment_picks_the_specific_part():
    change = diff(
        make("https://app/web/index.php/auth/login", ""),
        make("https://app/web/index.php/dashboard/index", ""),
    )
    assert change.url_changed
    assert change.new_url_segment == "dashboard"


def test_new_url_segment_is_none_without_navigation():
    assert diff(make("http://x/a", ""), make("http://x/a", "")).new_url_segment is None


# --------------------------------------------------------------------------
# Checkpoint candidate selection (PLAN.md C5)
# --------------------------------------------------------------------------


def test_text_already_present_is_rejected():
    """A checkpoint that was true before the action verifies nothing."""
    before = make("http://x/a", '- link "Search"\n- button "Go"')
    after = make("http://x/a", '- link "Search"\n- button "Go"\n- generic "Search"')
    assert distinctive_new_text(diff(before, after), before) is None


def test_genuinely_new_text_is_selected():
    before = make("http://x/a", '- button "Go"')
    after = make("http://x/a", '- button "Go"\n- heading "No Records Found"')
    assert distinctive_new_text(diff(before, after), before) == "No Records Found"


def test_stable_text_is_preferred_over_variable_text():
    """Strings containing digits usually embed per-run values that will not
    recur on the next replay."""
    before = make("http://x/a", "")
    after = make(
        "http://x/a", '- heading "Results"\n- cell "Employee 88213"'
    )
    assert distinctive_new_text(diff(before, after), before) == "Results"


def test_very_short_text_is_ignored():
    before = make("http://x/a", "")
    after = make("http://x/a", '- cell "OK"')
    assert distinctive_new_text(diff(before, after), before) is None


# --------------------------------------------------------------------------
# Against real pages
# --------------------------------------------------------------------------


@needs_chromium
async def test_modern_page_produces_a_rich_snapshot():
    async with browser_session(BrowserConfig(headless=True)) as session:
        await session.page.set_content(MODERN_PAGE)
        obs = await observe(session.page)

    assert not obs.is_sparse
    assert "Search Members" in obs.snapshot


@needs_chromium
async def test_legacy_mock_reads_as_sparse():
    """The whole Layer 2 path depends on this being true of the mock."""
    async with browser_session(BrowserConfig(headless=True)) as session:
        await session.page.set_content(MOCK_PAGE.read_text(encoding="utf-8"))
        obs = await observe(session.page)

    assert obs.is_sparse, (
        f"legacy mock exposes {obs.interactive_count} named interactive nodes; "
        f"it has drifted into being accessible and no longer exercises the "
        f"screenshot fallback"
    )
    # Named nodes exist (table cells carry text) -- they are just not actionable.
    assert obs.named_node_count > 0


@needs_chromium
async def test_observation_survives_a_blank_page():
    async with browser_session(BrowserConfig(headless=True)) as session:
        await session.page.set_content("<html><body></body></html>")
        obs = await observe(session.page)
    assert obs.is_sparse
    assert isinstance(obs.snapshot, str)
