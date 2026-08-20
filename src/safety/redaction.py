"""Sensitive-value redaction.

The rule this module enforces: a value belonging to a parameter marked
``sensitive`` must never reach an artifact, a log line, an evidence file, or
stdout. Replacement happens *before* the write, never after — nothing is
scrubbed retroactively, because by then it has already touched disk.

Two things are redacted, and they are different:

* **Literal values** — the actual secret string appearing anywhere in text.
* **Templates** — ``{{auth_password}}`` in a recorded step. The artifact holds
  the placeholder rather than the secret, but a log line rendering that step
  would otherwise show which parameter was used *and* its substituted value.

What this module does **not** cover is as important as what it does. Screenshots
of a page displaying PII, and accessibility snapshots sent to the model, can
still carry sensitive content. Those are handled elsewhere (screenshot
suppression around credential fills) or accepted and documented as limits. See
PLAN.md §11 C12 and REPORT.md § Safety.
"""

from __future__ import annotations

import re
from typing import Any

from src.artifact.schema import TEMPLATE, Artifact, Step

REDACTED = "***REDACTED***"

#: Values shorter than this are not substring-redacted. A one- or two-character
#: secret would match half the log and destroy its usefulness; the template and
#: parameter-name paths still cover it.
MIN_REDACTABLE_LENGTH = 4


def sensitive_values(artifact: Artifact, params: dict[str, Any]) -> set[str]:
    """The concrete secret strings for this run, from declared sensitive params."""
    values: set[str] = set()
    for name in artifact.sensitive_parameters():
        value = params.get(name)
        if isinstance(value, str) and len(value) >= MIN_REDACTABLE_LENGTH:
            values.add(value)
    return values


def redact_params(artifact: Artifact, params: dict[str, Any]) -> dict[str, Any]:
    """Copy of *params* with every sensitive value replaced.

    This is what gets logged. The un-redacted dict stays in memory and is
    passed to the browser, never to a writer.
    """
    sensitive = artifact.sensitive_parameters()
    return {
        name: (REDACTED if name in sensitive and value is not None else value)
        for name, value in params.items()
    }


def redact_text(text: str, secrets: set[str], sensitive_names: set[str]) -> str:
    """Scrub literal secrets and sensitive templates out of arbitrary text."""
    if not text:
        return text

    # Longest first, so an overlapping shorter secret cannot leave a fragment.
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = text.replace(secret, REDACTED)

    def _swap(match: re.Match[str]) -> str:
        return REDACTED if match.group(1) in sensitive_names else match.group(0)

    return TEMPLATE.sub(_swap, text)


def redact_step(
    step: Step, secrets: set[str], sensitive_names: set[str]
) -> dict[str, Any]:
    """Serialize a step for logging with sensitive content removed.

    Uses Pydantic v2 ``model_dump()`` and walks the result, so a field added to
    the schema later is covered without editing this function.
    """
    dumped = step.model_dump(mode="json", exclude_none=True)
    return _walk(dumped, secrets, sensitive_names)


def redact_mapping(
    data: dict[str, Any], secrets: set[str], sensitive_names: set[str]
) -> dict[str, Any]:
    """Redact an arbitrary JSON-serializable mapping before it is written."""
    return _walk(data, secrets, sensitive_names)


def _walk(node: Any, secrets: set[str], sensitive_names: set[str]) -> Any:
    if isinstance(node, dict):
        return {
            key: (
                REDACTED
                if key in sensitive_names and isinstance(value, str)
                else _walk(value, secrets, sensitive_names)
            )
            for key, value in node.items()
        }
    if isinstance(node, list):
        return [_walk(item, secrets, sensitive_names) for item in node]
    if isinstance(node, str):
        return redact_text(node, secrets, sensitive_names)
    return node


class Redactor:
    """Bound helper so callers do not re-derive the secret set on every write."""

    __slots__ = ("_secrets", "_names")

    def __init__(self, artifact: Artifact, params: dict[str, Any]) -> None:
        self._secrets = sensitive_values(artifact, params)
        self._names = artifact.sensitive_parameters()

    def text(self, value: str) -> str:
        return redact_text(value, self._secrets, self._names)

    def step(self, step: Step) -> dict[str, Any]:
        return redact_step(step, self._secrets, self._names)

    def mapping(self, data: dict[str, Any]) -> dict[str, Any]:
        return redact_mapping(data, self._secrets, self._names)

    def params(self, artifact: Artifact, params: dict[str, Any]) -> dict[str, Any]:
        return redact_params(artifact, params)

    @property
    def secret_count(self) -> int:
        return len(self._secrets)
