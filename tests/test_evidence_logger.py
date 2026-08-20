"""Evidence log.

The log is what a reviewer reads to check the determinism claim, so two
properties matter most: `llm_calls_made` must be truthful, and no secret may
appear anywhere in the written file.
"""

from __future__ import annotations

import json

import pytest

from src.artifact.schema import Artifact
from src.evidence.logger import (
    InterventionLog,
    LLMCallLog,
    RunLog,
    StepLog,
    append_intervention,
    new_run_id,
    step_log_for,
)
from src.safety.redaction import REDACTED

PARAMS = {
    "auth_username": "Admin",
    "auth_password": "hunter2-very-secret",
    "employee_name": "Peter Anderson",
}


@pytest.fixture
def artifact(artifact_dict) -> Artifact:
    return Artifact.model_validate(artifact_dict)


@pytest.fixture
def run(artifact) -> RunLog:
    return RunLog.start(
        artifact=artifact, phase="replay", mode="strict", params=PARAMS
    )


# --------------------------------------------------------------------------
# Basics
# --------------------------------------------------------------------------


def test_run_id_is_unique_and_prefixed():
    a, b = new_run_id("replay"), new_run_id("replay")
    assert a != b
    assert a.startswith("replay-")


def test_params_are_redacted_at_construction(run):
    assert run.params["auth_password"] == REDACTED
    assert run.params["employee_name"] == "Peter Anderson"


def test_step_log_for_prefills_from_the_artifact(artifact):
    entry = step_log_for(artifact.step_by_id(4), layer_used="accessibility_tree")
    assert entry.step_id == 4
    assert entry.action == "click"
    assert entry.layer_used == "accessibility_tree"


# --------------------------------------------------------------------------
# The determinism claim
# --------------------------------------------------------------------------


def test_strict_run_reports_zero_llm_calls(run, artifact):
    for step in artifact.steps:
        run.record_step(step_log_for(step, layer_used="accessibility_tree"))
    run.finish_success({"job_title": "Engineer", "sub_unit": "Engineering"})

    data = run.to_dict()
    assert data["llm_calls_made"] == 0
    assert data["layer2_used"] is False
    assert data["result_type"] == "success"


def test_layer2_usage_is_visible(run, artifact):
    run.record_step(step_log_for(artifact.step_by_id(4), layer_used="screenshot"))
    run.record_llm_call(
        LLMCallLog(
            timestamp="2026-08-20T00:00:00Z",
            purpose="locator_fallback",
            step_id=4,
            model="anthropic/claude-sonnet-5",
            provider="anthropic",
            generation_id="gen-abc123",
            prompt_tokens=900,
            completion_tokens=40,
        )
    )
    data = run.to_dict()
    assert data["layer2_used"] is True
    assert data["llm_calls_made"] == 1
    # C19: which provider actually served it must be attributable.
    assert data["llm_calls"][0]["provider"] == "anthropic"
    assert data["llm_calls"][0]["generation_id"] == "gen-abc123"


def test_navigate_steps_record_no_layer(run, artifact):
    run.record_step(step_log_for(artifact.step_by_id(1)))
    assert run.to_dict()["steps"][0]["layer_used"] is None


# --------------------------------------------------------------------------
# Result types
# --------------------------------------------------------------------------


def test_business_outcome_is_not_an_error(run):
    run.finish_business_outcome(
        "EMPLOYEE_NOT_FOUND",
        "No employee matched the supplied name.",
        {"job_title": None, "sub_unit": None},
    )
    data = run.to_dict()
    assert data["result_type"] == "business_outcome"
    assert data["outcome"]["is_error"] is False
    assert "failure" not in data


def test_failure_records_expected_and_observed(run):
    run.finish_failure(
        failed_at_step=7,
        error_type="checkpoint_failed",
        message="results table never appeared",
        expected="page contains 'Records Found'",
        observed="page still showed the search form",
    )
    data = run.to_dict()
    assert data["result_type"] == "failure"
    assert data["outputs"] is None
    # Both halves are what make a failure debuggable after the fact.
    assert data["failure"]["expected"]
    assert data["failure"]["observed"]
    assert data["failure"]["failed_at_step"] == 7


# --------------------------------------------------------------------------
# Redaction on write
# --------------------------------------------------------------------------


def test_no_secret_reaches_the_written_file(run, artifact, tmp_path):
    run.record_step(
        step_log_for(
            artifact.step_by_id(3),
            layer_used="accessibility_tree",
            notes=f"typed {PARAMS['auth_password']} into the password field",
            locator_used={"name": "{{auth_password}}"},
        )
    )
    run.finish_failure(
        failed_at_step=3,
        error_type="checkpoint_failed",
        message=f"value {PARAMS['auth_password']} was not accepted",
        expected="field populated",
        observed=f"field contained {PARAMS['auth_password']}",
    )

    path = run.write(tmp_path / "run.json")
    written = path.read_text(encoding="utf-8")

    assert PARAMS["auth_password"] not in written
    assert "{{auth_password}}" not in written
    assert REDACTED in written


def test_outputs_are_redacted(run):
    run.finish_success({"job_title": f"leaked {PARAMS['auth_password']}", "sub_unit": "X"})
    assert PARAMS["auth_password"] not in json.dumps(run.to_dict())


# --------------------------------------------------------------------------
# Files
# --------------------------------------------------------------------------


def test_write_creates_parent_directories(run, tmp_path):
    path = run.write(tmp_path / "nested" / "deeper" / "run.json")
    assert path.exists()
    assert json.loads(path.read_text(encoding="utf-8"))["run_id"] == run.run_id


def test_interventions_accumulate(tmp_path):
    target = tmp_path / "interventions.json"
    append_intervention({"session_id": "s1", "step_id": 4}, target)
    append_intervention({"session_id": "s2", "step_id": 7}, target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert [r["session_id"] for r in data] == ["s1", "s2"]


def test_corrupt_intervention_file_does_not_lose_the_new_record(tmp_path):
    target = tmp_path / "interventions.json"
    target.write_text("{ corrupted", encoding="utf-8")
    append_intervention({"session_id": "s1"}, target)
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data[-1]["session_id"] == "s1"


def test_intervention_log_shape():
    entry = InterventionLog(
        session_id="s1",
        step_id=5,
        reason="dead end detected",
        started_at="2026-08-20T00:00:00Z",
        resumed_at="2026-08-20T00:02:30Z",
        duration_s=150.0,
    )
    assert entry.duration_s == 150.0


def test_duration_is_zero_until_finished(run):
    assert run.duration_ms == 0
    run.finish_success({})
    assert run.duration_ms >= 0
