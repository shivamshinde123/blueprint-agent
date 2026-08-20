"""Base artifact + per-tenant overrides.

Hundreds of tenants run the same vendor application, configured differently:
a custom login URL, a renamed button, a branding header that shifts every
screenshot coordinate down by 60px, one extra welcome banner. Recording a
separate artifact per tenant does not scale, and worse, it means a fix to the
underlying flow has to be applied hundreds of times.

So: one base artifact holds the canonical flow, and a small override file per
tenant says only what differs. They are merged at load time, before execution.
A new tenant on the same underlying application is onboarded with one override
file and no re-recording.

The merged result is validated exactly like any other artifact, so an override
cannot produce something the engine would refuse — it fails at load with the
same messages. See PLAN.md §10.2.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.artifact.schema import Artifact

#: `steps[3]` in a dot path.
INDEX = re.compile(r"^(.*)\[(\d+)\]$")

#: `known_interstitials[+]` appends rather than replacing.
APPEND = re.compile(r"^(.*)\[\+\]$")


class MergeError(RuntimeError):
    """The override could not be applied."""


@dataclass(slots=True)
class TenantOverride:
    """What one tenant changes about the base flow."""

    base_artifact: str
    tenant_id: str
    tenant_name: str
    overrides: dict[str, Any]
    notes: str | None = None

    @classmethod
    def load(cls, path: str | Path) -> TenantOverride:
        path = Path(path)
        if not path.exists():
            raise MergeError(f"no tenant override at {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MergeError(f"{path} is not valid JSON: {exc}") from exc

        missing = [
            key
            for key in ("base_artifact", "tenant_id", "overrides")
            if key not in raw
        ]
        if missing:
            raise MergeError(f"{path} is missing required field(s): {missing}")

        return cls(
            base_artifact=raw["base_artifact"],
            tenant_id=raw["tenant_id"],
            tenant_name=raw.get("tenant_name", raw["tenant_id"]),
            overrides=raw["overrides"] or {},
            notes=raw.get("notes"),
        )


def apply_overrides(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply dot-notation overrides to a raw artifact dict.

    Paths address the artifact the way a reader would describe it::

        "target.url"                                    -> replace a scalar
        "steps[3].locators.primary.methods[0].name"     -> replace, deep
        "steps[3].locators.fallback.coordinates.y"      -> nudge a coordinate
        "known_interstitials[+]"                        -> append an entry

    Every path must resolve against the base. A path that does not is an error
    rather than a silent no-op: an override that quietly does nothing is how a
    tenant runs for weeks against the wrong button.
    """
    import copy

    merged = copy.deepcopy(base)

    for path, value in overrides.items():
        append = APPEND.match(path)
        if append:
            _append_at(merged, append.group(1), value, path)
        else:
            _set_at(merged, path, value)

    return merged


def _walk_to_parent(
    root: Any, parts: list[str], full_path: str
) -> tuple[Any, str | int]:
    """Resolve all but the last segment, returning (container, final key)."""
    node: Any = root

    for i, part in enumerate(parts[:-1]):
        node = _descend(node, part, full_path, parts[: i + 1])

    last = parts[-1]
    index = INDEX.match(last)
    if index:
        name, position = index.group(1), int(index.group(2))
        if name:
            node = _descend(node, name, full_path, parts)
        if not isinstance(node, list):
            raise MergeError(
                f"{full_path!r}: {name or 'value'} is not a list in the base artifact"
            )
        if position >= len(node):
            raise MergeError(
                f"{full_path!r}: index {position} is out of range "
                f"(the base has {len(node)} item(s))"
            )
        return node, position

    if not isinstance(node, dict):
        raise MergeError(f"{full_path!r}: parent is not an object")
    if last not in node:
        raise MergeError(
            f"{full_path!r}: {last!r} does not exist in the base artifact. "
            f"An override that matches nothing would silently do nothing."
        )
    return node, last


def _descend(node: Any, part: str, full_path: str, so_far: list[str]) -> Any:
    index = INDEX.match(part)
    if index:
        name, position = index.group(1), int(index.group(2))
        if name:
            if not isinstance(node, dict) or name not in node:
                raise MergeError(
                    f"{full_path!r}: {'.'.join(so_far)} does not exist in the base"
                )
            node = node[name]
        if not isinstance(node, list):
            raise MergeError(f"{full_path!r}: {name!r} is not a list")
        if position >= len(node):
            raise MergeError(
                f"{full_path!r}: index {position} is out of range "
                f"(the base has {len(node)} item(s))"
            )
        return node[position]

    if not isinstance(node, dict) or part not in node:
        raise MergeError(
            f"{full_path!r}: {'.'.join(so_far)} does not exist in the base artifact"
        )
    return node[part]


def _set_at(root: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    container, key = _walk_to_parent(root, parts, path)
    container[key] = value  # type: ignore[index]


def _append_at(root: dict[str, Any], path: str, value: Any, full_path: str) -> None:
    parts = path.split(".")
    node: Any = root
    for i, part in enumerate(parts):
        node = _descend(node, part, full_path, parts[: i + 1])
    if not isinstance(node, list):
        raise MergeError(f"{full_path!r}: {path!r} is not a list, cannot append")
    node.append(value)


def merge(
    base_path: str | Path, override_path: str | Path
) -> tuple[Artifact, TenantOverride]:
    """Load a base artifact and a tenant override, and return the merged result."""
    base_path = Path(base_path)
    override = TenantOverride.load(override_path)

    if not base_path.exists():
        raise MergeError(f"no base artifact at {base_path}")

    # The override names the base it was written against. A mismatch means the
    # base has been re-versioned and the override may no longer line up.
    if override.base_artifact and override.base_artifact != base_path.name:
        raise MergeError(
            f"override targets base artifact {override.base_artifact!r} but "
            f"{base_path.name!r} was supplied. Point at the right base, or "
            f"update the override after reviewing it against the new version."
        )

    raw = json.loads(base_path.read_text(encoding="utf-8"))
    merged = apply_overrides(raw, override.overrides)

    # Make the tenant visible in the artifact identity, so an evidence log
    # cannot be mistaken for a different tenant's run.
    merged["capability_id"] = f"{merged['capability_id']}__{override.tenant_id}"

    try:
        artifact = Artifact.model_validate(merged)
    except ValidationError as exc:
        raise MergeError(
            f"the merged artifact for tenant {override.tenant_id!r} is not "
            f"valid:\n{exc}"
        ) from exc

    return artifact, override
