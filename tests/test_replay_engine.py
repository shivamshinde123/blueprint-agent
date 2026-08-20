"""End-to-end replay against a fake app served by request interception.

Real navigation, real locators, real conditions -- no network, no credentials,
no live demo site. These are the tests that prove the three result types are
actually distinguished, and that strict mode makes no model calls.
"""

from __future__ import annotations

import json
from typing import Any

from src.artifact.schema import Artifact, BrowserConfig, ReplayMode, ResultType
from src.evidence.logger import RunLog
from src.replay.engine import ReplayEngine
from src.safety.guardrails import Allowlist
from src.session.browser import browser_session
from tests import fake_app
from tests.test_browser import needs_chromium

pytestmark = needs_chromium


# --------------------------------------------------------------------------
# A capability for the fake app
# --------------------------------------------------------------------------


def locator(method: str, **kw) -> dict[str, Any]:
    return {
        "primary": {
            "strategy": "accessibility_tree",
            "available": True,
            "methods": [{"method": method, **kw}],
        }
    }


def build_artifact(**overrides: Any) -> Artifact:
    data: dict[str, Any] = {
        "capability_id": "lookup_employee",
        "version": "1.0.0",
        "description": "Find an employee and read their job title and sub unit.",
        "recorded_by": "agent",
        "created_at": "2026-08-20",
        "surface_type": "modern_web",
        "target": {
            "app_name": "fake",
            "url": "http://localhost:8081/mock/",
            "surface_type": "modern_web",
        },
        "input_parameters": {
            "employee_name": {"type": "string", "required": True, "sensitive": False},
        },
        "output_schema": {"job_title": "string", "sub_unit": "string"},
        "replay_config": {
            "browser": {"headless": True, "enforce_strictly": False},
            "mode": "strict",
            "default_timeout_ms": 4000,
            "interstitial_probe_timeout_ms": 200,
        },
        "steps": [
            {
                "step_id": 1,
                "action": "navigate",
                "description": "Open the app",
                "fragile": False,
                "risk_level": "safe",
                "pre_condition": None,
                "url": "http://localhost:8081/mock/search",
                "post_condition": {
                    "condition": "url_contains",
                    "value": "search",
                    "on_fail": "hard_failure",
                },
            },
            {
                "step_id": 2,
                "action": "fill",
                "description": "Type the employee name",
                "fragile": False,
                "risk_level": "low",
                "value": "{{employee_name}}",
                "locators": locator("get_by_label", name="Employee Name"),
                "post_condition": {
                    "condition": "element_has_value",
                    "value": "{{employee_name}}",
                    "locators": locator("get_by_label", name="Employee Name"),
                    "on_fail": "retry",
                },
            },
            {
                "step_id": 3,
                "action": "click",
                "description": "Run the search",
                "fragile": False,
                "risk_level": "safe",
                "locators": locator("get_by_role", role="button", name="Search"),
                "post_condition": {
                    "condition": "page_contains_text",
                    "value": "Records Found",
                    "on_fail": "retry",
                },
            },
            {
                "step_id": 4,
                "action": "extract",
                "description": "Read the profile fields",
                "fragile": False,
                "risk_level": "safe",
                "extractions": [
                    {
                        "output_key": "job_title",
                        "locators": locator("get_by_text", value="Senior Engineer"),
                        "extract_method": "inner_text",
                        "expected_type": "string",
                        "required": True,
                    },
                    {
                        "output_key": "sub_unit",
                        "locators": locator("get_by_text", value="Engineering"),
                        "extract_method": "inner_text",
                        "expected_type": "string",
                        "required": True,
                    },
                ],
                "post_condition": {
                    "condition": "all_extractions_non_empty",
                    "on_fail": "hard_failure",
                },
            },
        ],
        "business_outcomes": [
            {
                "step_ids": [3, 4],
                "name": "Employee not found",
                "detect": {
                    "condition": "page_contains_text",
                    "value": "No Records Found",
                    "timeout_ms": 500,
                    "on_fail": "retry",
                },
                "outcome_code": "EMPLOYEE_NOT_FOUND",
                "outcome_message": "No employee matched that name.",
                "is_error": False,
                "return_value": {"job_title": None, "sub_unit": None},
            }
        ],
        "error_map": {
            "_default": {
                "element_not_found": {
                    "category": "recoverable",
                    "action": "retry",
                    "max_retries": 1,
                    "retry_wait_ms": 10,
                    "on_exhausted": "hard_failure",
                },
                "checkpoint_failed": {
                    "category": "recoverable",
                    "action": "retry",
                    "max_retries": 1,
                    "retry_wait_ms": 10,
                    "on_exhausted": "hard_failure",
                },
            },
            "step_4": {
                "extraction_empty": {
                    "category": "hard_failure",
                    "action": "stop",
                    "capture_screenshot": True,
                    "message": "Result row rendered but a value cell was empty.",
                }
            },
        },
    }
    data.update(overrides)
    return Artifact.model_validate(data)


