"""Human-in-the-loop handoff.

The requirement this satisfies is specific: the human must operate the **same
live session** the automation was using, not a fresh one, and control must be
transferable in both directions.

So the browser is never closed, never refreshed, and never re-navigated across
a handoff. The automation blocks on an ``asyncio.Event``; the operator works in
the same window, at the exact state the automation left it; clicking Resume
sets the event and execution continues from where it stopped.

One implementation detail is load-bearing enough to call out here rather than
bury: **the event is cleared at the start of**
:meth:`SessionHandoffManager.escalate_to_human`, immediately before the
``await``.

An ``asyncio.Event`` stays set until something clears it, and a resume can
arrive when nothing is waiting — an operator double-clicking Resume, a retried
request, a resume for a step that has not escalated. That leaves the event set,
and the next genuine escalation returns instantly: the automation proceeds with
nobody having looked at the screen, while the evidence log still records a
handoff.

Clearing immediately before the await is the only placement that survives this.
Clearing *after* the await is not sufficient, because the stray set happened
while nothing was waiting on it. Both placements are covered by mutation-tested
cases in ``tests/test_handoff.py``. See PLAN.md §8.3.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src import settings
from src.evidence.logger import append_intervention

if TYPE_CHECKING:  # pragma: no cover
    from src.session.browser import Session

log = logging.getLogger(__name__)


class Control(str, Enum):
    """Who holds the session. Exactly one party, always."""

    AUTOMATION = "automation"
    HUMAN = "human"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass
class Intervention:
    """One handoff, from the pause to the resume."""

    session_id: str
    capability_id: str
    step_id: int
    reason: str
    started_at: str
    operator_url: str
    screenshot_before: str | None = None
    url_before: str | None = None
    resumed_at: str | None = None
    duration_s: float | None = None
    screenshot_after: str | None = None
    url_after: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "capability_id": self.capability_id,
            "step_id": self.step_id,
            "reason": self.reason,
            "started_at": self.started_at,
            "resumed_at": self.resumed_at,
            "duration_s": self.duration_s,
            "operator_url": self.operator_url,
            "screenshot_before": self.screenshot_before,
            "screenshot_after": self.screenshot_after,
            "url_before": self.url_before,
            "url_after": self.url_after,
        }


@dataclass
class SessionHandoffManager:
    """Pauses the automation and hands the live session to a person."""

    session: Session
    capability_id: str
    session_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    control: Control = Control.AUTOMATION
    interventions: list[Intervention] = field(default_factory=list)
    _resume: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    _current: Intervention | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        register_session(self.session_id, self)

    # -- the handoff -------------------------------------------------------

    async def escalate_to_human(self, reason: str, step_id: int) -> None:
        """Pause and block until a human signals resume."""
        # Cleared FIRST, before the await. See the module docstring: an event
        # left set from an earlier handoff makes the next await return
        # instantly, and the automation resumes with nobody having looked.
        self._resume.clear()
        self.control = Control.HUMAN

        operator_url = settings.operator_url(self.session_id)
        intervention = Intervention(
            session_id=self.session_id,
            capability_id=self.capability_id,
            step_id=step_id,
            reason=reason,
            started_at=_now(),
            operator_url=operator_url,
            screenshot_before=await self._capture(step_id, "before"),
            url_before=self.session.page.url,
        )
        self._current = intervention
        self.interventions.append(intervention)
        append_intervention(intervention.to_dict())

        _announce(intervention)

        await self._resume.wait()

    def resume_from_human(self) -> None:
        """Called by the operator console. Unblocks the automation."""
        self.control = Control.AUTOMATION
        self._resume.set()

    async def complete_handoff(self) -> None:
        """Record what changed while the human was in control.

        Separate from :meth:`resume_from_human` because that is called from the
        web request handler, which cannot await the browser.
        """
        if self._current is None:
            return
        current = self._current
        current.resumed_at = _now()
        current.url_after = self.session.page.url
        current.screenshot_after = await self._capture(current.step_id, "after")
        try:
            started = datetime.fromisoformat(current.started_at)
            ended = datetime.fromisoformat(current.resumed_at)
            current.duration_s = (ended - started).total_seconds()
        except ValueError:  # pragma: no cover
            current.duration_s = None

        append_intervention(current.to_dict())
        log.info(
            "handoff complete for step %s after %.1fs",
            current.step_id,
            current.duration_s or 0.0,
        )
        self._current = None

    # -- context for the operator -----------------------------------------

    def context(self) -> dict[str, Any]:
        """What the operator console shows."""
        current = self._current
        return {
            "session_id": self.session_id,
            "capability_id": self.capability_id,
            "control": self.control.value,
            "awaiting_human": self.control is Control.HUMAN,
            "step_id": current.step_id if current else None,
            "reason": current.reason if current else None,
            "started_at": current.started_at if current else None,
            "screenshot": current.screenshot_before if current else None,
            "url": current.url_before if current else None,
        }

    @property
    def is_paused(self) -> bool:
        return self.control is Control.HUMAN

    async def _capture(self, step_id: int, label: str) -> str | None:
        try:
            return await capture_screenshot(self.session, step_id, label)
        except Exception:  # pragma: no cover
            log.warning("could not capture handoff screenshot", exc_info=True)
            return None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def capture_screenshot(session: Session, step_id: int, label: str) -> str:
    """Save a viewport screenshot and return its repo-relative path."""
    settings.ensure_dirs()
    name = f"handoff_step{step_id}_{label}_{uuid.uuid4().hex[:6]}.png"
    path: Path = settings.SCREENSHOTS_DIR / name
    path.write_bytes(await session.screenshot())
    try:
        return str(path.relative_to(settings.ROOT))
    except ValueError:  # pragma: no cover - screenshots dir moved in tests
        return str(path)


def _announce(intervention: Intervention) -> None:
    """Tell the operator where to go. Deliberately hard to miss in a terminal."""
    bar = "=" * 68
    print(
        f"\n{bar}\n"
        f"  HUMAN INTERVENTION REQUIRED\n"
        f"{bar}\n"
        f"  capability : {intervention.capability_id}\n"
        f"  step       : {intervention.step_id}\n"
        f"  reason     : {intervention.reason}\n"
        f"  page       : {intervention.url_before}\n"
        f"\n"
        f"  The browser window is still open at the exact state the\n"
        f"  automation left it. Do what is needed in THAT window, then:\n"
        f"\n"
        f"  open  {intervention.operator_url}\n"
        f"  and click Resume.\n"
        f"{bar}\n",
        flush=True,
    )


# --------------------------------------------------------------------------
# Session registry
#
# The operator console runs in the same process but a different task, and only
# knows a session id from the URL. This is how it finds the manager to resume.
# --------------------------------------------------------------------------

_SESSIONS: dict[str, SessionHandoffManager] = {}


def register_session(session_id: str, manager: SessionHandoffManager) -> None:
    _SESSIONS[session_id] = manager


def get_session_manager(session_id: str) -> SessionHandoffManager | None:
    return _SESSIONS.get(session_id)


def get_session_context(session_id: str) -> dict[str, Any] | None:
    manager = _SESSIONS.get(session_id)
    return manager.context() if manager else None


def active_sessions() -> list[dict[str, Any]]:
    return [m.context() for m in _SESSIONS.values()]


def clear_sessions() -> None:
    """Test helper; also used when a run ends."""
    _SESSIONS.clear()
