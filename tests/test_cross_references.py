"""Cross-reference validation (PLAN.md §11 C13).

These are the failures that do *not* announce themselves. A stale `step_9` key
in the error map silently falls through to `_default`, so the per-step handling
you designed never runs and nothing anywhere reports a problem. Same for an
`output_schema` field that no extraction produces: replay "succeeds" and hands
the caller a result missing a field they were promised.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from src.artifact.schema import Artifact
from tests.conftest import step_by_id


def expect_rejected(data: dict[str, Any]) -> str:
    with pytest.raises(ValidationError) as exc:
        Artifact.model_validate(data)
    return str(exc.value)


# --------------------------------------------------------------------------
# Step identity
# --------------------------------------------------------------------------


def test_duplicate_step_ids_are_rejected(artifact_dict):
    step_by_id(artifact_dict, 5)["step_id"] = 4
    assert "duplicate step_id" in expect_rejected(artifact_dict)


def test_step_ids_must_be_sequential_from_one(artifact_dict):
    step_by_id(artifact_dict, 8)["step_id"] = 99
    assert "sequential" in expect_rejected(artifact_dict)


# --------------------------------------------------------------------------
# error_map keys
# --------------------------------------------------------------------------


def test_error_map_key_for_missing_step_is_rejected(artifact_dict):
    """The silent-failure case this whole module exists for."""
    artifact_dict["error_map"]["step_99"] = artifact_dict["error_map"].pop("step_8")
    assert "step_99" in expect_rejected(artifact_dict)


@pytest.mark.parametrize("bad_key", ["step8", "8", "default", "step_eight"])
def test_malformed_error_map_keys_are_rejected(artifact_dict, bad_key):
    artifact_dict["error_map"][bad_key] = artifact_dict["error_map"]["step_8"]
    assert "_default" in expect_rejected(artifact_dict)


def test_per_step_handler_overrides_default(artifact_dict):
    """Precedence must be step-specific first, then `_default`."""
    from src.artifact.schema import ErrorTypeKey

    artifact_dict["error_map"]["step_4"] = {
        "timeout": {
            "category": "recoverable",
            "action": "retry",
            "max_retries": 9,
            "retry_wait_ms": 500,
            "on_exhausted": "hard_failure",
        }
    }
    artifact = Artifact.model_validate(artifact_dict)

    specific = artifact.error_handler(4, ErrorTypeKey.TIMEOUT)
    fallback = artifact.error_handler(5, ErrorTypeKey.TIMEOUT)
    assert specific.max_retries == 9
    assert fallback.max_retries == 2  # from _default
    assert artifact.error_handler(4, ErrorTypeKey.SESSION_EXPIRED).action.value == "re_login"


def test_missing_handler_returns_none(artifact_dict):
    from src.artifact.schema import ErrorTypeKey

    del artifact_dict["error_map"]["_default"]["timeout"]
    artifact = Artifact.model_validate(artifact_dict)
    assert artifact.error_handler(5, ErrorTypeKey.TIMEOUT) is None


# --------------------------------------------------------------------------
# Business outcome references
# --------------------------------------------------------------------------


def test_business_outcome_referencing_missing_step_is_rejected(artifact_dict):
    artifact_dict["business_outcomes"][0]["step_ids"] = [7, 42]
    assert "step_id 42" in expect_rejected(artifact_dict)


def test_business_outcome_return_value_must_match_output_schema(artifact_dict):
    """Callers must always receive the same shape, whatever the result type."""
    artifact_dict["business_outcomes"][0]["return_value"] = {"job_title": None}
    assert "do not match output_schema" in expect_rejected(artifact_dict)


def test_business_outcome_extra_return_key_is_rejected(artifact_dict):
    artifact_dict["business_outcomes"][0]["return_value"]["unexpected"] = None
    assert "do not match output_schema" in expect_rejected(artifact_dict)


# --------------------------------------------------------------------------
# Outputs
# --------------------------------------------------------------------------


def test_output_schema_key_nothing_extracts_is_rejected(artifact_dict):
    """Otherwise a 'success' result quietly omits a promised field."""
    artifact_dict["output_schema"]["employment_status"] = "string"
    for outcome in artifact_dict["business_outcomes"]:
        outcome["return_value"]["employment_status"] = None
    assert "declares keys nothing extracts" in expect_rejected(artifact_dict)


def test_extracted_key_missing_from_output_schema_is_rejected(artifact_dict):
    step_by_id(artifact_dict, 8)["extractions"][0]["output_key"] = "undeclared_field"
    assert "missing from output_schema" in expect_rejected(artifact_dict)


def test_duplicate_output_key_is_rejected(artifact_dict):
    extractions = step_by_id(artifact_dict, 8)["extractions"]
    extractions[1]["output_key"] = extractions[0]["output_key"]
    assert "extracted more" in expect_rejected(artifact_dict)


# --------------------------------------------------------------------------
# Parameter templates
# --------------------------------------------------------------------------


def test_undeclared_template_parameter_is_rejected(artifact_dict):
    """`{{member_id}}` with no matching input parameter would substitute to
    nothing at replay time and type the literal braces into the field."""
    step_by_id(artifact_dict, 6)["value"] = "{{member_id}}"
    assert "undeclared parameters" in expect_rejected(artifact_dict)


def test_template_in_locator_name_is_resolved(artifact_dict):
    """Dynamic labels like "Account {{employee_name}} - Edit" are legal, as
    long as the parameter is declared."""
    step_by_id(artifact_dict, 7)["locators"]["primary"]["methods"] = [
        {"method": "get_by_role", "role": "link", "name": "Edit {{employee_name}}"}
    ]
    artifact = Artifact.model_validate(artifact_dict)
    assert "employee_name" in artifact.step_by_id(7).referenced_params


def test_undeclared_parameter_in_locator_is_rejected(artifact_dict):
    step_by_id(artifact_dict, 7)["locators"]["primary"]["methods"] = [
        {"method": "get_by_role", "role": "link", "name": "Edit {{ghost_param}}"}
    ]
    assert "ghost_param" in expect_rejected(artifact_dict)


def test_session_recovery_templates_are_checked(artifact_dict):
    artifact_dict["session_recovery"]["recovery_steps"][0]["value"] = "{{nope}}"
    assert "nope" in expect_rejected(artifact_dict)


# --------------------------------------------------------------------------
# Accessors used by the replay engine
# --------------------------------------------------------------------------


def test_outcomes_for_step_filters_correctly(artifact_dict):
    artifact = Artifact.model_validate(artifact_dict)
    assert [o.outcome_code for o in artifact.outcomes_for_step(4)] == ["AUTH_FAILED"]
    assert [o.outcome_code for o in artifact.outcomes_for_step(8)] == ["EMPLOYEE_NOT_FOUND"]
    assert artifact.outcomes_for_step(1) == []


def test_step_by_id_raises_for_unknown_step(artifact_dict):
    artifact = Artifact.model_validate(artifact_dict)
    assert artifact.step_by_id(3).action.value == "fill"
    with pytest.raises(KeyError):
        artifact.step_by_id(99)


def test_risk_levels_that_require_confirmation(artifact_dict):
    from src.artifact.schema import RiskLevel

    assert RiskLevel.HIGH.requires_human_confirmation
    assert RiskLevel.CRITICAL.requires_human_confirmation
    assert not RiskLevel.SAFE.requires_human_confirmation
    assert not RiskLevel.LOW.requires_human_confirmation
