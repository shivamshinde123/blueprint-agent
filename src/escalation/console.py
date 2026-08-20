"""The operator console.

Deliberately minimal. A real-time co-browsing surface is out of scope (the
brief says so explicitly); what has to be real is the **handoff mechanism and
the control-transfer model**, not the UI.

So the operator does not work in this page. They work in the browser window the
automation already opened, which is still sitting at the exact state it
stopped at. This page exists to tell them what happened, show them the screen
as it was, and give them a way to hand control back.
"""

from __future__ import annotations

import asyncio
import html
import logging
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from src import settings
from src.escalation.handoff import active_sessions, get_session_manager

log = logging.getLogger(__name__)

app = FastAPI(title="Blueprint Agent - operator console", docs_url=None, redoc_url=None)


@app.get("/operator", response_class=HTMLResponse)
def operator(session_id: str = "") -> HTMLResponse:
    manager = get_session_manager(session_id) if session_id else None
    if manager is None:
        return HTMLResponse(_no_session(session_id), status_code=404)
    return HTMLResponse(_render(manager.context()))


@app.get("/operator/status")
def status(session_id: str = "") -> JSONResponse:
    manager = get_session_manager(session_id) if session_id else None
    if manager is None:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    return JSONResponse(manager.context())


@app.get("/operator/sessions")
def sessions() -> JSONResponse:
    return JSONResponse({"sessions": active_sessions()})


@app.post("/resume/{session_id}")
async def resume(session_id: str) -> JSONResponse:
    manager = get_session_manager(session_id)
    if manager is None:
        return JSONResponse({"error": "unknown session"}, status_code=404)
    if not manager.is_paused:
        return JSONResponse(
            {"status": "not_paused", "message": "this session is not awaiting a human"},
            status_code=409,
        )

    manager.resume_from_human()
    # Recording what changed needs the browser, which this request cannot
    # await without holding the response open. Hand it to the loop.
    asyncio.create_task(manager.complete_handoff())
    return JSONResponse({"status": "resumed", "session_id": session_id})


@app.get("/screenshot/{name}")
def screenshot(name: str) -> HTMLResponse:
    """Serve a handoff screenshot.

    Path traversal is blocked by resolving and confirming the file really sits
    inside the screenshots directory -- this server is loopback-only, but a
    console that will read any file on disk is not worth shipping either way.
    """
    from fastapi.responses import FileResponse

    settings.ensure_dirs()
    target = (settings.SCREENSHOTS_DIR / name).resolve()
    try:
        target.relative_to(settings.SCREENSHOTS_DIR.resolve())
    except ValueError:
        return HTMLResponse("<h1>403</h1>", status_code=403)
    if not target.exists():
        return HTMLResponse("<h1>404</h1>", status_code=404)
    return FileResponse(target)  # type: ignore[return-value]


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------


def _no_session(session_id: str) -> str:
    return _page(
        "No such session",
        f"<p>No active handoff for session "
        f"<code>{html.escape(session_id) or '(none given)'}</code>.</p>"
        f"<p>It may already have been resumed, or the run may have ended.</p>",
    )


