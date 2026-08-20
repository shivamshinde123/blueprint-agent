"""Operator console, and the end-to-end escalation wiring.

The console is deliberately minimal -- co-browsing is out of scope -- so what
is tested here is that the *mechanism* is real: a paused run is visible, Resume
actually releases it, and the whole thing is reachable over HTTP.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from src.artifact.schema import BrowserConfig, ReplayMode, ResultType
from src.escalation.console import ConsoleServer, _render, app
from src.escalation.handoff import SessionHandoffManager, clear_sessions
from src.session.browser import browser_session
from tests import fake_app
from tests.test_browser import needs_chromium
from tests.test_replay_engine import build_artifact

pytestmark = needs_chromium


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    from src import settings

    clear_sessions()
    monkeypatch.setattr(settings, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", tmp_path / "evidence" / "shots")
    monkeypatch.setattr(settings, "ROOT", tmp_path)
    yield
    clear_sessions()


async def _request(method: str, path: str) -> tuple[int, bytes]:
    """Drive the ASGI app directly -- no socket, no extra dependency."""
    body_parts: list[bytes] = []
    status = {"code": 0}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "path": path.split("?", 1)[0],
        "raw_path": path.encode(),
        "query_string": (path.split("?", 1)[1] if "?" in path else "").encode(),
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 8080),
        "scheme": "http",
        "root_path": "",
    }

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            status["code"] = message["status"]
        elif message["type"] == "http.response.body":
            body_parts.append(message.get("body", b""))

    await app(scope, receive, send)
    return status["code"], b"".join(body_parts)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def test_render_escapes_user_supplied_text():
    """A reason string reaches the page; it must not reach it as markup."""
    html = _render(
        {
            "session_id": "abc",
            "capability_id": "cap",
            "awaiting_human": True,
            "step_id": 3,
            "reason": "<script>alert('x')</script>",
            "started_at": "2026-08-20T00:00:00Z",
            "screenshot": None,
            "url": "http://x/",
        }
    )
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_render_shows_nothing_to_do_when_running():
    html = _render(
        {
            "session_id": "abc",
            "capability_id": "cap",
            "awaiting_human": False,
            "step_id": None,
            "reason": None,
            "started_at": None,
            "screenshot": None,
            "url": None,
        }
    )
    assert "not currently waiting" in html


# --------------------------------------------------------------------------
# HTTP surface
# --------------------------------------------------------------------------


async def test_unknown_session_is_a_404():
    status, body = await _request("GET", "/operator?session_id=nope")
    assert status == 404
    assert b"No active handoff" in body


async def test_resume_for_unknown_session_is_a_404():
    status, _ = await _request("POST", "/resume/nope")
    assert status == 404


async def test_resume_on_a_running_session_is_refused():
    """Guards against a stray resume arming the event for a later escalation."""
    async with browser_session(BrowserConfig(headless=True)) as session:
        manager = SessionHandoffManager(session=session, capability_id="cap")
        status, body = await _request("POST", f"/resume/{manager.session_id}")

    assert status == 409
    assert b"not awaiting a human" in body


async def test_paused_session_is_visible_and_resumable():
    async with browser_session(BrowserConfig(headless=True)) as session:
        await session.page.set_content("<h1>stuck here</h1>")
        manager = SessionHandoffManager(session=session, capability_id="lookup_thing")

        task = asyncio.create_task(manager.escalate_to_human("dead end at step 4", 4))
        await asyncio.sleep(0.2)

        status, body = await _request("GET", f"/operator?session_id={manager.session_id}")
        assert status == 200
        assert b"Human intervention required" in body
        assert b"dead end at step 4" in body

        status, body = await _request("GET", f"/operator/status?session_id={manager.session_id}")
        assert json.loads(body)["awaiting_human"] is True

        status, body = await _request("POST", f"/resume/{manager.session_id}")
        assert status == 200
        assert json.loads(body)["status"] == "resumed"

        await asyncio.wait_for(task, timeout=2)
        assert not manager.is_paused


async def test_screenshot_path_traversal_is_blocked():
    status, _ = await _request("GET", "/screenshot/..%2F..%2F.env")
    assert status in (403, 404)


async def test_sessions_listing():
    async with browser_session(BrowserConfig(headless=True)) as session:
        SessionHandoffManager(session=session, capability_id="cap")
        status, body = await _request("GET", "/operator/sessions")
    assert status == 200
    assert len(json.loads(body)["sessions"]) == 1


# --------------------------------------------------------------------------
# The console actually binds
# --------------------------------------------------------------------------


async def test_console_server_starts_and_stops():
    async with ConsoleServer(port=8099) as server:
        assert server._server is not None
        assert server._server.started


# --------------------------------------------------------------------------
# End-to-end wiring through replay()
# --------------------------------------------------------------------------


async def test_replay_wires_escalation_and_records_it(monkeypatch):
    """`--escalate` has to produce a real, resumable pause -- not a flag that
    quietly does nothing."""
    from src.escalation.handoff import get_session_manager
    from src.replay.engine import replay
    from src.safety.guardrails import Allowlist

    artifact = build_artifact()
    artifact.steps[2].risk_level = artifact.steps[2].risk_level.__class__("critical")

    # The engine opens its own browser, so intercept there.
    import src.replay.engine as engine_module

    original = engine_module.browser_session

    def patched(config, **kw):
        ctx = original(config, **kw)

        class Wrapper:
            async def __aenter__(self):
                self.session = await ctx.__aenter__()
                await fake_app.serve(self.session.page)
                return self.session

            async def __aexit__(self, *exc):
                return await ctx.__aexit__(*exc)

        return Wrapper()

    monkeypatch.setattr(engine_module, "browser_session", patched)

    async def resume_shortly():
        for _ in range(60):
            await asyncio.sleep(0.1)
            for ctx in _all_managers():
                if ctx.is_paused:
                    ctx.resume_from_human()
                    return

    def _all_managers():
        from src.escalation.handoff import _SESSIONS

        return list(_SESSIONS.values())

    resumer = asyncio.create_task(resume_shortly())
    result, run_log = await replay(
        artifact,
        {"employee_name": "Peter Anderson"},
        mode=ReplayMode.STRICT,
        allowlist=Allowlist.load(),
        enable_escalation=True,
    )
    resumer.cancel()

    assert result.result_type is ResultType.SUCCESS
    # The pause happened, and it is in the evidence.
    logged = run_log.to_dict()["interventions"]
    assert len(logged) == 1
    assert logged[0]["step_id"] == 3
    assert "authorisation is required" in logged[0]["reason"]
