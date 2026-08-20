"""Does an artifact work for inputs it was never recorded with?

The property the whole system exists to deliver, and the one 320 other tests
could not see. Every bug reproduced below shipped past a green suite: valid
schema, resolvable locators, passing checkpoints, correct cross-references --
and an artifact that returns its recording run's answer forever.

Two layers here:

* Unit tests on the rule itself, one per real bug.
* An end-to-end replay of the *same artifact* with two different inputs,
  against a local fake store, asserting it returns each product's own price.
  That is the actual claim, and it needs no network and no model.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.artifact.reusability import (
    NotReusable,
    Violation,
    assert_reusable,
    check_reusable,
    embeds,
)
from src.artifact.schema import Artifact, BrowserConfig, ReplayMode, ResultType
from src.evidence.logger import RunLog
from src.replay.engine import ReplayEngine
from src.session.browser import browser_session
from tests import fake_app
from tests.test_browser import needs_chromium

# --------------------------------------------------------------------------
# The rule
# --------------------------------------------------------------------------


def test_template_reference_is_not_a_violation():
    """`{{param}}` is the *correct* way to depend on an input."""
    assert not embeds("{{employee_name}}", "Peter Anderson")
    assert not embeds("Edit {{product_name}}", "Widget")


def test_literal_value_is_a_violation():
    assert embeds("Peter Mac Anderson", "Peter Anderson")
    assert embeds("$29.99", "$29.99")


def test_substring_either_direction_counts():
    # Locator narrower than the value, and wider than it.
    assert embeds("Anderson", "Peter Anderson")
    assert embeds("Peter Anderson Profile", "Peter Anderson")


def test_short_values_are_ignored():
    """A two-character value collides with ordinary page text."""
    assert not embeds("Submit", "ID")
    assert not embeds("OK", "OK")


def test_structural_values_are_ignored():
    """A path or separator is structure, not data."""
    assert not embeds("/inventory.html", "/")
    assert not embeds("some text", "  ")


def test_case_and_whitespace_insensitive():
    assert embeds("  ANDERSON  ", "anderson")


# --------------------------------------------------------------------------
# The three bugs that actually shipped
# --------------------------------------------------------------------------


def _artifact(artifact_dict: dict[str, Any]) -> Artifact:
    return Artifact.model_validate(artifact_dict)


def test_checkpoint_naming_this_runs_result_is_caught(artifact_dict):
    """Bug 1: `page_contains_text: "Anderson"` after a search."""
    step = artifact_dict["steps"][6]
    step["post_condition"] = {
        "condition": "page_contains_text",
        "value": "Anderson",
        "on_fail": "retry",
    }
    violations = check_reusable(
        _artifact(artifact_dict), {"employee_name": "Peter Anderson"}
    )
    assert violations
    assert "checkpoint" in violations[0].where


def test_click_locator_naming_this_runs_record_is_caught(artifact_dict):
    """Bug 2: `option "Peter Mac Anderson"` for a typeahead suggestion."""
    artifact_dict["steps"][6]["locators"]["primary"]["methods"] = [
        {"method": "get_by_role", "role": "option", "name": "Peter Mac Anderson"}
    ]
    violations = check_reusable(
        _artifact(artifact_dict), {"employee_name": "Peter Anderson"}
    )
    assert violations
    assert "locator" in violations[0].where


def test_extraction_located_by_its_own_value_is_caught(artifact_dict):
    """Bug 3, and the worst: `get_by_text("$29.99")` to read a price.

    Circular -- it only finds the element when the answer is already known.
    Replay *succeeds* and reports this run's figure for every input, so there
    is no failure anywhere to notice.
    """
    extraction = artifact_dict["steps"][7]["extractions"][0]
    extraction["locators"]["primary"]["methods"] = [
        {"method": "get_by_text", "value": "Senior Engineer"}
    ]
    violations = check_reusable(
        _artifact(artifact_dict), {"job_title": "Senior Engineer"}
    )
    assert violations
    assert "extraction" in violations[0].where


def test_a_clean_artifact_has_no_violations(artifact_dict):
    artifact = _artifact(artifact_dict)
    assert (
        check_reusable(artifact, {"employee_name": "Someone Not On This Page"}) == []
    )


def test_credentials_are_never_echoed_into_a_violation(artifact_dict):
    """A locator embedding a password is the worst case -- and reporting it
    must not write the password into a log."""
    artifact_dict["steps"][3]["locators"]["primary"]["methods"] = [
        {"method": "get_by_role", "role": "button", "name": "hunter2-very-secret"}
    ]
    violations = check_reusable(
        _artifact(artifact_dict),
        {"auth_password": "hunter2-very-secret"},
        sensitive={"auth_password"},
    )
    assert violations
    assert "hunter2" not in str(violations[0])
    assert "REDACTED" in str(violations[0])


def test_assert_reusable_raises_with_actionable_guidance(artifact_dict):
    artifact_dict["steps"][6]["post_condition"] = {
        "condition": "page_contains_text",
        "value": "Anderson",
        "on_fail": "retry",
    }
    with pytest.raises(NotReusable) as exc:
        assert_reusable(_artifact(artifact_dict), {"name": "Peter Anderson"})
    message = str(exc.value)
    assert "only work for the input it was recorded with" in message
    assert "{{parameter}}" in message


def test_violation_reads_clearly():
    v = Violation("step 5 locator (get_by_text.value)", "$29.99", "price")
    assert "step 5" in str(v) and "price" in str(v)


# --------------------------------------------------------------------------
# The claim itself: one artifact, two different inputs
# --------------------------------------------------------------------------


def _store_artifact() -> Artifact:
    """A capability recorded against one product, written the right way.

    The product name is a `{{template}}` in the locator, and the price and
    description are located by their captions -- not by their values.
    """
    return Artifact.model_validate(
        {
            "capability_id": "lookup_product_price",
            "version": "1.0.0",
            "description": "Open a product and read its price and description.",
            "recorded_by": "agent",
            "created_at": "2026-08-20",
            "surface_type": "modern_web",
            "target": {
                "app_name": "store",
                "url": "http://localhost:8081/mock/store",
                "surface_type": "modern_web",
            },
            "input_parameters": {
                "product_name": {"type": "string", "required": True, "sensitive": False}
            },
            "output_schema": {"price": "string", "description": "string"},
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
                    "description": "Open the catalogue",
                    "fragile": False,
                    "risk_level": "safe",
                    "pre_condition": None,
                    "url": "http://localhost:8081/mock/store",
                    "post_condition": {
                        "condition": "page_contains_text",
                        "value": "Catalogue",
                        "on_fail": "hard_failure",
                    },
                },
                {
                    "step_id": 2,
                    "action": "click",
                    "description": "Open the requested product",
                    "fragile": False,
                    "risk_level": "safe",
                    # Parameterised: depends on the input by reference.
                    "locators": {
                        "primary": {
                            "strategy": "accessibility_tree",
                            "available": True,
                            "methods": [
                                {
                                    "method": "get_by_role",
                                    "role": "link",
                                    "name": "{{product_name}}",
                                }
                            ],
                        }
                    },
                    "post_condition": {
                        "condition": "url_contains",
                        "value": "item",
                        "on_fail": "retry",
                    },
                },
                {
                    "step_id": 3,
                    "action": "extract",
                    "description": "Read the price and description",
                    "fragile": False,
                    "risk_level": "safe",
                    "extractions": [
                        {
                            "output_key": "price",
                            # Located by its caption, never by its value.
                            "locators": {
                                "primary": {
                                    "strategy": "accessibility_tree",
                                    "available": True,
                                    "methods": [
                                        {"method": "get_by_label", "name": "Price"}
                                    ],
                                }
                            },
                            "extract_method": "get_value",
                            "expected_type": "string",
                            "required": True,
                        },
                        {
                            "output_key": "description",
                            "locators": {
                                "primary": {
                                    "strategy": "accessibility_tree",
                                    "available": True,
                                    "methods": [
                                        {"method": "get_by_label", "name": "Description"}
                                    ],
                                }
                            },
                            "extract_method": "get_value",
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
            "error_map": {
                "_default": {
                    "element_not_found": {
                        "category": "recoverable",
                        "action": "retry",
                        "max_retries": 1,
                        "retry_wait_ms": 10,
                        "on_exhausted": "hard_failure",
                    }
                }
            },
        }
    )


async def _replay_store(artifact: Artifact, product: str):
    engine = ReplayEngine(artifact, mode=ReplayMode.STRICT)
    params = {"product_name": product}
    run_log = RunLog.start(
        artifact=artifact, phase="replay", mode="strict", params=params
    )
    async with browser_session(BrowserConfig(headless=True)) as session:
        await fake_app.serve_store(session.page)
        return await engine.run(session, params, run_log)


@needs_chromium
async def test_artifact_recorded_for_one_input_works_for_another():
    """The claim, stated as a test.

    Record against Widget, replay against Gizmo, and get Gizmo's price. If a
    locator or checkpoint had baked in Widget's data, this returns $29.99 for
    both -- succeeding while being wrong, which is exactly the failure mode
    that motivated the reusability rule.
    """
    artifact = _store_artifact()

    recorded = await _replay_store(artifact, "Widget")
    assert recorded.result_type is ResultType.SUCCESS
    assert recorded.outputs["price"] == "$29.99"

    other = await _replay_store(artifact, "Gizmo")
    assert other.result_type is ResultType.SUCCESS
    assert other.outputs["price"] == "$9.99", (
        "the artifact returned the price it was recorded with, for a different "
        "product -- a locator or checkpoint has this run's data baked into it"
    )
    assert other.outputs["description"] != recorded.outputs["description"]


@needs_chromium
async def test_the_reusable_artifact_passes_the_rule():
    """Belt and braces: the same artifact the replay test uses must also
    satisfy the static check, with the values that replay produced."""
    artifact = _store_artifact()
    result = await _replay_store(artifact, "Widget")
    assert_reusable(
        artifact, {"product_name": "Widget", **(result.outputs or {})}
    )


@needs_chromium
async def test_a_baked_in_artifact_is_caught_by_replay_too():
    """The negative control.

    Break the artifact the way discovery once broke it -- locate the price by
    its value -- and confirm replay with a different product no longer returns
    that product's price. Without this, the positive test above could pass for
    the wrong reason.
    """
    from src.artifact.schema import AccessibilityLocatorMethod, AccessibilityMethod

    artifact = _store_artifact()
    extraction = artifact.steps[2].extractions[0]
    extraction.locators.primary.methods = [
        AccessibilityLocatorMethod(
            method=AccessibilityMethod.GET_BY_TEXT, value="$29.99"
        )
    ]

    result = await _replay_store(artifact, "Gizmo")
    # Either it fails outright, or it returns the wrong price. Both are wrong;
    # what it must never do is quietly return Gizmo's correct price.
    if result.result_type is ResultType.SUCCESS:
        assert result.outputs["price"] != "$9.99"

    # And the static rule catches it without running anything at all.
    assert check_reusable(artifact, {"price": "$29.99"})
