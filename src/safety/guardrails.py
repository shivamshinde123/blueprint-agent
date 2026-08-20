"""Allowlist enforcement and risk gating.

Checked before every action, in **both** discovery and replay. The distinction
that matters: this is enforced in code, not merely requested in a prompt. A
discovery run where the model proposes navigating off-domain gets the action
refused, not politely discouraged.

See PLAN.md §7 G1 and G2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from src import settings
from src.artifact.schema import ActionType, RiskLevel, Step


class SafetyViolation(RuntimeError):
    """Base class for anything the guardrails refuse."""


class BlockedByAllowlist(SafetyViolation):
    """The action or destination is outside the permitted set."""


class RequiresHumanApproval(SafetyViolation):
    """A high/critical-risk step reached the gate without an approval path."""


@dataclass(frozen=True, slots=True)
class Allowlist:
    permitted_domains: frozenset[str]
    permitted_url_patterns: tuple[str, ...]
    permitted_actions: frozenset[str]
    blocked_actions: frozenset[str]

    @classmethod
    def load(cls, path: Path | None = None) -> Allowlist:
        path = path or settings.ALLOWLIST_PATH
        if not path.exists():
            raise BlockedByAllowlist(
                f"allowlist not found at {path}. Refusing to run without one — "
                f"an absent allowlist must never mean 'allow everything'."
            )
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            permitted_domains=frozenset(raw.get("permitted_domains", [])),
            permitted_url_patterns=tuple(raw.get("permitted_url_patterns", [])),
            permitted_actions=frozenset(raw.get("permitted_actions", [])),
            blocked_actions=frozenset(raw.get("blocked_actions", [])),
        )

    # -- checks ------------------------------------------------------------

    def check_origin(self, url: str) -> None:
        """Raise unless the scheme and host of *url* are permitted.

        Route patterns are deliberately *not* applied. An artifact's
        ``target.url`` identifies the application ("this capability is for
        OrangeHRM") and is usually a bare origin; requiring it to match a route
        pattern would reject every legitimate artifact. Actual navigation is
        checked by :meth:`check_url`.
        """
        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            raise BlockedByAllowlist(
                f"refused scheme {parsed.scheme!r} in {url!r}; only http/https "
                f"are permitted (file:// would expose the local filesystem)"
            )

        host = (parsed.hostname or "").lower()
        if not host:
            raise BlockedByAllowlist(f"could not parse a host from {url!r}")

        if host not in self.permitted_domains:
            raise BlockedByAllowlist(
                f"domain {host!r} is not in the allowlist. Permitted: "
                f"{sorted(self.permitted_domains)}"
            )

    def check_url(self, url: str) -> None:
        """Raise unless *url* is a permitted destination to navigate to."""
        self.check_origin(url)

        # An empty pattern list means "any route on a permitted domain".
        if self.permitted_url_patterns:
            parsed = urlparse(url)
            path = parsed.path or "/"
            # The application's own front door. Every flow starts by opening it,
            # and it is not a route the agent navigated *to* -- requiring it to
            # match a route pattern rejects the first step of every artifact.
            # The domain check above has already run.
            if path == "/":
                return
            if not any(p in path for p in self.permitted_url_patterns):
                raise BlockedByAllowlist(
                    f"path {path!r} on {parsed.hostname} matches no permitted "
                    f"route pattern. Permitted: "
                    f"{list(self.permitted_url_patterns)}"
                )

    def check_action(self, action: str | ActionType) -> None:
        """Raise unless *action* is permitted."""
        name = action.value if isinstance(action, ActionType) else str(action)

        if name in self.blocked_actions:
            raise BlockedByAllowlist(
                f"action {name!r} is explicitly blocked and is never executed, "
                f"including when the model asks for it"
            )
        if name not in self.permitted_actions:
            raise BlockedByAllowlist(
                f"action {name!r} is not in the permitted set "
                f"{sorted(self.permitted_actions)}"
            )

    def is_url_permitted(self, url: str) -> bool:
        try:
            self.check_url(url)
        except BlockedByAllowlist:
            return False
        return True

    def is_action_permitted(self, action: str | ActionType) -> bool:
        try:
            self.check_action(action)
        except BlockedByAllowlist:
            return False
        return True


# --------------------------------------------------------------------------
# Risk gating
# --------------------------------------------------------------------------


def requires_approval(step: Step) -> bool:
    """High and critical steps always pause for a human, with no override."""
    return step.risk_level.requires_human_confirmation


def describe_risk(step: Step) -> str:
    """Human-readable reason shown in the operator console."""
    reasons = {
        RiskLevel.HIGH: "irreversible write",
        RiskLevel.CRITICAL: "financial transaction",
    }
    reason = reasons.get(step.risk_level, step.risk_level.value)
    return (
        f"Step {step.step_id} ({step.action.value}) is classified "
        f"{step.risk_level.value} — {reason}. Human authorisation is required "
        f"before it executes."
    )


def check_step(step: Step, allowlist: Allowlist) -> None:
    """Validate a step's action and destination. Does not perform risk gating.

    Risk gating needs an escalation channel, so it lives in the replay engine
    where a handoff manager is available; this function is the part that can be
    decided from the step alone.
    """
    allowlist.check_action(step.action)
    if step.action is ActionType.NAVIGATE and step.url:
        allowlist.check_url(step.url)


def preflight(artifact_target_url: str, steps: list[Step], allowlist: Allowlist) -> None:
    """Check everything checkable before a browser is opened.

    Fail-fast matters here: discovering an allowlist violation at step 6 means
    steps 1–5 already ran against a live system.
    """
    # The target identifies the app, so only its origin is checked; every
    # concrete navigation still gets the full domain + route check.
    allowlist.check_origin(artifact_target_url)
    for step in steps:
        check_step(step, allowlist)
