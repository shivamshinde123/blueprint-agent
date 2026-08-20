"""Structured run logging.

Every discovery and replay run writes one JSON file to ``evidence/``. It is the
artefact a reviewer reads to answer three questions: what happened, which layer
resolved each element, and — for a replay claiming determinism — how many model
calls were made.

Everything written here passes through the redactor first. See PLAN.md §9.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src import settings
from src.artifact.schema import Artifact, ResultType, Step
from src.safety.redaction import Redactor


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_run_id(prefix: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


@dataclass(slots=True)
class StepLog:
    """One executed step."""

    step_id: int
    action: str
    description: str
    timestamp: str
    #: ``None`` for navigate steps, which resolve no element.
    #: ``"accessibility_tree"`` or ``"screenshot"`` otherwise.
    layer_used: str | None = None
    locator_used: dict[str, Any] | None = None
    pre_condition: str = "skipped"
    post_condition: str = "skipped"
    outcome: str = "ok"
    duration_ms: int = 0
    retries: int = 0
    notes: str | None = None


@dataclass(slots=True)
class LLMCallLog:
    """One model call, with the attribution C19 requires."""

    timestamp: str
    purpose: str
    step_id: int | None = None
    model: str | None = None
    #: Which upstream provider actually served it. A gateway may re-route.
    provider: str | None = None
    generation_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


@dataclass(slots=True)
class InterventionLog:
    """One human handoff."""

    session_id: str
    step_id: int
    reason: str
    started_at: str
    resumed_at: str | None = None
    duration_s: float | None = None
    screenshot_before: str | None = None
    screenshot_after: str | None = None
    url_before: str | None = None
    url_after: str | None = None


@dataclass
class RunLog:
    """Accumulates a run, then writes it once at the end."""

    run_id: str
    capability_id: str
    phase: str  # "discovery" | "replay"
    mode: str
    started_at: str = field(default_factory=_now)
    finished_at: str | None = None
    result_type: str | None = None
    target_url: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    steps: list[StepLog] = field(default_factory=list)
    llm_calls: list[LLMCallLog] = field(default_factory=list)
    interventions: list[InterventionLog] = field(default_factory=list)
    outputs: dict[str, Any] | None = None
    failure: dict[str, Any] | None = None
    outcome: dict[str, Any] | None = None
    screenshot: str | None = None
    dom_snapshot: str | None = None
    browser: dict[str, Any] = field(default_factory=dict)

    _redactor: Redactor | None = field(default=None, repr=False, compare=False)

    # -- construction ------------------------------------------------------

    @classmethod
    def start(
        cls,
        *,
        artifact: Artifact,
        phase: str,
        mode: str,
        params: dict[str, Any],
        redactor: Redactor | None = None,
    ) -> RunLog:
        redactor = redactor or Redactor(artifact, params)
        return cls(
            run_id=new_run_id(phase),
            capability_id=artifact.capability_id,
            phase=phase,
            mode=mode,
            target_url=artifact.target.url,
            params=redactor.params(artifact, params),
            _redactor=redactor,
        )

    # -- recording ---------------------------------------------------------

    def record_step(self, entry: StepLog) -> None:
        if self._redactor and entry.locator_used:
            entry.locator_used = self._redactor.mapping(entry.locator_used)
        if self._redactor and entry.notes:
            entry.notes = self._redactor.text(entry.notes)
        self.steps.append(entry)

    def record_llm_call(self, entry: LLMCallLog) -> None:
        self.llm_calls.append(entry)

    def record_intervention(self, entry: InterventionLog) -> None:
        self.interventions.append(entry)

    def finish_success(self, outputs: dict[str, Any]) -> None:
        self.result_type = ResultType.SUCCESS.value
        self.outputs = self._redactor.mapping(outputs) if self._redactor else outputs
        self.finished_at = _now()

    def finish_business_outcome(self, code: str, message: str, value: dict[str, Any]) -> None:
        self.result_type = ResultType.BUSINESS_OUTCOME.value
        self.outcome = {
            "outcome_code": code,
            "outcome_message": message,
            "is_error": False,
        }
        self.outputs = value
        self.finished_at = _now()

    def finish_failure(
        self,
        *,
        failed_at_step: int,
        error_type: str,
        message: str,
        expected: str,
        observed: str,
    ) -> None:
        self.result_type = ResultType.FAILURE.value
        payload = {
            "failure_category": "hard_failure",
            "failed_at_step": failed_at_step,
            "error_type": error_type,
            "message": message,
            "expected": expected,
            "observed": observed,
        }
        self.failure = self._redactor.mapping(payload) if self._redactor else payload
        self.outputs = None
        self.finished_at = _now()

    # -- derived -----------------------------------------------------------

    @property
    def llm_call_count(self) -> int:
        """Strict-mode replay evidence must show this as 0."""
        return len(self.llm_calls)

    @property
    def layer2_used(self) -> bool:
        return any(s.layer_used == "screenshot" for s in self.steps)

    @property
    def duration_ms(self) -> int:
        if not self.finished_at:
            return 0
        start = datetime.fromisoformat(self.started_at)
        end = datetime.fromisoformat(self.finished_at)
        return int((end - start).total_seconds() * 1000)

    # -- output ------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "run_id": self.run_id,
            "capability_id": self.capability_id,
            "phase": self.phase,
            "mode": self.mode,
            "target_url": self.target_url,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": self.duration_ms,
            "result_type": self.result_type,
            "params": self.params,
            "browser": self.browser,
            "steps_completed": len(self.steps),
            "layer2_used": self.layer2_used,
            "llm_calls_made": self.llm_call_count,
            "steps": [asdict(s) for s in self.steps],
            "llm_calls": [asdict(c) for c in self.llm_calls],
            "interventions": [asdict(i) for i in self.interventions],
            "outputs": self.outputs,
            "evidence": {
                "screenshot": self.screenshot,
                "dom_snapshot": self.dom_snapshot,
            },
        }
        if self.outcome:
            data["outcome"] = self.outcome
        if self.failure:
            data["failure"] = self.failure
        return data

    def write(self, path: str | Path | None = None) -> Path:
        """Write the log and return where it landed."""
        settings.ensure_dirs()
        target = Path(path) if path else settings.EVIDENCE_DIR / f"{self.run_id}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return target


def step_log_for(step: Step, **overrides: Any) -> StepLog:
    """Build a :class:`StepLog` pre-filled from the artifact's step."""
    return StepLog(
        step_id=step.step_id,
        action=step.action.value,
        description=step.description,
        timestamp=_now(),
        **overrides,
    )


def append_intervention(record: dict[str, Any], path: Path | None = None) -> Path:
    """Append one intervention record to ``evidence/interventions.json``.

    Kept separate from the run log because the operator console writes it from
    a different process than the one running the flow.
    """
    settings.ensure_dirs()
    target = path or settings.EVIDENCE_DIR / "interventions.json"
    existing: list[Any] = []
    if target.exists():
        try:
            existing = json.loads(target.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = []
    if not isinstance(existing, list):
        existing = [existing]
    existing.append(record)
    target.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return target
