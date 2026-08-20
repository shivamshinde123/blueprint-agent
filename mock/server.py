"""Serves the local legacy surface.

Run with::

    uv run python -m mock.server

The page needs to be served over http rather than opened as ``file://``: the
allowlist refuses ``file://`` outright (it would expose the local filesystem),
and coordinate behaviour should match how a real site loads.
"""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, PlainTextResponse

from src import settings

HERE = Path(__file__).parent
PAGE = HERE / "legacy_bank.html"

app = FastAPI(title="Blueprint Agent - legacy mock", docs_url=None, redoc_url=None)


@app.get("/mock/", response_class=HTMLResponse)
@app.get("/mock/bank", response_class=HTMLResponse)
def legacy_bank() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))


@app.get("/mock/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


def main() -> None:
    url = f"http://127.0.0.1:{settings.MOCK_PORT}/mock/bank"
    print(f"legacy mock serving at {url}")
    uvicorn.run(app, host="127.0.0.1", port=settings.MOCK_PORT, log_level="warning")


if __name__ == "__main__":
    main()
