"""Allowlist, risk gating, and redaction.

Redaction is the highest-stakes code in this repo: a miss writes a real
credential to a file that ships in the evidence folder of a public repository.
The tests are correspondingly paranoid.
"""

from __future__ import annotations

import json

import pytest

from src.artifact.schema import Artifact
from src.safety.guardrails import (
    Allowlist,
    BlockedByAllowlist,
    check_step,
    describe_risk,
    preflight,
    requires_approval,
)
from src.safety.redaction import REDACTED, Redactor, redact_params, redact_text

PARAMS = {
    "auth_username": "Admin",
    "auth_password": "sup3rs3cr3t-pa55",
    "employee_name": "Peter Anderson",
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
                "permitted_domains": ["opensource-demo.orangehrmlive.com", "localhost"],
                "permitted_url_patterns": ["/web/index.php/auth/", "/web/index.php/pim/"],
                "permitted_actions": ["click", "fill", "navigate", "extract"],
                "blocked_actions": ["upload", "download", "execute_script"],
            }
        ),
        encoding="utf-8",
    )
    return Allowlist.load(path)


# --------------------------------------------------------------------------
# Allowlist — domains and routes
# --------------------------------------------------------------------------


def test_permitted_url_passes(allowlist):
    allowlist.check_url("https://opensource-demo.orangehrmlive.com/web/index.php/auth/login")


def test_off_domain_navigation_is_blocked(allowlist):
    with pytest.raises(BlockedByAllowlist, match="not in the allowlist"):
        allowlist.check_url("https://evil.example.com/web/index.php/auth/login")


def test_permitted_domain_but_forbidden_route_is_blocked(allowlist):
    with pytest.raises(BlockedByAllowlist, match="no permitted route"):
        allowlist.check_url("https://opensource-demo.orangehrmlive.com/admin/deleteAll")


def test_file_scheme_is_blocked(allowlist):
    """file:// on a permitted-looking host would expose the local filesystem."""
    with pytest.raises(BlockedByAllowlist, match="scheme"):
        allowlist.check_url("file:///c:/Users/secrets.txt")


@pytest.mark.parametrize(
    "sneaky",
    [
        "https://evil.com/?next=opensource-demo.orangehrmlive.com/web/index.php/auth/",
        "https://opensource-demo.orangehrmlive.com.evil.com/web/index.php/auth/",
        "https://user@evil.com/web/index.php/auth/",
    ],
)
def test_lookalike_hosts_are_blocked(allowlist, sneaky):
    """Matching on the parsed hostname, not a substring of the URL."""
    with pytest.raises(BlockedByAllowlist):
        allowlist.check_url(sneaky)


def test_subdomain_is_not_implicitly_permitted(allowlist):
    with pytest.raises(BlockedByAllowlist):
        allowlist.check_url("https://staging.opensource-demo.orangehrmlive.com/web/index.php/pim/")


def test_missing_allowlist_file_refuses_rather_than_permitting(tmp_path):
    """An absent allowlist must never be read as 'allow everything'."""
    with pytest.raises(BlockedByAllowlist, match="Refusing to run"):
        Allowlist.load(tmp_path / "does-not-exist.json")


# --------------------------------------------------------------------------
# Allowlist — actions
# --------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["click", "fill", "navigate", "extract"])
def test_permitted_actions_pass(allowlist, action):
    allowlist.check_action(action)


@pytest.mark.parametrize("action", ["upload", "download", "execute_script"])
def test_blocked_actions_are_refused(allowlist, action):
    with pytest.raises(BlockedByAllowlist, match="never executed"):
        allowlist.check_action(action)


def test_unlisted_action_is_refused(allowlist):
    with pytest.raises(BlockedByAllowlist, match="not in the permitted set"):
        allowlist.check_action("drag_and_drop")


def test_preflight_checks_every_navigate_step(artifact, allowlist):
    preflight(artifact.target.url, artifact.steps, allowlist)


def test_target_origin_is_checked_without_route_patterns(allowlist):
    """`target.url` names the application and is usually a bare origin, so
    applying route patterns to it would reject every valid artifact."""
    allowlist.check_origin("https://opensource-demo.orangehrmlive.com")
    with pytest.raises(BlockedByAllowlist, match="no permitted route"):
        allowlist.check_url("https://opensource-demo.orangehrmlive.com")


def test_origin_check_still_enforces_domain_and_scheme(allowlist):
    with pytest.raises(BlockedByAllowlist, match="not in the allowlist"):
        allowlist.check_origin("https://evil.example.com")
    with pytest.raises(BlockedByAllowlist, match="scheme"):
        allowlist.check_origin("file:///c:/secrets.txt")


