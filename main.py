"""Blueprint Agent CLI.

Three commands:

    discover   Drive a live UI with an LLM and record a reusable artifact.
    replay     Execute a saved artifact mechanically, with no LLM decisions.
    validate   Type-check an artifact and its cross-references, offline.

`validate` works today. `discover` and `replay` are wired up but not yet
implemented — see PLAN.md §13 for the build order.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from src.artifact.schema import Artifact
from src.settings import MODEL_SLUG, ensure_dirs


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    """Phase 1: LLM-driven discovery against a live surface."""
    ensure_dirs()
    raise NotImplementedError(
        "discovery is not implemented yet - see PLAN.md section 13, Phase 2 "
        "(src/agent/discovery.py)"
    )


def cmd_replay(args: argparse.Namespace) -> int:
    """Phase 2: deterministic replay. No LLM in the decision loop."""
    ensure_dirs()
    raise NotImplementedError(
        "replay is not implemented yet - see PLAN.md section 13, Phase 3 "
        "(src/replay/engine.py)"
    )


def cmd_validate(args: argparse.Namespace) -> int:
    """Load an artifact and run every schema and cross-reference check."""
    path = Path(args.artifact)
    if not path.exists():
        print(f"error: no such file: {path}", file=sys.stderr)
        return 2

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"invalid JSON in {path}: {exc}", file=sys.stderr)
        return 1

    try:
        artifact = Artifact.model_validate(raw)
    except ValidationError as exc:
        print(f"artifact rejected: {path}\n", file=sys.stderr)
        print(exc, file=sys.stderr)
        return 1

    sensitive = sorted(artifact.sensitive_parameters())
    print(f"valid: {path}")
    print(f"  capability     {artifact.capability_id} v{artifact.version}")
    print(f"  surface        {artifact.surface_type.value}")
    print(f"  steps          {len(artifact.steps)}")
    print(f"  inputs         {sorted(artifact.input_parameters)}")
    print(f"  outputs        {sorted(artifact.output_schema)}")
    print(f"  sensitive      {sensitive or '(none)'}")
    print(f"  replay mode    {artifact.replay_config.mode.value}")
    print(f"  outcomes       {[o.outcome_code for o in artifact.business_outcomes]}")
    fragile = [s.step_id for s in artifact.steps if s.fragile]
    print(f"  fragile steps  {fragile or '(none)'}")
    risky = [
        s.step_id for s in artifact.steps if s.risk_level.requires_human_confirmation
    ]
    print(f"  needs approval {risky or '(none)'}")
    return 0


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blueprint",
        description="Discover a UI flow once with an LLM; replay it forever without one.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    discover = sub.add_parser(
        "discover", help="record a capability artifact from a natural-language goal"
    )
    discover.add_argument("--goal", required=True, help="natural-language goal")
    discover.add_argument("--url", required=True, help="target application URL")
    discover.add_argument("--output", required=True, help="path to write artifact JSON")
    discover.add_argument(
        "--mode",
        default="assisted",
        choices=["strict", "assisted"],
        help="discovery may use the vision fallback (default: assisted)",
    )
    discover.add_argument(
        "--model",
        default=MODEL_SLUG,
        help=f"gateway model slug (default: {MODEL_SLUG})",
    )
    discover.set_defaults(func=cmd_discover)

    replay = sub.add_parser("replay", help="execute a saved artifact deterministically")
    replay.add_argument("--artifact", required=True, help="path to artifact JSON")
    replay.add_argument(
        "--params", required=True, help='input parameters as JSON, e.g. \'{"x": "1"}\''
    )
    replay.add_argument(
        "--mode",
        default="strict",
        choices=["strict", "assisted"],
        help="strict makes zero LLM calls (default, and used for evidence runs)",
    )
    replay.set_defaults(func=cmd_replay)

    validate = sub.add_parser(
        "validate", help="type-check an artifact offline, without opening a browser"
    )
    validate.add_argument("artifact", help="path to artifact JSON")
    validate.set_defaults(func=cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except NotImplementedError as exc:
        print(f"not implemented: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
