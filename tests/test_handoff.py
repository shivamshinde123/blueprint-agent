"""Human handoff: control transfer, blocking, and recording.

The test that matters most is `test_second_escalation_also_blocks`. An
asyncio.Event stays set once set, so a manager that clears it anywhere other
than at the *start* of escalate_to_human will hand the second escalation
straight back to the automation — no human involved — while the log still
records a handoff. That is a silent correctness failure, and it is the exact
bug the module is written to avoid.
"""

from __future__ import annotations

import asyncio

import pytest

from src.artifact.schema import BrowserConfig
from src.escalation.handoff import (
    Control,
    SessionHandoffManager,
    active_sessions,
    clear_sessions,
    get_session_context,
    get_session_manager,
)
from src.session.browser import browser_session
from tests.test_browser import needs_chromium

pytestmark = needs_chromium


@pytest.fixture(autouse=True)
def isolate_registry():
    clear_sessions()
    yield
    clear_sessions()


@pytest.fixture(autouse=True)
def redirect_evidence(tmp_path, monkeypatch):
    """Keep screenshots and interventions.json out of the real evidence dir."""
    from src import settings

    monkeypatch.setattr(settings, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", tmp_path / "evidence" / "shots")
    monkeypatch.setattr(settings, "ROOT", tmp_path)


async def _session():
    return browser_session(BrowserConfig(headless=True))


# --------------------------------------------------------------------------
# Control transfer
# --------------------------------------------------------------------------


async def test_automation_holds_control_initially():
    async with await _session() as session:
        manager = SessionHandoffManager(session=session, capability_id="c")
        assert manager.control is Control.AUTOMATION
        assert not manager.is_paused


async def test_escalation_blocks_until_resume():
    async with await _session() as session:
        await session.page.set_content("<h1>step</h1>")
        manager = SessionHandoffManager(session=session, capability_id="c")

        task = asyncio.create_task(manager.escalate_to_human("stuck at step 5", 5))
        await asyncio.sleep(0.2)

        # Still blocked; control is with the human.
        assert not task.done()
        assert manager.control is Control.HUMAN
        assert manager.is_paused

        manager.resume_from_human()
        await asyncio.wait_for(task, timeout=2)

        assert manager.control is Control.AUTOMATION
        assert not manager.is_paused


async def test_second_escalation_also_blocks():
    """Two handoffs in one run must each wait for a person."""
    async with await _session() as session:
        await session.page.set_content("<h1>step</h1>")
        manager = SessionHandoffManager(session=session, capability_id="c")

        first = asyncio.create_task(manager.escalate_to_human("first", 3))
        await asyncio.sleep(0.15)
        manager.resume_from_human()
        await asyncio.wait_for(first, timeout=2)

        second = asyncio.create_task(manager.escalate_to_human("second", 7))
        await asyncio.sleep(0.3)

        assert not second.done(), (
            "the second escalation returned without waiting for a human"
        )
        assert manager.control is Control.HUMAN

        manager.resume_from_human()
        await asyncio.wait_for(second, timeout=2)


async def test_a_stray_resume_does_not_release_a_later_escalation():
    """The load-bearing test.

    An asyncio.Event stays set until something clears it. A resume that arrives
    while the automation is *not* paused -- an operator double-clicking Resume,
    a duplicated request, a resume for a step that has not escalated yet --
    leaves the event set. The next genuine escalation then returns instantly:
    the automation proceeds with nobody having looked at the screen, and the
    evidence log still records a handoff.

    Clearing the event immediately before the await is the only placement that
    survives this. Clearing it *after* the await is not enough, because the
    stray set happened while nothing was waiting.
    """
    async with await _session() as session:
        await session.page.set_content("<h1>step</h1>")
        manager = SessionHandoffManager(session=session, capability_id="c")

        # A resume nobody asked for, while the automation holds control.
        manager.resume_from_human()
        assert manager.control is Control.AUTOMATION

        escalation = asyncio.create_task(manager.escalate_to_human("genuinely stuck", 4))
        await asyncio.sleep(0.3)

        assert not escalation.done(), (
            "a stray resume released a later escalation -- the event was not "
            "cleared before awaiting, so the automation continued without a "
            "human ever seeing the page"
        )
        assert manager.control is Control.HUMAN

        manager.resume_from_human()
        await asyncio.wait_for(escalation, timeout=2)


async def test_both_interventions_are_recorded():
    async with await _session() as session:
        await session.page.set_content("<h1>step</h1>")
        manager = SessionHandoffManager(session=session, capability_id="c")

        for reason, step in (("first", 3), ("second", 7)):
            task = asyncio.create_task(manager.escalate_to_human(reason, step))
            await asyncio.sleep(0.15)
            manager.resume_from_human()
            await asyncio.wait_for(task, timeout=2)

        assert [i.step_id for i in manager.interventions] == [3, 7]
        assert [i.reason for i in manager.interventions] == ["first", "second"]


# --------------------------------------------------------------------------
# The session is preserved
# --------------------------------------------------------------------------


async def test_the_browser_is_never_closed_or_reloaded():
    """The whole requirement: the human works in the same live session."""
    async with await _session() as session:
        await session.page.set_content(
            "<h1>original</h1><input id='x'>"
            "<script>window.__marker = 'set-before-handoff';</script>"
        )
        await session.page.fill("#x", "typed before the handoff")

        manager = SessionHandoffManager(session=session, capability_id="c")
        task = asyncio.create_task(manager.escalate_to_human("stuck", 2))
        await asyncio.sleep(0.15)
        manager.resume_from_human()
        await asyncio.wait_for(task, timeout=2)

        # Page identity, JS state, and form contents all survive.
        assert await session.page.evaluate("window.__marker") == "set-before-handoff"
        assert await session.page.input_value("#x") == "typed before the handoff"
        assert await session.page.inner_text("h1") == "original"


async def test_work_done_by_the_human_is_visible_to_the_automation():
    """Whatever the operator does in the window is simply there afterwards."""
    async with await _session() as session:
        await session.page.set_content("<input id='x'>")
        manager = SessionHandoffManager(session=session, capability_id="c")

        task = asyncio.create_task(manager.escalate_to_human("fill it in", 2))
        await asyncio.sleep(0.1)
        # Stand in for the operator typing in the same window.
        await session.page.fill("#x", "filled by hand")
        manager.resume_from_human()
        await asyncio.wait_for(task, timeout=2)

        assert await session.page.input_value("#x") == "filled by hand"


# --------------------------------------------------------------------------
# Recording
# --------------------------------------------------------------------------


async def test_handoff_records_context_and_evidence():
    async with await _session() as session:
        await session.page.set_content("<h1>before</h1>")
        manager = SessionHandoffManager(session=session, capability_id="lookup")

        task = asyncio.create_task(manager.escalate_to_human("dead end", 4))
        await asyncio.sleep(0.15)

        record = manager.interventions[0]
        assert record.step_id == 4
        assert record.reason == "dead end"
        assert record.screenshot_before
        assert record.url_before is not None
        assert record.operator_url.endswith(manager.session_id)

        manager.resume_from_human()
        await asyncio.wait_for(task, timeout=2)
        await manager.complete_handoff()

        assert record.resumed_at
        assert record.duration_s is not None and record.duration_s >= 0
        assert record.screenshot_after


async def test_interventions_are_appended_to_the_evidence_file(tmp_path):
    import json

    from src import settings

    async with await _session() as session:
        await session.page.set_content("<h1>x</h1>")
        manager = SessionHandoffManager(session=session, capability_id="c")
        task = asyncio.create_task(manager.escalate_to_human("stuck", 1))
        await asyncio.sleep(0.15)
        manager.resume_from_human()
        await asyncio.wait_for(task, timeout=2)

    path = settings.EVIDENCE_DIR / "interventions.json"
    assert path.exists()
    records = json.loads(path.read_text(encoding="utf-8"))
    assert records[0]["reason"] == "stuck"


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


async def test_manager_registers_itself():
    async with await _session() as session:
        manager = SessionHandoffManager(session=session, capability_id="c")
        assert get_session_manager(manager.session_id) is manager
        assert get_session_context(manager.session_id)["capability_id"] == "c"
        assert len(active_sessions()) == 1


async def test_unknown_session_returns_nothing():
    assert get_session_manager("does-not-exist") is None
    assert get_session_context("does-not-exist") is None


async def test_context_reports_whether_a_human_is_awaited():
    async with await _session() as session:
        await session.page.set_content("<h1>x</h1>")
        manager = SessionHandoffManager(session=session, capability_id="c")

        assert manager.context()["awaiting_human"] is False

        task = asyncio.create_task(manager.escalate_to_human("stuck", 2))
        await asyncio.sleep(0.15)
        ctx = manager.context()
        assert ctx["awaiting_human"] is True
        assert ctx["step_id"] == 2
        assert ctx["reason"] == "stuck"

        manager.resume_from_human()
        await asyncio.wait_for(task, timeout=2)
        assert manager.context()["awaiting_human"] is False
