"""Field-level and step-level schema validation.

Each test drives one invalid artifact through `Artifact.model_validate` and
asserts it is rejected. The point is not coverage for its own sake: replay is
mechanical, so anything not caught here becomes a silent wrong action against a
live banking UI.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.artifact.schema import Artifact
from tests.conftest import step_by_id


def expect_rejected(data: dict[str, Any], *, because: str) -> str:
    """Assert the artifact is rejected and return the error text."""
    with pytest.raises(ValidationError) as exc:
        Artifact.model_validate(data)
    return str(exc.value)


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------


def test_golden_artifact_is_valid(artifact_dict):
    artifact = Artifact.model_validate(artifact_dict)
    assert artifact.capability_id == "lookup_employee_profile"
    assert len(artifact.steps) == 8


def test_unknown_field_is_rejected(artifact_dict):
    # A typo'd field in a permissive schema is a silent no-op: the engine would
    # simply never see the value. extra="forbid" turns that into a load error.
    artifact_dict["capabilty_id"] = "typo"
    assert "capabilty_id" in expect_rejected(artifact_dict, because="unknown field")


# --------------------------------------------------------------------------
# Identity fields
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["Lookup_Employee", "lookup-employee", "lookup employee", "9lookup"]
)
def test_capability_id_must_be_snake_case(artifact_dict, bad):
    artifact_dict["capability_id"] = bad
    assert "snake_case" in expect_rejected(artifact_dict, because="capability_id")


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1", "1.0.0-beta"])
def test_version_must_be_semver(artifact_dict, bad):
    artifact_dict["version"] = bad
    assert "semver" in expect_rejected(artifact_dict, because="version")


@pytest.mark.parametrize("bad", ["20-08-2026", "2026/08/20", "Aug 20 2026"])
def test_created_at_must_be_iso_date(artifact_dict, bad):
    artifact_dict["created_at"] = bad
    assert "YYYY-MM-DD" in expect_rejected(artifact_dict, because="created_at")


# --------------------------------------------------------------------------
# sensitive: strict boolean (PLAN.md C12 depends on this)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("truthy", ["true", "yes", 1, "false", "no", 0])
def test_sensitive_rejects_non_boolean(artifact_dict, truthy):
    """`sensitive: "no"` is *truthy* as a string — coercion here disables
    redaction on a real password. Only JSON booleans are accepted."""
    artifact_dict["input_parameters"]["auth_password"]["sensitive"] = truthy
    assert "boolean" in expect_rejected(artifact_dict, because="sensitive coercion")


def test_sensitive_accepts_real_booleans(artifact_dict):
    artifact_dict["input_parameters"]["auth_password"]["sensitive"] = True
    artifact_dict["input_parameters"]["employee_name"]["sensitive"] = False
    artifact = Artifact.model_validate(artifact_dict)
    assert artifact.sensitive_parameters() == {"auth_username", "auth_password"}


# --------------------------------------------------------------------------
# Step shape by action
# --------------------------------------------------------------------------


def test_navigate_requires_url(artifact_dict):
    del step_by_id(artifact_dict, 1)["url"]
    assert "navigate requires 'url'" in expect_rejected(artifact_dict, because="url")


def test_navigate_rejects_locators(artifact_dict):
    step_by_id(artifact_dict, 1)["locators"] = {
        "primary": {"available": True, "methods": [{"method": "get_by_text", "value": "x"}]}
    }
    assert "navigate takes no 'locators'" in expect_rejected(
        artifact_dict, because="navigate locators"
    )


def test_fill_requires_value(artifact_dict):
    del step_by_id(artifact_dict, 2)["value"]
    assert "fill requires 'value'" in expect_rejected(artifact_dict, because="value")


def test_click_requires_locators(artifact_dict):
    del step_by_id(artifact_dict, 4)["locators"]
    assert "click requires 'locators'" in expect_rejected(
        artifact_dict, because="click locators"
    )


def test_extract_rejects_top_level_locators(artifact_dict):
    """Extract steps carry per-extraction locators, because one step can pull
    several fields from different places on the page."""
    step_by_id(artifact_dict, 8)["locators"] = {
        "primary": {"available": True, "methods": [{"method": "get_by_text", "value": "x"}]}
    }
    assert "must not set top-level" in expect_rejected(
        artifact_dict, because="extract locators"
    )


def test_extract_requires_extractions(artifact_dict):
    step_by_id(artifact_dict, 8)["extractions"] = []
    assert "non-empty" in expect_rejected(artifact_dict, because="extractions")


def test_non_extract_step_rejects_extractions(artifact_dict):
    step_by_id(artifact_dict, 4)["extractions"] = [
        {
            "output_key": "stray",
            "extract_method": "inner_text",
            "expected_type": "string",
            "locators": {
                "primary": {"available": True, "methods": [{"method": "get_by_text", "value": "x"}]}
            },
        }
    ]
    assert "only valid on extract steps" in expect_rejected(
        artifact_dict, because="stray extractions"
    )


def test_value_only_valid_on_fill_steps(artifact_dict):
    step_by_id(artifact_dict, 4)["value"] = "something"
    assert "only valid on fill steps" in expect_rejected(
        artifact_dict, because="stray value"
    )


def test_step_one_cannot_have_pre_condition(artifact_dict):
    """PLAN.md C17: nothing exists to assert against before the first navigate."""
    step_by_id(artifact_dict, 1)["pre_condition"] = {
        "condition": "element_visible",
        "on_fail": "retry",
        "locators": {
            "primary": {"available": True, "methods": [{"method": "get_by_text", "value": "x"}]}
        },
    }
    assert "step 1 cannot have a pre_condition" in expect_rejected(
        artifact_dict, because="step 1 precondition"
    )


def test_post_condition_is_mandatory(artifact_dict):
    del step_by_id(artifact_dict, 4)["post_condition"]
    assert "post_condition" in expect_rejected(artifact_dict, because="missing checkpoint")


# --------------------------------------------------------------------------
# Fragile steps
# --------------------------------------------------------------------------


def test_fragile_requires_reason(artifact_dict):
    step = step_by_id(artifact_dict, 4)
    step["fragile"] = True
    step["locators"]["primary"] = {"available": False, "methods": []}
    assert "requires 'fragile_reason'" in expect_rejected(
        artifact_dict, because="fragile reason"
    )


def test_fragile_reason_without_fragile_flag_is_rejected(artifact_dict):
    step_by_id(artifact_dict, 4)["fragile_reason"] = "leftover from an edit"
    assert "fragile=false" in expect_rejected(artifact_dict, because="stale reason")


def test_fragile_step_must_disable_primary_locator(artifact_dict):
    step = step_by_id(artifact_dict, 4)
    step["fragile"] = True
    step["fragile_reason"] = "legacy surface exposes no accessible names"
    # primary left available=true — replay would waste time on a layer that
    # discovery already proved useless here.
    assert "available=false" in expect_rejected(artifact_dict, because="fragile primary")


def test_fragile_step_requires_a_fallback(artifact_dict):
    step = step_by_id(artifact_dict, 4)
    step["fragile"] = True
    step["fragile_reason"] = "legacy surface exposes no accessible names"
    step["locators"]["primary"] = {"available": False, "methods": []}
    del step["locators"]["fallback"]
    assert "require a screenshot fallback" in expect_rejected(
        artifact_dict, because="fragile without fallback"
    )


# --------------------------------------------------------------------------
# Locators
# --------------------------------------------------------------------------


def test_get_by_role_requires_role_and_name(artifact_dict):
    step_by_id(artifact_dict, 4)["locators"]["primary"]["methods"] = [
        {"method": "get_by_role", "role": "button"}
    ]
    assert "requires 'name'" in expect_rejected(artifact_dict, because="role w/o name")


def test_role_without_name_is_allowed_with_an_explicit_index(artifact_dict):
    """Position is a legitimate choice when the name is this run's data.

    A typeahead suggestion reads "Peter Mac Anderson" for one employee and
    something else for the next, so naming it records a capability that works
    exactly once. `role + nth` -- the first option in the list -- stays correct
    for every input, and is an explicit recorded decision rather than an
    accident of ordering.
    """
    step_by_id(artifact_dict, 4)["locators"]["primary"]["methods"] = [
        {"method": "get_by_role", "role": "option", "nth": 0}
    ]
    artifact = Artifact.model_validate(artifact_dict)
    method = artifact.step_by_id(4).locators.primary.methods[0]
    assert method.name is None
    assert method.nth == 0


def test_available_primary_must_list_methods(artifact_dict):
    step_by_id(artifact_dict, 4)["locators"]["primary"] = {
        "available": True,
        "methods": [],
    }
    assert "lists no methods" in expect_rejected(artifact_dict, because="empty methods")


def test_unavailable_primary_must_not_list_methods(artifact_dict):
    step = step_by_id(artifact_dict, 4)
    step["locators"]["primary"] = {
        "available": False,
        "methods": [{"method": "get_by_role", "role": "button", "name": "Login"}],
    }
    assert "marked unavailable but lists methods" in expect_rejected(
        artifact_dict, because="contradictory availability"
    )


def test_screenshot_fallback_needs_coordinates_or_region(artifact_dict):
    fallback = step_by_id(artifact_dict, 4)["locators"]["fallback"]
    del fallback["coordinates"]
    assert "'coordinates' or 'region'" in expect_rejected(
        artifact_dict, because="empty fallback"
    )


def test_fallback_records_the_viewport_it_was_captured_at(artifact_dict):
    """Coordinates are meaningless without the viewport they belong to."""
    del step_by_id(artifact_dict, 4)["locators"]["fallback"]["viewport"]
    assert "viewport" in expect_rejected(artifact_dict, because="missing viewport")


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------


def test_element_condition_requires_locators(artifact_dict):
    step_by_id(artifact_dict, 2)["pre_condition"] = {
        "condition": "element_visible",
        "on_fail": "retry",
    }
    assert "requires 'locators'" in expect_rejected(
        artifact_dict, because="element condition"
    )


def test_text_condition_requires_value(artifact_dict):
    step_by_id(artifact_dict, 4)["post_condition"] = {
        "condition": "url_contains",
        "on_fail": "retry",
    }
    assert "requires 'value'" in expect_rejected(artifact_dict, because="text condition")


def test_all_extractions_non_empty_takes_no_operands(artifact_dict):
    step_by_id(artifact_dict, 8)["post_condition"] = {
        "condition": "all_extractions_non_empty",
        "value": "unexpected",
        "on_fail": "hard_failure",
    }
    assert "takes neither" in expect_rejected(artifact_dict, because="stray operand")


# --------------------------------------------------------------------------
# Extractions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Job Title", "job-title", "jobTitle"])
def test_output_key_must_be_snake_case(artifact_dict, bad):
    step_by_id(artifact_dict, 8)["extractions"][0]["output_key"] = bad
    assert "snake_case" in expect_rejected(artifact_dict, because="output_key")


# --------------------------------------------------------------------------
# Error map
# --------------------------------------------------------------------------


def test_recoverable_handler_requires_on_exhausted(artifact_dict):
    del artifact_dict["error_map"]["_default"]["element_not_found"]["on_exhausted"]
    assert "require 'on_exhausted'" in expect_rejected(
        artifact_dict, because="no exhaustion policy"
    )


def test_hard_failure_handler_must_capture_screenshot(artifact_dict):
    """A hard failure with no screenshot is not debuggable after the fact."""
    artifact_dict["error_map"]["step_8"]["extraction_empty"]["capture_screenshot"] = False
    assert "capture_screenshot=true" in expect_rejected(
        artifact_dict, because="no evidence"
    )


def test_custom_error_type_keys_are_rejected(artifact_dict):
    artifact_dict["error_map"]["_default"]["totally_made_up"] = {
        "category": "recoverable",
        "action": "retry",
        "on_exhausted": "hard_failure",
    }
    assert "totally_made_up" in expect_rejected(artifact_dict, because="invented key")


# --------------------------------------------------------------------------
# Business outcomes
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["employee_not_found", "EmployeeNotFound", "EMPLOYEE-NOT-FOUND"])
def test_outcome_code_must_be_screaming_snake(artifact_dict, bad):
    artifact_dict["business_outcomes"][0]["outcome_code"] = bad
    assert "SCREAMING_SNAKE_CASE" in expect_rejected(artifact_dict, because="outcome_code")


def test_business_outcome_cannot_be_an_error(artifact_dict):
    """"No Records Found" is an answer, not a crash."""
    artifact_dict["business_outcomes"][0]["is_error"] = True
    assert "is_error" in expect_rejected(artifact_dict, because="is_error true")


def test_business_outcome_needs_at_least_one_step(artifact_dict):
    artifact_dict["business_outcomes"][0]["step_ids"] = []
    assert "at least one step" in expect_rejected(artifact_dict, because="no step_ids")


# --------------------------------------------------------------------------
# Replay config
# --------------------------------------------------------------------------


def test_interstitial_probe_must_be_shorter_than_action_timeout(artifact_dict):
    """PLAN.md C9: probing every interstitial at the full action timeout adds
    minutes of dead waiting to an otherwise seconds-long flow."""
    artifact_dict["replay_config"]["interstitial_probe_timeout_ms"] = 8000
    assert "well below" in expect_rejected(artifact_dict, because="slow probe")


def test_default_replay_mode_is_strict(artifact_dict):
    """The graded evidence run must show zero LLM calls (PLAN.md C10)."""
    del artifact_dict["replay_config"]["mode"]
    artifact = Artifact.model_validate(artifact_dict)
    assert artifact.replay_config.mode.value == "strict"


# --------------------------------------------------------------------------
# Self-healing
# --------------------------------------------------------------------------


def test_self_healing_enabled_requires_configuration(artifact_dict):
    artifact_dict["self_healing"] = {"enabled": True}
    assert "on_layer2_used" in expect_rejected(artifact_dict, because="half-configured")


# --------------------------------------------------------------------------
# Session recovery
# --------------------------------------------------------------------------


def test_recovery_post_condition_must_use_on_recovery_fail(artifact_dict):
    artifact_dict["session_recovery"]["recovery_post_condition"]["on_fail"] = "retry"
    assert "on_recovery_fail" in expect_rejected(artifact_dict, because="wrong on_fail")