def _render(ctx: dict) -> str:
    if not ctx.get("awaiting_human"):
        return _page(
            "Automation is running",
            "<p>This session is not currently waiting for a human. "
            "Nothing to do.</p>"
            f"<p class='muted'>Session <code>{html.escape(ctx['session_id'])}</code>, "
            f"capability <code>{html.escape(ctx['capability_id'])}</code>.</p>",
        )

    shot = ctx.get("screenshot")
    image = ""
    if shot:
        name = html.escape(Path(shot).name)
        image = (
            f"<h2>The screen when the automation stopped</h2>"
            f"<img src='/screenshot/{name}' alt='page state at handoff'>"
        )

    return _page(
        "Human intervention required",
        f"""
        <div class="card warn">
          <p class="label">Reason</p>
          <p class="reason">{html.escape(str(ctx.get('reason') or ''))}</p>
        </div>

        <table class="facts">
          <tr><td>Capability</td><td><code>{html.escape(ctx['capability_id'])}</code></td></tr>
          <tr><td>Step</td><td>{ctx.get('step_id')}</td></tr>
          <tr><td>Page</td><td><code>{html.escape(str(ctx.get('url') or ''))}</code></td></tr>
          <tr><td>Paused at</td><td>{html.escape(str(ctx.get('started_at') or ''))}</td></tr>
          <tr><td>Session</td><td><code>{html.escape(ctx['session_id'])}</code></td></tr>
        </table>

        <div class="card">
          <h2>What to do</h2>
          <ol>
            <li>Switch to the browser window the automation opened. It is still
                open, at exactly the state shown below &mdash; it was not closed
                or reloaded.</li>
            <li>Complete the step by hand in <em>that</em> window.</li>
            <li>Come back here and click Resume. The automation continues from
                the next step.</li>
          </ol>
          <p class="muted">Everything you do is recorded: timestamps, the URL
             before and after, and screenshots either side.</p>
        </div>

        <button id="resume" onclick="resume()">Resume automation</button>
        <p id="msg" class="muted"></p>

        {image}

        <script>
          async function resume() {{
            const btn = document.getElementById('resume');
            const msg = document.getElementById('msg');
            btn.disabled = true;
            msg.textContent = 'Handing control back...';
            try {{
              const r = await fetch('/resume/{ctx["session_id"]}', {{method: 'POST'}});
              const body = await r.json();
              msg.textContent = r.ok
                ? 'Control returned to the automation. You can close this tab.'
                : ('Could not resume: ' + (body.message || body.error));
              if (!r.ok) btn.disabled = false;
            }} catch (e) {{
              msg.textContent = 'Could not reach the console: ' + e;
              btn.disabled = false;
            }}
          }}
        </script>
        """,
    )


def _page(title: str, body: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>{html.escape(title)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif;
         max-width: 820px; margin: 40px auto; padding: 0 20px; line-height: 1.55; }}
  h1 {{ font-size: 1.5rem; margin-bottom: 1.5rem; }}
  h2 {{ font-size: 1.05rem; margin: 1.4rem 0 .6rem; }}
  .card {{ border: 1px solid #d0d7de; border-radius: 8px; padding: 16px 20px;
          margin: 16px 0; }}
  .card.warn {{ border-color: #d1863c; background: rgba(209,134,60,.08); }}
  .label {{ text-transform: uppercase; font-size: .72rem; letter-spacing: .06em;
           color: #8a6a3a; margin: 0 0 .3rem; }}
  .reason {{ margin: 0; font-size: 1.05rem; }}
  table.facts {{ border-collapse: collapse; margin: 16px 0; width: 100%; }}
  table.facts td {{ padding: 6px 10px; border-bottom: 1px solid #e3e7eb;
                   vertical-align: top; }}
  table.facts td:first-child {{ width: 130px; color: #6a737d; }}
  code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
         font-size: .88em; }}
  button {{ font-size: 1rem; padding: 11px 22px; border-radius: 7px;
           border: 0; background: #1f6feb; color: #fff; cursor: pointer; }}
  button:disabled {{ background: #8b949e; cursor: default; }}
  .muted {{ color: #6a737d; font-size: .9rem; }}
  img {{ max-width: 100%; border: 1px solid #d0d7de; border-radius: 6px;
        margin-top: 8px; }}
  ol {{ padding-left: 1.2rem; }}
</style></head>
<body><h1>{html.escape(title)}</h1>{body}</body></html>"""


# --------------------------------------------------------------------------
# Running it alongside a flow
# --------------------------------------------------------------------------


class ConsoleServer:
    """Runs the console in the background for the lifetime of a run."""

    def __init__(self, host: str | None = None, port: int | None = None) -> None:
        self.host = host or settings.OPERATOR_HOST
        self.port = port or settings.OPERATOR_PORT
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task | None = None

    async def __aenter__(self) -> ConsoleServer:
        config = uvicorn.Config(
            app, host=self.host, port=self.port, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        # Wait for the socket to be accepting before the flow can escalate.
        for _ in range(100):
            if self._server.started:
                break
            await asyncio.sleep(0.05)
        log.info("operator console listening on http://%s:%s", self.host, self.port)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (TimeoutError, asyncio.TimeoutError):  # pragma: no cover
                self._task.cancel()


def main() -> None:  # pragma: no cover - manual use
    uvicorn.run(app, host=settings.OPERATOR_HOST, port=settings.OPERATOR_PORT)


if __name__ == "__main__":  # pragma: no cover
    main()
