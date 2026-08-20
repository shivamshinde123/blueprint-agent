"""Pre-run validation.

Every check here exists to fail *before* a browser opens. Finding a missing
parameter at step 6 means steps 1-5 already ran against a live system.
"""

from __future__ import annotations

import json

import pytest

from src.artifact.schema import Artifact, BrowserConfig, RiskLevel
from src.artifact.validator import (
    ArtifactError,
    BrowserMismatch,
    ConfigError,
    MissingParameters,
    check_browser_matches,
    check_escalation_available,
    check_parameters,
    load_artifact,
    preflight_replay,
)
from src.safety.guardrails import Allowlist
from tests.conftest import GOLDEN_ARTIFACT

GOOD_PARAMS = {
    "auth_username": "Admin",
    "auth_password": "admin123",
    "product_name": "Sauce Labs Backpack",
}


@pytest.fixture
def artifact(artifact_dict) -> Artifact:
    return Artifact.model_validate(artifact_dict)


@pytest.fixture
def allowlist(tmp_path) -> Allowlist:
    path = tmp_path / "allowlist.json"
    path.write_text(
        json.dumps(
            {
                "permitted_domains": ["www.saucedemo.com"],
                "permitted_url_patterns": ["/inventory"],
                "permitted_actions": ["click", "fill", "navigate", "extract"],
                "blocked_actions": ["upload", "download"],
            }
        ),
        encoding="utf-8",
    )
    return Allowlist.load(path)


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_load_golden_artifact():
    artifact = load_artifact(GOLDEN_ARTIFACT)
    assert artifact.capability_id == "add_product_to_cart"


def test_missing_file_is_reported_clearly(tmp_path):
    with pytest.raises(ArtifactError, match="no artifact at"):
        load_artifact(tmp_path / "nope.json")


def test_malformed_json_is_reported_clearly(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ArtifactError, match="not valid JSON"):
        load_artifact(path)


def test_schema_failure_names_the_file(tmp_path, artifact_dict):
    artifact_dict["version"] = "1.0"
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(artifact_dict), encoding="utf-8")
    with pytest.raises(ArtifactError, match="failed validation"):
        load_artifact(path)


# --------------------------------------------------------------------------
# Parameters
# --------------------------------------------------------------------------


def test_valid_params_pass(artifact):
    check_parameters(artifact, GOOD_PARAMS)


def test_missing_required_param_is_named(artifact):
    params = {k: v for k, v in GOOD_PARAMS.items() if k != "product_name"}
    with pytest.raises(MissingParameters, match="product_name"):
        check_parameters(artifact, params)


def test_empty_string_counts_as_missing(artifact):
    with pytest.raises(MissingParameters, match="product_name"):
        check_parameters(artifact, {**GOOD_PARAMS, "product_name": ""})


def test_unknown_param_is_refused_not_ignored(artifact):
    """A silently ignored parameter is usually a typo for a real one."""
    with pytest.raises(MissingParameters, match="employe_name"):
        check_parameters(artifact, {**GOOD_PARAMS, "employe_name": "typo"})


def test_wrong_type_is_refused(artifact):
    with pytest.raises(MissingParameters, match="wrong type"):
        check_parameters(artifact, {**GOOD_PARAMS, "product_name": 12345})


def test_boolean_is_not_accepted_as_integer(artifact_dict):
    """bool subclasses int in Python; the declared types should not blur."""
    artifact_dict["input_parameters"]["product_name"]["type"] = "integer"
    artifact = Artifact.model_validate(artifact_dict)
    with pytest.raises(MissingParameters, match="wrong type"):
        check_parameters(artifact, {**GOOD_PARAMS, "product_name": True})
    check_parameters(artifact, {**GOOD_PARAMS, "product_name": 42})


# --------------------------------------------------------------------------
# Escalation availability
# --------------------------------------------------------------------------


def test_safe_artifact_needs_no_handoff(artifact):
    check_escalation_available(artifact, has_handoff=False)


def test_risky_artifact_without_handoff_refuses_to_start(artifact):
    """Otherwise replay pauses mid-flow with no way to resume."""
    artifact.steps[3].risk_level = RiskLevel.CRITICAL
    with pytest.raises(ConfigError, match="no handoff manager"):
        check_escalation_available(artifact, has_handoff=False)


def test_risky_artifact_with_handoff_is_allowed(artifact):
    artifact.steps[3].risk_level = RiskLevel.HIGH
    check_escalation_available(artifact, has_handoff=True)


# --------------------------------------------------------------------------
# Browser match
# --------------------------------------------------------------------------


def test_matching_browser_passes():
    recorded = BrowserConfig()
    live = {
        "viewport_width": 1280,
        "viewport_height": 720,
        "device_scale_factor": 1,
        "is_mobile": False,
        "headless": False,
        "locale": "en-US",
        "timezone_id": "UTC",
    }
    check_browser_matches(recorded, live)


@pytest.mark.parametrize(
    "field,value",
    [
        ("viewport_width", 1920),
        ("viewport_height", 1080),
        ("device_scale_factor", 2),
        ("headless", True),
        ("locale", "de-DE"),
        ("timezone_id", "Asia/Kolkata"),
    ],
)
def test_any_pinned_mismatch_is_refused(field, value):
    """Stored coordinates are only valid under the recorded configuration --
    and more than width/height moves pixels (PLAN.md C2)."""
    recorded = BrowserConfig()
    live = {
        "viewport_width": 1280,
        "viewport_height": 720,
        "device_scale_factor": 1,
        "is_mobile": False,
        "headless": False,
        "locale": "en-US",
        "timezone_id": "UTC",
        field: value,
    }
    with pytest.raises(BrowserMismatch, match=field.split("_")[0]):
        check_browser_matches(recorded, live)


def test_enforce_strictly_false_skips_the_check():
    recorded = BrowserConfig(enforce_strictly=False)
    check_browser_matches(recorded, {"viewport_width": 800})


def test_unknown_live_fields_are_not_compared():
    """A field the caller could not measure should not fail the run."""
    check_browser_matches(BrowserConfig(), {"viewport_width": None})


# --------------------------------------------------------------------------
# Full pre-flight
# --------------------------------------------------------------------------


def test_preflight_returns_a_summary(artifact, allowlist):
    result = preflight_replay(artifact, GOOD_PARAMS, allowlist=allowlist)
    assert result.mode.value == "strict"
    assert result.risky_steps == []
    assert result.fragile_steps == []
    assert result.redacted_params["auth_password"] == "***REDACTED***"
    assert result.redacted_params["product_name"] == "Sauce Labs Backpack"


def test_preflight_mode_override(artifact, allowlist):
    from src.artifact.schema import ReplayMode

    result = preflight_replay(
        artifact, GOOD_PARAMS, allowlist=allowlist, mode=ReplayMode.ASSISTED
    )
    assert result.mode is ReplayMode.ASSISTED


def test_preflight_fails_on_bad_params_before_touching_the_allowlist(artifact, allowlist):
    with pytest.raises(MissingParameters):
        preflight_replay(artifact, {}, allowlist=allowlist)
