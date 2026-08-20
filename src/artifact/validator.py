"""Artifact loading and pre-run validation.

Everything here runs *before* a browser is opened. The ordering is deliberate:
discovering a missing parameter or an off-allowlist target after five steps
have already executed means five real actions against a live system that
cannot be taken back.

Schema and cross-reference checks live on the model itself
(:class:`src.artifact.schema.Artifact`); this module handles the file, the
runtime inputs, and the environment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.artifact.schema import Artifact, BrowserConfig, ReplayMode
from src.safety.guardrails import Allowlist, preflight, requires_approval


class ArtifactError(RuntimeError):
    """The artifact cannot be loaded or is not runnable as configured."""


class MissingParameters(ArtifactError):
    """Required input parameters were not supplied."""


class ConfigError(ArtifactError):
    """The artifact needs a capability the runtime was not given."""


class BrowserMismatch(ArtifactError):
    """The live browser does not match the config the artifact was recorded at."""


def load_artifact(path: str | Path) -> Artifact:
    """Read and fully validate an artifact from disk."""
    path = Path(path)
    if not path.exists():
        raise ArtifactError(f"no artifact at {path}")

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ArtifactError(f"{path} is not valid JSON: {exc}") from exc

    try:
        return Artifact.model_validate(raw)
    except ValidationError as exc:
        raise ArtifactError(f"{path} failed validation:\n{exc}") from exc


def check_parameters(artifact: Artifact, params: dict[str, Any]) -> None:
    """Every required parameter must be present, and no unknown ones supplied."""
    missing = [
        name
        for name, spec in artifact.input_parameters.items()
        if spec.required and params.get(name) in (None, "")
    ]
    if missing:
        raise MissingParameters(
            f"missing required parameter(s): {sorted(missing)}. "
            f"Declared inputs: {sorted(artifact.input_parameters)}"
        )

    unknown = sorted(set(params) - set(artifact.input_parameters))
    if unknown:
        raise MissingParameters(
            f"unknown parameter(s) supplied: {unknown}. The artifact declares "
            f"{sorted(artifact.input_parameters)}. Refusing rather than "
            f"silently ignoring them, in case one is a typo for a real field."
        )

    wrong_type = [
        name
        for name, spec in artifact.input_parameters.items()
        if name in params
        and params[name] is not None
        and not _matches_type(params[name], spec.type.value)
    ]
    if wrong_type:
        raise MissingParameters(
            f"parameter(s) with the wrong type: {sorted(wrong_type)}"
        )


def _matches_type(value: Any, declared: str) -> bool:
    if declared == "string":
        return isinstance(value, str)
    if declared == "integer":
        # bool is an int subclass in Python; a boolean is not an integer here.
        return isinstance(value, int) and not isinstance(value, bool)
    if declared == "boolean":
        return isinstance(value, bool)
    return True


def check_escalation_available(artifact: Artifact, *, has_handoff: bool) -> None:
    """A risky artifact with nowhere to escalate must not start."""
    risky = [s.step_id for s in artifact.steps if requires_approval(s)]
    if risky and not has_handoff:
        raise ConfigError(
            f"step(s) {risky} are high/critical risk and require human "
            f"authorisation, but no handoff manager is configured. Refusing to "
            f"start: the alternative is pausing mid-flow with no way to resume."
        )


def check_browser_matches(recorded: BrowserConfig, live: dict[str, Any]) -> None:
    """Refuse to replay if the live browser differs from the recorded one.

    Screenshot coordinates are only meaningful at the configuration they were
    captured under. Width and height are not sufficient — device pixel ratio and
    headless-vs-headful rendering both move pixels. See PLAN.md §11 C2.
    """
    if not recorded.enforce_strictly:
        return

    mismatches: list[str] = []

    def compare(name: str, expected: Any, actual: Any) -> None:
        if actual is not None and actual != expected:
            mismatches.append(f"{name}: recorded {expected!r}, live {actual!r}")

    compare("viewport.width", recorded.viewport.width, live.get("viewport_width"))
    compare("viewport.height", recorded.viewport.height, live.get("viewport_height"))
    compare(
        "device_scale_factor",
        recorded.device_scale_factor,
        live.get("device_scale_factor"),
    )
    compare("is_mobile", recorded.is_mobile, live.get("is_mobile"))
    compare("headless", recorded.headless, live.get("headless"))
    compare("locale", recorded.locale, live.get("locale"))
    compare("timezone_id", recorded.timezone_id, live.get("timezone_id"))

    if mismatches:
        raise BrowserMismatch(
            "browser configuration does not match the artifact:\n  - "
            + "\n  - ".join(mismatches)
            + "\nStored screenshot coordinates are only valid under the "
            "recorded configuration."
        )


@dataclass(slots=True)
class PreflightResult:
    """What the pre-flight established, for the evidence log."""

    artifact: Artifact
    mode: ReplayMode
    redacted_params: dict[str, Any]
    risky_steps: list[int]
    fragile_steps: list[int]


def preflight_replay(
    artifact: Artifact,
    params: dict[str, Any],
    *,
    allowlist: Allowlist,
    mode: ReplayMode | None = None,
    has_handoff: bool = False,
) -> PreflightResult:
    """Run every check that can be made before opening a browser.

    Order is cheapest-and-most-likely-first, so the common mistakes surface
    with the clearest message.
    """
    from src.safety.redaction import redact_params

    check_parameters(artifact, params)
    preflight(artifact.target.url, artifact.steps, allowlist)
    check_escalation_available(artifact, has_handoff=has_handoff)

    resolved = mode or artifact.replay_config.mode

    return PreflightResult(
        artifact=artifact,
        mode=resolved,
        redacted_params=redact_params(artifact, params),
        risky_steps=[s.step_id for s in artifact.steps if requires_approval(s)],
        fragile_steps=[s.step_id for s in artifact.steps if s.fragile],
    )
