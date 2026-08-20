"""What discovery records — checkpoint synthesis, locator conversion, defaults.

No model and no browser: this is the logic that turns an executed action into
a step that will still work tomorrow.
"""

from __future__ import annotations

import pytest

from src.agent.decisions import (
    AgentDecision,
    DecisionAction,
    LocatorMethodName,
    ProposedLocator,
)
from src.agent.discovery import (
    _looks_sensitive,
    _risk,
    build_locators,
    default_error_map,
    synthesise_post_condition,
    to_artifact_locator,
)
from src.agent.observation import Observation, diff
from src.artifact.schema import (
    AccessibilityMethod,
    ActionType,
    ConditionType,
    ErrorCategory,
    ErrorTypeKey,
    RiskLevel,
)
from src.replay.locator import substitute


def make(url: str, snapshot: str) -> Observation:
    from src.agent.observation import NAMED_NODE

    return Observation(
        url=url, title="", snapshot=snapshot, named_nodes=NAMED_NODE.findall(snapshot)
    )


def proposed(method: str = "get_by_role", **kw) -> ProposedLocator:
    return ProposedLocator(method=LocatorMethodName(method), **kw)


# --------------------------------------------------------------------------
# Locator conversion
# --------------------------------------------------------------------------


def test_role_locator_converts():
    method = to_artifact_locator(proposed(role="button", name="Search"))
    assert method.method is AccessibilityMethod.GET_BY_ROLE
    assert method.role == "button"
    assert method.name == "Search"


def test_role_without_name_borrows_the_value():
    """The schema requires role+name together; a name-less proposal would
    otherwise fail validation and lose the whole turn."""
    method = to_artifact_locator(proposed(role="button", value="Search"))
    assert method.name == "Search"


def test_text_locator_without_value_borrows_the_name():
    method = to_artifact_locator(proposed("get_by_text", name="Balance Enquiry"))
    assert method.value == "Balance Enquiry"


def test_build_locators_marks_primary_available():
    locators = build_locators(proposed(role="button", name="Login"))
    assert locators.primary.available is True
    assert len(locators.primary.methods) == 1
    assert locators.fallback is None


# --------------------------------------------------------------------------
# Parameter substitution
# --------------------------------------------------------------------------


def test_substitute_replaces_templates():
    assert substitute("{{name}}", {"name": "Peter"}) == "Peter"
    assert substitute("Edit {{name}} now", {"name": "Peter"}) == "Edit Peter now"


def test_substitute_leaves_unknown_templates_intact():
    """Blanking an unknown template would type nothing and fail silently;
    leaving the braces makes the mistake visible."""
    assert substitute("{{ghost}}", {"name": "Peter"}) == "{{ghost}}"


def test_substitute_handles_none_and_empty():
    assert substitute(None, {}) is None
    assert substitute("", {"a": "b"}) == ""


def test_substitute_ignores_none_values():
    assert substitute("{{a}}", {"a": None}) == "{{a}}"


# --------------------------------------------------------------------------
# Checkpoint synthesis (PLAN.md C5)
# --------------------------------------------------------------------------


def test_navigation_produces_a_url_checkpoint():
    before = make("https://store.example/inventory", '- button "Login"')
    after = make("https://store.example/cart", '- heading "Your Cart"')

    condition, warning = synthesise_post_condition(
        diff(before, after),
        before,
        action=ActionType.CLICK,
        locators=None,
        value=None,
        timeout_ms=8000,
    )
    assert condition.condition is ConditionType.URL_CONTAINS
    assert condition.value == "cart"
    assert warning is None


def test_fill_checkpoint_asserts_the_field_holds_the_value():
    before = make("http://x/a", '- textbox "Name"')
    after = make("http://x/a", '- textbox "Name"')
    locators = build_locators(proposed("get_by_placeholder", value="Name"))

    condition, warning = synthesise_post_condition(
        diff(before, after),
        before,
        action=ActionType.FILL,
        locators=locators,
        value="{{product_name}}",
        timeout_ms=8000,
    )
    assert condition.condition is ConditionType.ELEMENT_HAS_VALUE
    assert condition.value == "{{product_name}}"
    assert condition.locators is not None
    assert warning is None