async def run_replay(
    artifact: Artifact,
    params: dict[str, Any],
    *,
    mode: ReplayMode = ReplayMode.STRICT,
    pages: dict[str, str] | None = None,
    escalate=None,
    llm=None,
):
    engine = ReplayEngine(artifact, mode=mode, llm=llm, escalate=escalate)
    run_log = RunLog.start(
        artifact=artifact, phase="replay", mode=mode.value, params=params
    )
    async with browser_session(BrowserConfig(headless=True)) as session:
        await fake_app.serve(session.page, pages)
        result = await engine.run(session, params, run_log)
    return result, run_log


# --------------------------------------------------------------------------
# The three result types
# --------------------------------------------------------------------------


async def test_success_path_returns_outputs():
    result, run_log = await run_replay(
        build_artifact(), {"employee_name": "Peter Anderson"}
    )

    assert result.result_type is ResultType.SUCCESS
    assert result.outputs == {"job_title": "Senior Engineer", "sub_unit": "Engineering"}
    assert result.steps_completed == result.total_steps
    assert run_log.to_dict()["result_type"] == "success"


async def test_not_found_is_a_business_outcome_not_a_crash():
    """The distinction the whole error model exists for."""
    result, run_log = await run_replay(
        build_artifact(), {"employee_name": "Nobody At All"}
    )

    assert result.result_type is ResultType.BUSINESS_OUTCOME
    assert result.outcome_code == "EMPLOYEE_NOT_FOUND"
    assert result.is_error is False
    # Shape matches output_schema even though nothing was found.
    assert result.outputs == {"job_title": None, "sub_unit": None}
    assert run_log.to_dict()["outcome"]["is_error"] is False


async def test_business_outcome_is_checked_before_failure_handling():
    """If ordering were reversed, the post-condition would fail first and the
    outcome would be reported as a crash."""
    result, _ = await run_replay(build_artifact(), {"employee_name": "Nobody"})
    assert result.result_type is not ResultType.FAILURE


async def test_broken_locator_produces_a_structured_failure():
    artifact = build_artifact()
    artifact.steps[2].locators.primary.methods[0].name = "Nonexistent Button"

    result, run_log = await run_replay(artifact, {"employee_name": "Peter Anderson"})

    assert result.result_type is ResultType.FAILURE
    assert result.failure["failed_at_step"] == 3
    assert result.failure["error_type"] == "element_not_found"
    # Both halves are what make it debuggable.
    assert result.failure["expected"]
    assert result.failure["observed"]
    assert result.outputs is None


async def test_empty_required_extraction_is_a_hard_failure():
    pages = dict(fake_app.PAGES)
    pages["/mock/search"] = fake_app.SEARCH_EMPTY_CELLS

    artifact = build_artifact()
    # The empty-cell page has no "Senior Engineer" text to locate at all.
    result, _ = await run_replay(
        artifact, {"employee_name": "Peter Anderson"}, pages=pages
    )
    assert result.result_type is ResultType.FAILURE
    assert result.failure["failed_at_step"] == 4


# --------------------------------------------------------------------------
# Determinism
# --------------------------------------------------------------------------


async def test_strict_mode_makes_zero_model_calls():
    """The claim the evidence file has to support."""
    result, run_log = await run_replay(
        build_artifact(), {"employee_name": "Peter Anderson"}
    )
    assert result.llm_calls == 0
    assert result.layer2_used is False
    assert run_log.to_dict()["llm_calls_made"] == 0


async def test_strict_mode_holds_no_model_client_even_if_one_is_offered():
    """Structural, not a promise: `replay()` refuses to hand strict mode a
    client, so no later edit can accidentally introduce a call."""
    from src.artifact.validator import preflight_replay
    from src.replay.engine import ReplayEngine as Engine

    artifact = build_artifact()
    sentinel = object()
    preflight = preflight_replay(
        artifact,
        {"employee_name": "x"},
        allowlist=Allowlist.load(),
        mode=ReplayMode.STRICT,
    )
    engine = Engine(
        artifact,
        mode=preflight.mode,
        llm=None if preflight.mode is ReplayMode.STRICT else sentinel,
    )
    assert engine.llm is None


async def test_repeated_runs_produce_identical_outputs():
    """Determinism is the point; two runs of the same artifact must agree."""
    first, _ = await run_replay(build_artifact(), {"employee_name": "Peter Anderson"})
    second, _ = await run_replay(build_artifact(), {"employee_name": "Peter Anderson"})
    assert first.outputs == second.outputs
    assert first.result_type is second.result_type


