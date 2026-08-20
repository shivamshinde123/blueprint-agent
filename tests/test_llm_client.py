"""LLM client tests — offline.

Nothing here makes a network call. The part worth testing is the Pydantic ->
strict-JSON-schema conversion: strict structured outputs reject a schema with
`$ref` pointers, missing `required` entries, or absent
`additionalProperties: false`, and the failure surfaces as an opaque gateway
400 at the worst possible moment (mid-discovery, against a live UI).
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

import pytest
from pydantic import BaseModel

from src.llm.client import LLMError, _json_schema_for, _tighten


class ActionKind(str, Enum):
    CLICK = "click"
    FILL = "fill"


class Nested(BaseModel):
    role: str | None = None
    name: str | None = None


class SampleDecision(BaseModel):
    """Shaped like the real discovery decision: enum, optionals, nested model."""

    action: ActionKind
    reasoning: str
    target_description: str
    value: str | None = None
    locator: Nested | None = None
    method: Literal["get_by_role", "get_by_text"] | None = None
    goal_achieved: bool = False
    stuck: bool = False


# --------------------------------------------------------------------------
# Strict-mode requirements
# --------------------------------------------------------------------------


def test_schema_has_no_ref_pointers():
    """Strict structured outputs reject `$ref`; nested models must be inlined."""
    schema = _json_schema_for(SampleDecision)
    assert "$defs" not in schema
    assert "$ref" not in _flatten_keys(schema)


def test_every_object_forbids_additional_properties():
    schema = _json_schema_for(SampleDecision)
    for obj in _all_objects(schema):
        assert obj.get("additionalProperties") is False


def test_every_property_is_required():
    """Strict mode requires the full property list, defaults included."""
    schema = _json_schema_for(SampleDecision)
    assert set(schema["required"]) == set(schema["properties"])
    # Fields with defaults must still appear.
    assert "goal_achieved" in schema["required"]
    assert "stuck" in schema["required"]


def test_defaults_are_stripped():
    """`default` is an annotation keyword strict mode rejects."""
    schema = _json_schema_for(SampleDecision)
    for obj in _all_objects(schema):
        assert "default" not in obj
        for prop in obj.get("properties", {}).values():
            assert "default" not in prop


def test_nested_model_is_inlined_and_tightened():
    schema = _json_schema_for(SampleDecision)
    locator = schema["properties"]["locator"]
    # Optional nested model becomes an anyOf of the object and null.
    objects = _all_objects(locator)
    assert objects, "nested model should be inlined as an object schema"
    for obj in objects:
        assert obj["additionalProperties"] is False
        assert set(obj["required"]) == {"role", "name"}


def test_enum_values_survive_conversion():
    schema = _json_schema_for(SampleDecision)
    action = schema["properties"]["action"]
    enum_values = action.get("enum") or [
        v for branch in action.get("anyOf", []) for v in branch.get("enum", [])
    ]
    assert set(enum_values) == {"click", "fill"}


def test_conversion_is_idempotent():
    once = _json_schema_for(SampleDecision)
    twice = _json_schema_for(SampleDecision)
    assert once == twice


def test_tighten_handles_lists_and_scalars():
    node = {"type": "object", "properties": {"xs": {"type": "array", "items": {}}}}
    _tighten(node)
    assert node["additionalProperties"] is False
    assert node["required"] == ["xs"]


def test_deeply_nested_cycle_is_rejected():
    """A self-referencing schema would otherwise inline forever."""
    from src.llm.client import _inline_refs

    node = {"$ref": "#/$defs/Loop"}
    defs = {"Loop": {"type": "object", "properties": {"next": {"$ref": "#/$defs/Loop"}}}}
    with pytest.raises(LLMError, match="too deep"):
        _inline_refs(node, defs)


# --------------------------------------------------------------------------
# Construction guardrails
# --------------------------------------------------------------------------


def test_missing_api_key_raises_a_useful_error(monkeypatch):
    from src.llm.client import LLMClient
    from src import settings

    monkeypatch.delenv(settings.API_KEY_ENV, raising=False)
    with pytest.raises(LLMError) as exc:
        LLMClient(api_key=None)
    assert settings.API_KEY_ENV in str(exc.value)
    assert "openrouter.ai/keys" in str(exc.value)


def test_call_count_starts_at_zero(monkeypatch):
    """Replay in strict mode asserts this stays 0 for the whole run."""
    from src.llm.client import LLMClient

    client = LLMClient(api_key="test-key-not-used")
    assert client.call_count == 0
    assert client.total_usage().total_tokens == 0


def test_sampling_parameters_are_never_sent():
    """Current Claude models reject temperature/top_p/seed outright, and
    determinism here is structural rather than sampler-based (PLAN.md C3)."""
    import inspect

    from src.llm.client import LLMClient

    source = inspect.getsource(LLMClient._create)
    for banned in ("temperature", "top_p", "top_k", "seed="):
        assert banned not in source.replace("# No temperature/top_p/seed", "")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _all_objects(node) -> list[dict]:
    """Every object-typed subschema, recursively."""
    found: list[dict] = []
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            found.append(node)
        for value in node.values():
            found.extend(_all_objects(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_all_objects(item))
    return found


def _flatten_keys(node) -> set[str]:
    keys: set[str] = set()
    if isinstance(node, dict):
        keys |= set(node.keys())
        for value in node.values():
            keys |= _flatten_keys(value)
    elif isinstance(node, list):
        for item in node:
            keys |= _flatten_keys(item)
    return keys
