"""Shared test fixtures.

Every test in this suite is hermetic: no network, no credentials, no live demo
sites. The golden artifact is the single realistic sample the schema tests
mutate to produce each invalid case.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src import settings

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_ARTIFACT = FIXTURES / "golden_artifact.json"

#: Output paths redirected below, so a test run never writes into the repo.
_OUTPUT_DIRS = ("ARTIFACTS_DIR", "HEAL_DIR", "EVIDENCE_DIR", "SCREENSHOTS_DIR")


@pytest.fixture(scope="session", autouse=True)
def _isolate_output_dirs(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """Point every write path at a temp dir for the whole session.

    ``evidence/`` and ``artifacts/`` are committed deliverables. Tests that
    exercise the failure path save a screenshot and a DOM dump, so without this
    a plain ``pytest`` run drops junk files into the curated folder.
    """
    sandbox = tmp_path_factory.mktemp("blueprint_output")
    layout = {
        "ARTIFACTS_DIR": sandbox / "artifacts",
        "HEAL_DIR": sandbox / "artifacts" / "heal",
        "EVIDENCE_DIR": sandbox / "evidence",
        "SCREENSHOTS_DIR": sandbox / "evidence" / "screenshots",
    }
    with pytest.MonkeyPatch.context() as mp:
        for name in _OUTPUT_DIRS:
            mp.setattr(settings, name, layout[name])
        mp.setattr(settings, "ROOT", sandbox)
        settings.ensure_dirs()
        yield sandbox


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
