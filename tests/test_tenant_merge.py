"""Base + tenant override merging.

The design claim being tested: a new tenant on the same underlying application
is onboarded with one small file and no re-recording, and the merged result is
held to exactly the same validation as any hand-written artifact.
"""

from __future__ import annotations

import json

import pytest

from src.artifact.merge import MergeError, TenantOverride, apply_overrides, merge
from tests.conftest import GOLDEN_ARTIFACT

TENANT_FILE = GOLDEN_ARTIFACT.parent.parent.parent / "config" / "tenants" / "bank_a.json"


@pytest.fixture
def base(golden_raw) -> dict:
    import copy

    return copy.deepcopy(golden_raw)


# --------------------------------------------------------------------------
# Path resolution
# --------------------------------------------------------------------------


def test_scalar_override(base):
    out = apply_overrides(base, {"target.url": "https://banka.example.com"})
    assert out["target"]["url"] == "https://banka.example.com"
    # The base is not mutated.
    assert base["target"]["url"] != "https://banka.example.com"


def test_indexed_override(base):
    out = apply_overrides(base, {"steps[0].description": "Open Bank A's login page"})
    assert out["steps"][0]["description"] == "Open Bank A's login page"


def test_deep_indexed_override(base):
    out = apply_overrides(
        base, {"steps[6].locators.primary.methods[0].name": "Find"}
    )
    assert out["steps"][6]["locators"]["primary"]["methods"][0]["name"] == "Find"


def test_coordinate_nudge(base):
    """The branding-header case: every screenshot coordinate shifts down."""
    original = base["steps"][6]["locators"]["fallback"]["coordinates"]["y"]
    out = apply_overrides(
        base, {"steps[6].locators.fallback.coordinates.y": original + 60}
    )
    assert out["steps"][6]["locators"]["fallback"]["coordinates"]["y"] == original + 60


def test_append_to_a_list(base):
    before = len(base["known_interstitials"])
    out = apply_overrides(
        base,
        {
            "known_interstitials[+]": {
                "name": "welcome_banner",
                "detect": {"condition": "url_contains", "value": "x", "on_fail": "retry"},
                "dismiss": {
                    "dismiss_action": "click",
                    "locators": {
                        "primary": {
                            "available": True,
                            "methods": [
                                {"method": "get_by_role", "role": "button", "name": "OK"}
                            ],
                        }
                    },
                },
            }
        },
    )
    assert len(out["known_interstitials"]) == before + 1
    assert out["known_interstitials"][-1]["name"] == "welcome_banner"


def test_several_overrides_apply_together(base):
    out = apply_overrides(
        base,
        {
            "target.url": "https://banka.example.com",
            "steps[0].url": "https://banka.example.com/login",
            "version": "1.0.1",
        },
    )
    assert out["target"]["url"] == "https://banka.example.com"
    assert out["steps"][0]["url"] == "https://banka.example.com/login"
    assert out["version"] == "1.0.1"


# --------------------------------------------------------------------------
# Overrides that match nothing
# --------------------------------------------------------------------------


def test_unknown_key_is_an_error_not_a_no_op(base):
    """An override that silently does nothing is how a tenant runs for weeks
    against the wrong button."""
    with pytest.raises(MergeError, match="does not exist"):
        apply_overrides(base, {"target.nonexistent": "x"})


def test_unknown_nested_path_is_an_error(base):
    with pytest.raises(MergeError, match="does not exist"):
        apply_overrides(base, {"steps[0].locators.primary.methods[0].name": "x"})


def test_index_out_of_range_is_an_error(base):
    with pytest.raises(MergeError, match="out of range"):
        apply_overrides(base, {"steps[99].description": "x"})


def test_indexing_a_non_list_is_an_error(base):
    with pytest.raises(MergeError, match="not a list"):
        apply_overrides(base, {"target[0].url": "x"})


def test_appending_to_a_non_list_is_an_error(base):
    with pytest.raises(MergeError, match="cannot append"):
        apply_overrides(base, {"target[+]": {"x": 1}})


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def test_shipped_example_override_loads():
    override = TenantOverride.load(TENANT_FILE)
    assert override.tenant_id == "bank_a"
    assert override.base_artifact == "golden_artifact.json"
    assert override.overrides


def test_missing_file(tmp_path):
    with pytest.raises(MergeError, match="no tenant override"):
        TenantOverride.load(tmp_path / "nope.json")


def test_malformed_json(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{ nope", encoding="utf-8")
    with pytest.raises(MergeError, match="not valid JSON"):
        TenantOverride.load(path)


def test_missing_required_field(tmp_path):
    path = tmp_path / "partial.json"
    path.write_text(json.dumps({"tenant_id": "x"}), encoding="utf-8")
    with pytest.raises(MergeError, match="missing required field"):
        TenantOverride.load(path)


# --------------------------------------------------------------------------
# End-to-end
# --------------------------------------------------------------------------


def test_shipped_override_merges_and_validates():
    """The whole claim in one test: one small file, no re-recording, and the
    result is a fully valid artifact."""
    artifact, override = merge(GOLDEN_ARTIFACT, TENANT_FILE)

    assert override.tenant_id == "bank_a"
    assert artifact.target.url == "https://banka.orangehrm.example.com"
    assert artifact.steps[6].locators.primary.methods[0].name == "Find"
    assert artifact.steps[6].locators.fallback.coordinates.y == 512
    assert any(i.name == "bank_a_welcome_banner" for i in artifact.known_interstitials)
    # Everything not overridden is inherited unchanged.
    assert len(artifact.steps) == 8
    assert artifact.business_outcomes[0].outcome_code == "EMPLOYEE_NOT_FOUND"


def test_tenant_appears_in_the_capability_id():
    """So an evidence log cannot be mistaken for a different tenant's run."""
    artifact, _ = merge(GOLDEN_ARTIFACT, TENANT_FILE)
    assert artifact.capability_id.endswith("__bank_a")


def test_base_mismatch_is_refused(tmp_path):
    """A re-versioned base may no longer line up with an old override."""
    override = json.loads(TENANT_FILE.read_text(encoding="utf-8"))
    override["base_artifact"] = "some_other_artifact_v2.json"
    path = tmp_path / "stale.json"
    path.write_text(json.dumps(override), encoding="utf-8")

    with pytest.raises(MergeError, match="targets base artifact"):
        merge(GOLDEN_ARTIFACT, path)


def test_merged_artifact_is_held_to_full_validation(tmp_path):
    """An override cannot produce something the engine would refuse -- it fails
    at load, with the same messages as any other bad artifact."""
    override = json.loads(TENANT_FILE.read_text(encoding="utf-8"))
    override["overrides"] = {"version": "not-semver"}
    path = tmp_path / "broken.json"
    path.write_text(json.dumps(override), encoding="utf-8")

    with pytest.raises(MergeError, match=r"not.*valid"):
        merge(GOLDEN_ARTIFACT, path)


def test_missing_base(tmp_path):
    with pytest.raises(MergeError, match="no base artifact"):
        merge(tmp_path / "nope.json", TENANT_FILE)