def test_click_checkpoint_uses_text_that_appeared():
    before = make("http://x/a", '- button "Search"')
    after = make("http://x/a", '- button "Search"\n- heading "Records Found"')
    locators = build_locators(proposed(role="button", name="Search"))

    condition, warning = synthesise_post_condition(
        diff(before, after),
        before,
        action=ActionType.CLICK,
        locators=locators,
        value=None,
        timeout_ms=8000,
    )
    assert condition.condition is ConditionType.PAGE_CONTAINS_TEXT
    assert condition.value == "Records Found"
    assert warning is None


def test_no_observable_change_produces_a_warning():
    """A weak checkpoint is recorded, but the reviewer is told it is weak
    rather than discovering it when replay passes a broken step."""
    before = make("http://x/a", '- button "Search"')
    after = make("http://x/a", '- button "Search"')
    locators = build_locators(proposed(role="button", name="Search"))

    condition, warning = synthesise_post_condition(
        diff(before, after),
        before,
        action=ActionType.CLICK,
        locators=locators,
        value=None,
        timeout_ms=8000,
    )
    assert condition.condition is ConditionType.ELEMENT_VISIBLE
    assert warning is not None
    assert "weak" in warning


def test_checkpoint_never_reuses_text_that_was_already_present():
    """The core C5 guarantee."""
    before = make("http://x/a", '- link "Search"\n- button "Go"')
    after = make("http://x/a", '- link "Search"\n- button "Go"\n- generic "Search"')
    locators = build_locators(proposed(role="button", name="Go"))

    condition, warning = synthesise_post_condition(
        diff(before, after),
        before,
        action=ActionType.CLICK,
        locators=locators,
        value=None,
        timeout_ms=8000,
    )
    assert condition.condition is not ConditionType.PAGE_CONTAINS_TEXT
    assert warning is not None


# --------------------------------------------------------------------------
# Risk classification
# --------------------------------------------------------------------------


def decision(**kw) -> AgentDecision:
    base = {"reasoning": "because", "action": DecisionAction.CLICK}
    return AgentDecision(**{**base, **kw})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("safe", RiskLevel.SAFE),
        ("low", RiskLevel.LOW),
        ("HIGH", RiskLevel.HIGH),
        ("critical", RiskLevel.CRITICAL),
    ],
)
def test_risk_parsing(raw, expected):
    assert _risk(decision(risk_level=raw)) is expected


def test_unrecognised_risk_is_not_treated_as_safe():
    """An unparseable classification must not become the safest option."""
    assert _risk(decision(risk_level="probably fine")) is RiskLevel.LOW


# --------------------------------------------------------------------------
# Sensitive-parameter defaults
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["password", "auth_password", "user_pwd", "api_key", "apikey", "session_token",
     "ssn", "card_pin", "client_secret", "auth_username"],
)
def test_credential_like_names_default_to_sensitive(name):
    assert _looks_sensitive(name)


@pytest.mark.parametrize("name", ["product_name", "account_number", "branch", "query"])
def test_ordinary_names_are_not_sensitive(name):
    assert not _looks_sensitive(name)


# --------------------------------------------------------------------------
# Default error map
# --------------------------------------------------------------------------


def test_default_error_map_is_schema_valid(artifact_dict):
    """Generated defaults must satisfy the same validators a hand-written map
    does -- otherwise every discovery run produces an unloadable artifact."""
    from src.artifact.schema import Artifact

    generated = default_error_map()
    artifact_dict["error_map"] = {
        "_default": {
            key.value: handler.model_dump(mode="json", exclude_none=True)
            for key, handler in generated["_default"].items()
        }
    }
    artifact = Artifact.model_validate(artifact_dict)
    assert artifact.error_handler(3, ErrorTypeKey.TIMEOUT) is not None


def test_default_hard_failures_capture_evidence():
    for key, handler in default_error_map()["_default"].items():
        if handler.category is ErrorCategory.HARD_FAILURE:
            assert handler.capture_screenshot, f"{key.value} captures no screenshot"


def test_default_recoverables_declare_exhaustion_behaviour():
    for key, handler in default_error_map()["_default"].items():
        if handler.category is ErrorCategory.RECOVERABLE:
            assert handler.on_exhausted is not None, f"{key.value} has no on_exhausted"
