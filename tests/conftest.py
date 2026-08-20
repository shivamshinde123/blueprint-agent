"""Shared test fixtures.

Every test in this suite is hermetic: no network, no credentials, no live demo
sites. The golden artifact is the single realistic sample the schema tests
mutate to produce each invalid case.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_ARTIFACT = FIXTURES / "golden_artifact.json"


@pytest.fixture(scope="session")
def golden_raw() -> dict[str, Any]:
    """The golden artifact as a raw dict. Do not mutate — use `artifact_dict`."""
    with GOLDEN_ARTIFACT.open(encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture
def artifact_dict(golden_raw: dict[str, Any]) -> dict[str, Any]:
    """A fresh deep copy of the golden artifact, safe to mutate per test."""
    return copy.deepcopy(golden_raw)


def step_by_id(data: dict[str, Any], step_id: int) -> dict[str, Any]:
    """Locate a step inside a raw artifact dict."""
    for step in data["steps"]:
        if step["step_id"] == step_id:
            return step
    raise KeyError(f"no step {step_id} in fixture")