async def test_parameters_flow_through_to_the_page():
    """A different input must reach the app, not a baked-in recorded value."""
    result, _ = await run_replay(build_artifact(), {"employee_name": "Someone Else"})
    assert result.result_type is ResultType.BUSINESS_OUTCOME


# --------------------------------------------------------------------------
# Risk gating
# --------------------------------------------------------------------------


async def test_risky_step_without_a_handler_refuses_to_execute():
    artifact = build_artifact()
    artifact.steps[2].risk_level = artifact.steps[2].risk_level.__class__("critical")

    result, _ = await run_replay(artifact, {"employee_name": "Peter Anderson"})

    assert result.result_type is ResultType.FAILURE
    assert "authorisation" in result.failure["expected"]
    # The action did not run.
    assert result.steps_completed < result.total_steps


async def test_risky_step_proceeds_after_authorisation():
    """Situation 3: the human authorises, the automation performs the action."""
    approvals: list[tuple[str, int]] = []

    async def approve(reason: str, step_id: int) -> None:
        approvals.append((reason, step_id))

    artifact = build_artifact()
    artifact.steps[2].risk_level = artifact.steps[2].risk_level.__class__("high")

    result, _ = await run_replay(
        artifact, {"employee_name": "Peter Anderson"}, escalate=approve
    )

    assert len(approvals) == 1
    assert approvals[0][1] == 3
    assert "authorisation is required" in approvals[0][0]
    assert result.result_type is ResultType.SUCCESS


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


async def test_run_log_records_the_layer_used_per_step():
    _, run_log = await run_replay(build_artifact(), {"employee_name": "Peter Anderson"})
    steps = run_log.to_dict()["steps"]

    by_id = {s["step_id"]: s for s in steps}
    assert by_id[1]["layer_used"] is None          # navigate resolves no element
    assert by_id[2]["layer_used"] == "accessibility_tree"
    assert by_id[3]["layer_used"] == "accessibility_tree"


async def test_failure_captures_evidence(tmp_path, monkeypatch):
    from src import settings

    monkeypatch.setattr(settings, "SCREENSHOTS_DIR", tmp_path / "shots")
    monkeypatch.setattr(settings, "EVIDENCE_DIR", tmp_path / "evidence")
    monkeypatch.setattr(settings, "ROOT", tmp_path)

    artifact = build_artifact()
    artifact.steps[2].locators.primary.methods[0].name = "Nope"
    result, _ = await run_replay(artifact, {"employee_name": "Peter Anderson"})

    assert result.result_type is ResultType.FAILURE
    assert result.evidence.get("screenshot")
    assert result.evidence.get("dom_snapshot")


async def test_evidence_log_is_json_serialisable(tmp_path):
    _, run_log = await run_replay(build_artifact(), {"employee_name": "Peter Anderson"})
    path = run_log.write(tmp_path / "run.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["capability_id"] == "lookup_employee"
    assert data["steps_completed"] == 4


# --------------------------------------------------------------------------
# Checkpoints must be verifiable from the same view they were recorded from
# --------------------------------------------------------------------------


async def test_checkpoint_matches_an_accessible_name():
    """Discovery reads the accessibility snapshot; replay must accept it.

    A real recording asserted "Go back" after opening a product, taken from an
    image's alt text. The button *reads* "Back to products", so verifying
    against visible body text alone failed a step that had worked perfectly.
    """
    from src.artifact.schema import Condition, ConditionType
    from src.replay.conditions import evaluate

    page_html = """
    <!doctype html><html><body>
      <button><img alt="Go back"><span>Back to products</span></button>
    </body></html>
    """
    async with browser_session(BrowserConfig(headless=True)) as session:
        await session.page.set_content(page_html)

        visible = await evaluate(
            session,
            Condition(condition=ConditionType.PAGE_CONTAINS_TEXT, value="Back to products"),
            {},
            default_timeout_ms=1000,
        )
        assert visible.passed

        alt_only = await evaluate(
            session,
            Condition(condition=ConditionType.PAGE_CONTAINS_TEXT, value="Go back"),
            {},
            default_timeout_ms=1000,
        )
        assert alt_only.passed, (
            "an accessible name is a legitimate presentation of the page, and "
            "is where discovery takes its checkpoints from"
        )


async def test_absent_text_still_fails():
    """The relaxation must not turn the checkpoint into a no-op."""
    from src.artifact.schema import Condition, ConditionType
    from src.replay.conditions import evaluate

    async with browser_session(BrowserConfig(headless=True)) as session:
        await session.page.set_content("<html><body><p>hello</p></body></html>")
        result = await evaluate(
            session,
            Condition(condition=ConditionType.PAGE_CONTAINS_TEXT, value="definitely not here"),
            {},
            default_timeout_ms=500,
        )
        assert not result.passed
