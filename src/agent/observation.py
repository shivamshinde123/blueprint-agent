"""Reading the page.

Three jobs:

**Snapshot.** An ARIA snapshot is a compact YAML-ish rendering of the
accessibility tree — roles and accessible names, without the markup noise. It
is what the model sees on a well-built page.

**Sparseness.** Deciding whether the tree is worth showing at all. This is the
switch between Layer 1 and Layer 2, so it is deliberately conservative: falling
back to vision unnecessarily costs a call, but *not* falling back on a page
with no labels wastes the whole turn.

**Diff.** Comparing before and after an action, so a recorded checkpoint
asserts something that actually changed. A post-condition generated from the
after-state alone can be trivially true — "page contains Search" on a page with
a permanent Search link passes whether or not the click worked. See PLAN.md
§11 C5.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from playwright.async_api import Page

#: A named node in an ARIA snapshot: `- button "Login"`, `- textbox "Username"`.
NAMED_NODE = re.compile(r'^\s*-\s*([a-z]+)\s+"([^"]*)"', re.MULTILINE)

#: Roles you can actually *do* something to. The distinction matters more than
#: it looks: a table-based legacy page produces plenty of named nodes, because
#: `cell` and `row` inherit an accessible name from their text content. The
#: local mock yields fourteen of them — `cell "LOGIN"`, `cell "Password :"` —
#: while exposing no button and no named input at all. Counting every named
#: node therefore reports a rich tree for a page Layer 1 cannot drive, and the
#: screenshot fallback never fires. See PLAN.md §11 C20.
INTERACTIVE_ROLES = frozenset(
    {
        "button", "link", "textbox", "searchbox", "checkbox", "radio", "combobox",
        "listbox", "option", "menuitem", "menuitemcheckbox", "menuitemradio",
        "tab", "switch", "slider", "spinbutton", "textarea",
    }
)

#: Sparse when fewer than this many *named interactive* nodes exist. Two,
#: because any page you can meaningfully drive exposes at least a control and
#: a target — a field and a submit, a link and a heading's link. One or zero
#: means the tree cannot carry the flow.
SPARSE_INTERACTIVE_THRESHOLD = 2

#: Snapshots are truncated before going to the model. Large applications can
#: emit thousands of lines, and the tail is almost always footer boilerplate.
MAX_SNAPSHOT_CHARS = 12_000


@dataclass(slots=True)
class Observation:
    """One reading of the page."""

    url: str
    title: str
    snapshot: str
    named_nodes: list[tuple[str, str]] = field(default_factory=list)
    truncated: bool = False

    @property
    def named_node_count(self) -> int:
        return len(self.named_nodes)

    @property
    def interactive_nodes(self) -> list[tuple[str, str]]:
        """Named nodes you can actually click, type into, or select."""
        return [
            (role, name)
            for role, name in self.named_nodes
            if role in INTERACTIVE_ROLES and name.strip()
        ]

    @property
    def interactive_count(self) -> int:
        return len(self.interactive_nodes)

    @property
    def is_sparse(self) -> bool:
        """True when the accessibility tree cannot drive this page.

        The Layer 1 / Layer 2 switch. Judged on *interactive* named nodes, not
        named nodes in general — a table-based page is full of named cells and
        rows while exposing nothing you can act on.
        """
        return self.interactive_count < SPARSE_INTERACTIVE_THRESHOLD

    @property
    def fingerprint(self) -> str:
        """Stable identity for dead-end detection.

        Uses URL plus the set of named nodes rather than the raw snapshot text,
        so cosmetic churn — a clock, a row count, a spinner frame — does not
        read as progress when nothing has actually changed.
        """
        nodes = "|".join(f"{role}:{name}" for role, name in sorted(self.named_nodes))
        return f"{self.url}#{nodes}"

    def names(self) -> set[str]:
        return {name for _, name in self.named_nodes if name}

    def contains_text(self, needle: str) -> bool:
        return needle.lower() in self.snapshot.lower()


async def observe(page: Page) -> Observation:
    """Read the current page state."""
    url = page.url
    try:
        title = await page.title()
    except Exception:  # pragma: no cover - page may be mid-navigation
        title = ""

    snapshot = await _aria_snapshot(page)
    truncated = len(snapshot) > MAX_SNAPSHOT_CHARS
    if truncated:
        snapshot = snapshot[:MAX_SNAPSHOT_CHARS] + "\n... (snapshot truncated)"

    return Observation(
        url=url,
        title=title,
        snapshot=snapshot,
        named_nodes=NAMED_NODE.findall(snapshot),
        truncated=truncated,
    )


async def _aria_snapshot(page: Page) -> str:
    """Best-effort ARIA snapshot.

    A legacy page may produce nothing useful here, which is not an error — it
    is precisely the signal that Layer 2 is needed.
    """
    try:
        return await page.locator("body").aria_snapshot()
    except Exception:
        # Older Playwright, or a page that refuses the call. Fall back to the
        # legacy accessibility API rather than failing the turn.
        try:
            tree = await page.accessibility.snapshot()
        except Exception:  # pragma: no cover
            return ""
        return _render_legacy_tree(tree) if tree else ""


def _render_legacy_tree(node: dict, depth: int = 0) -> str:
    """Render the legacy accessibility tree in the same shape as an ARIA snapshot."""
    if depth > 40:  # pragma: no cover - pathological nesting
        return ""
    lines: list[str] = []
    role = node.get("role", "")
    name = node.get("name", "")
    if role:
        indent = "  " * depth
        lines.append(f'{indent}- {role} "{name}"' if name else f"{indent}- {role}")
    for child in node.get("children", []) or []:
        rendered = _render_legacy_tree(child, depth + 1)
        if rendered:
            lines.append(rendered)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Diffing
# --------------------------------------------------------------------------


@dataclass(slots=True)
class Change:
    """What an action changed on the page."""

    url_changed: bool
    url_before: str
    url_after: str
    appeared: list[tuple[str, str]] = field(default_factory=list)
    disappeared: list[tuple[str, str]] = field(default_factory=list)

    @property
    def anything_changed(self) -> bool:
        return bool(self.url_changed or self.appeared or self.disappeared)

    @property
    def new_url_segment(self) -> str | None:
        """The most distinctive path segment the navigation introduced.

        Used to build a `url_contains` checkpoint that is specific to the
        destination rather than matching the whole application.
        """
        if not self.url_changed:
            return None
        before = set(_segments(self.url_before))
        after = _segments(self.url_after)
        new = [s for s in after if s not in before]
        if not new:
            return after[-1] if after else None
        # Longest new segment is the most specific, and least likely to be an
        # id that changes between runs.
        return max(new, key=len)


def _segments(url: str) -> list[str]:
    path = url.split("?", 1)[0].split("#", 1)[0]
    return [s for s in path.split("/") if s and "://" not in s and "." not in s]


def diff(before: Observation, after: Observation) -> Change:
    """What changed between two observations."""
    before_nodes = set(before.named_nodes)
    after_nodes = set(after.named_nodes)
    return Change(
        url_changed=before.url != after.url,
        url_before=before.url,
        url_after=after.url,
        appeared=sorted(after_nodes - before_nodes),
        disappeared=sorted(before_nodes - after_nodes),
    )


def distinctive_new_text(change: Change, before: Observation) -> str | None:
    """Text that appeared and was not present beforehand.

    Deliberately rejects anything already in the before-snapshot: a checkpoint
    that was true before the action verifies nothing.
    """
    before_lower = before.snapshot.lower()
    candidates = [
        name
        for _, name in change.appeared
        if name and len(name) >= 3 and name.lower() not in before_lower
    ]
    if not candidates:
        return None
    # Prefer short, stable-looking text: long strings tend to embed per-run
    # values (names, ids, totals) that will not recur on the next replay.
    candidates.sort(key=lambda s: (_looks_variable(s), len(s)))
    return candidates[0]


def _looks_variable(text: str) -> bool:
    """Heuristic: does this string look like it embeds per-run data?"""
    return bool(re.search(r"\d", text))