def test_preflight_rejects_an_off_domain_target(artifact, allowlist):
    artifact.target.url = "https://evil.example.com"
    with pytest.raises(BlockedByAllowlist):
        preflight(artifact.target.url, artifact.steps, allowlist)


def test_preflight_catches_an_off_domain_step(artifact, allowlist):
    """Fail before the browser opens, not at step 6 with five actions done."""
    artifact.steps[0].url = "https://evil.example.com/login"
    with pytest.raises(BlockedByAllowlist):
        preflight(artifact.target.url, artifact.steps, allowlist)


def test_check_step_validates_action_and_url(artifact, allowlist):
    for step in artifact.steps:
        check_step(step, allowlist)


# --------------------------------------------------------------------------
# Risk gating
# --------------------------------------------------------------------------


def test_safe_and_low_steps_do_not_require_approval(artifact):
    assert not any(requires_approval(s) for s in artifact.steps)


def test_high_and_critical_steps_require_approval(artifact):
    from src.artifact.schema import RiskLevel

    step = artifact.steps[3]
    step.risk_level = RiskLevel.HIGH
    assert requires_approval(step)
    assert "authorisation is required" in describe_risk(step)

    step.risk_level = RiskLevel.CRITICAL
    assert requires_approval(step)
    assert "financial transaction" in describe_risk(step)


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------


def test_sensitive_params_are_redacted(artifact):
    out = redact_params(artifact, PARAMS)
    assert out["auth_username"] == REDACTED
    assert out["auth_password"] == REDACTED
    assert out["employee_name"] == "Peter Anderson"  # not sensitive


def test_literal_secret_is_scrubbed_from_text(artifact):
    redactor = Redactor(artifact, PARAMS)
    line = f"filled password field with {PARAMS['auth_password']} and submitted"
    out = redactor.text(line)
    assert PARAMS["auth_password"] not in out
    assert REDACTED in out


def test_sensitive_template_is_scrubbed(artifact):
    """The artifact stores `{{auth_password}}`; a rendered log line must not
    reveal which parameter a step consumed."""
    redactor = Redactor(artifact, PARAMS)
    assert redactor.text("value={{auth_password}}") == f"value={REDACTED}"


def test_non_sensitive_template_survives(artifact):
    redactor = Redactor(artifact, PARAMS)
    assert redactor.text("value={{employee_name}}") == "value={{employee_name}}"


def test_step_dump_is_redacted(artifact):
    """model_dump() walks the whole step, so a schema field added later is
    covered without editing the redactor."""
    redactor = Redactor(artifact, PARAMS)
    dumped = redactor.step(artifact.step_by_id(3))
    serialized = json.dumps(dumped)
    assert PARAMS["auth_password"] not in serialized
    assert "{{auth_password}}" not in serialized
    assert REDACTED in serialized


def test_nested_structures_are_redacted(artifact):
    redactor = Redactor(artifact, PARAMS)
    payload = {
        "outer": {
            "inner": [
                {"note": f"used {PARAMS['auth_password']}"},
                "{{auth_password}}",
            ]
        }
    }
    serialized = json.dumps(redactor.mapping(payload))
    assert PARAMS["auth_password"] not in serialized
    assert serialized.count(REDACTED) == 2


def test_key_named_after_a_sensitive_param_is_redacted(artifact):
    """Even if the value never matched a known secret string."""
    redactor = Redactor(artifact, PARAMS)
    out = redactor.mapping({"auth_password": "some-other-value"})
    assert out["auth_password"] == REDACTED


def test_overlapping_secrets_do_not_leave_fragments(artifact_dict):
    """Longest-first replacement: a shorter secret contained in a longer one
    must not leave the remainder of the longer one in the text."""
    artifact = Artifact.model_validate(artifact_dict)
    params = {**PARAMS, "auth_username": "secret", "auth_password": "secretpassword"}
    redactor = Redactor(artifact, params)
    out = redactor.text("token=secretpassword")
    assert "password" not in out
    assert out == f"token={REDACTED}"


def test_very_short_secrets_are_not_substring_redacted(artifact_dict):
    """A 2-character secret would match half the log and destroy it. The
    template and parameter-name paths still cover the value."""
    artifact = Artifact.model_validate(artifact_dict)
    redactor = Redactor(artifact, {**PARAMS, "auth_password": "ab"})
    assert redactor.text("a table of absolute values") == "a table of absolute values"
    assert redactor.text("{{auth_password}}") == REDACTED


def test_redact_text_handles_empty_input(artifact):
    redactor = Redactor(artifact, PARAMS)
    assert redactor.text("") == ""


def test_missing_param_value_does_not_crash(artifact):
    """Redaction runs before parameter validation in some paths."""
    redactor = Redactor(artifact, {"employee_name": "Peter Anderson"})
    assert redactor.secret_count == 0
    assert redactor.text("{{auth_password}}") == REDACTED
