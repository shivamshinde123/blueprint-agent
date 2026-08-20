"""Blueprint Agent CLI.

    discover   Drive a live UI with an LLM and record a reusable artifact.
    replay     Execute a saved artifact mechanically, with no LLM decisions.
    validate   Type-check an artifact and its cross-references, offline.
    merge      Apply a tenant override to a base artifact.

`validate` and `merge` need neither a browser nor an API key. `replay --mode
strict` needs a browser but makes no model calls. Only `discover` (and
`--mode assisted`) contact a model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from src import settings
from src.artifact.merge import MergeError, merge
from src.artifact.schema import Artifact, ReplayMode
from src.artifact.validator import ArtifactError, load_artifact
from src.safety.guardrails import Allowlist, SafetyViolation
from src.settings import MODEL_SLUG, ensure_dirs

# --------------------------------------------------------------------------
# discover
# --------------------------------------------------------------------------


def cmd_discover(args: argparse.Namespace) -> int:
    from src.agent.discovery import discover
    from src.llm.client import LLMError

    ensure_dirs()

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        print(f"error: --params is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(params, dict):
        print("error: --params must be a JSON object", file=sys.stderr)
        return 2

    params = _with_credentials(params, args.credentials)

    _announce_console(args.escalate)

    async def go() -> int:
        result = await discover(
            goal=args.goal,
            url=args.url,
            params=params,
            capability_id=args.capability,
            output_path=args.output,
            enable_escalation=args.escalate,
        )
        log_path = settings.EVIDENCE_DIR / f"{result.run_log.run_id}.json"

        if result.succeeded and result.artifact:
            print(f"\nrecorded {len(result.artifact.steps)} steps")
            print(f"  artifact  {args.output}")
            print(f"  evidence  {log_path}")
            print(f"  outputs   {sorted(result.artifact.output_schema)}")
            print(f"  model     {result.run_log.llm_call_count} call(s)")
            print(
                "\nNext: run the negative probe to record business outcomes, "
                "then replay in strict mode."
            )
            return 0

        print(f"\ndiscovery did not complete: {result.stopped_because}", file=sys.stderr)
        print(f"  evidence  {log_path}", file=sys.stderr)
        return 1

    try:
        return asyncio.run(go())
    except LLMError as exc:
        print(f"model error: {exc}", file=sys.stderr)
        return 4
    except SafetyViolation as exc:
        print(f"blocked by guardrails: {exc}", file=sys.stderr)
        return 5


# --------------------------------------------------------------------------
# replay
# --------------------------------------------------------------------------


def cmd_replay(args: argparse.Namespace) -> int:
    from src.artifact.schema import ResultType
    from src.replay.engine import replay

    ensure_dirs()

    try:
        artifact = load_artifact(args.artifact)
    except ArtifactError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    try:
        params = json.loads(args.params) if args.params else {}
    except json.JSONDecodeError as exc:
        print(f"error: --params is not valid JSON: {exc}", file=sys.stderr)
        return 2

    params = _with_credentials(params, args.credentials)
    mode = ReplayMode(args.mode)

    llm = None
    if mode is ReplayMode.ASSISTED:
        from src.llm.client import LLMClient, LLMError

        try:
            llm = LLMClient()
        except LLMError as exc:
            print(f"assisted mode needs a model client: {exc}", file=sys.stderr)
            return 4

    _announce_console(args.escalate)

    async def go() -> int:
        result, run_log = await replay(
            artifact,
            params,
            mode=mode,
            llm=llm,
            enable_escalation=args.escalate,
        )
        log_path = settings.EVIDENCE_DIR / f"{run_log.run_id}.json"

        print(f"\n{result.summary()}")
        print(f"  steps     {result.steps_completed}/{result.total_steps}")
        print(f"  layer 2   {'used' if result.layer2_used else 'not used'}")
        print(f"  model     {result.llm_calls} call(s)")
        print(f"  duration  {result.duration_ms} ms")
        print(f"  evidence  {log_path}")

        if result.result_type is ResultType.SUCCESS:
            return 0
        if result.result_type is ResultType.BUSINESS_OUTCOME:
            # A valid answer, not an error -- but distinguishable from success
            # for a caller checking the exit code.
            return 0 if not args.strict_exit else 20
        return 1

    try:
        return asyncio.run(go())
    except (ArtifactError, SafetyViolation) as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 5


# --------------------------------------------------------------------------
# validate
# --------------------------------------------------------------------------


def cmd_validate(args: argparse.Namespace) -> int:
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

    _describe(artifact, path)

    if args.check_allowlist:
        from src.safety.guardrails import preflight

        try:
            preflight(artifact.target.url, artifact.steps, Allowlist.load())
            print("  allowlist      ok")
        except SafetyViolation as exc:
            print(f"\nallowlist check failed: {exc}", file=sys.stderr)
            return 5

    return 0


# --------------------------------------------------------------------------
# merge
# --------------------------------------------------------------------------


def cmd_merge(args: argparse.Namespace) -> int:
    try:
        artifact, override = merge(args.base, args.override)
    except MergeError as exc:
        print(f"merge failed: {exc}", file=sys.stderr)
        return 1

    print(f"merged {Path(args.base).name} + {override.tenant_id}")
    _describe(artifact, Path(args.override))

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                artifact.model_dump(mode="json", exclude_none=True),
                indent=2,
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )
        print(f"  written        {out}")
    return 0


# --------------------------------------------------------------------------
# shared
# --------------------------------------------------------------------------


def _describe(artifact: Artifact, path: Path) -> None:
    sensitive = sorted(artifact.sensitive_parameters())
    fragile = [s.step_id for s in artifact.steps if s.fragile]
    risky = [
        s.step_id for s in artifact.steps if s.risk_level.requires_human_confirmation
    ]
    print(f"valid: {path}")
    print(f"  capability     {artifact.capability_id} v{artifact.version}")
    print(f"  surface        {artifact.surface_type.value}")
    print(f"  steps          {len(artifact.steps)}")
    print(f"  inputs         {sorted(artifact.input_parameters)}")
    print(f"  outputs        {sorted(artifact.output_schema)}")
    print(f"  sensitive      {sensitive or '(none)'}")
    print(f"  replay mode    {artifact.replay_config.mode.value}")
    print(f"  outcomes       {[o.outcome_code for o in artifact.business_outcomes]}")
    print(f"  fragile steps  {fragile or '(none)'}")
    print(f"  needs approval {risky or '(none)'}")


def _with_credentials(params: dict, prefix: str | None) -> dict:
    """Fold credentials from .env into params, without putting them on argv.

    A password passed as a command-line argument lands in shell history and in
    the process list, where redaction cannot reach it.
    """
    if not prefix:
        return params
    creds = settings.credentials(prefix.upper())
    merged = dict(params)
    if creds["username"]:
        merged.setdefault("auth_username", creds["username"])
    if creds["password"]:
        merged.setdefault("auth_password", creds["password"])
    return merged


def _announce_console(enabled: bool) -> None:
    if enabled:
        print(
            f"operator console will start on "
            f"http://{settings.OPERATOR_HOST}:{settings.OPERATOR_PORT}/operator "
            f"if a handoff is needed"
        )


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="blueprint",
        description=(
            "Discover a UI flow once with a model; replay it forever without one."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # -- discover --
    discover = sub.add_parser(
        "discover", help="record a capability artifact from a natural-language goal"
    )
    discover.add_argument("--goal", required=True, help="natural-language goal")
    discover.add_argument("--url", required=True, help="target application URL")
    discover.add_argument("--output", required=True, help="path to write artifact JSON")
    discover.add_argument(
        "--capability",
        default="recorded_capability",
        help="snake_case capability id (default: recorded_capability)",
    )
    discover.add_argument(
        "--params",
        default="{}",
        help='input parameters as JSON, e.g. \'{"employee_name": "Peter Anderson"}\'',
    )
    discover.add_argument(
        "--credentials",
        help="env prefix to read credentials from, e.g. SAUCEDEMO",
    )
    discover.add_argument(
        "--model", default=MODEL_SLUG, help=f"gateway model slug (default: {MODEL_SLUG})"
    )
    discover.add_argument(
        "--escalate", action="store_true", help="enable the human handoff console"
    )
    discover.set_defaults(func=cmd_discover)

    # -- replay --
    replay = sub.add_parser("replay", help="execute a saved artifact deterministically")
    replay.add_argument("--artifact", required=True, help="path to artifact JSON")
    replay.add_argument(
        "--params", default="{}", help='input parameters as JSON, e.g. \'{"x": "1"}\''
    )
    replay.add_argument(
        "--credentials",
        help="env prefix to read credentials from, e.g. SAUCEDEMO",
    )
    replay.add_argument(
        "--mode",
        default="strict",
        choices=["strict", "assisted"],
        help="strict makes zero model calls (default, and used for evidence runs)",
    )
    replay.add_argument(
        "--escalate", action="store_true", help="enable the human handoff console"
    )
    replay.add_argument(
        "--strict-exit",
        action="store_true",
        help="exit 20 on a business outcome instead of 0",
    )
    replay.set_defaults(func=cmd_replay)

    # -- validate --
    validate = sub.add_parser(
        "validate", help="type-check an artifact offline, without opening a browser"
    )
    validate.add_argument("artifact", help="path to artifact JSON")
    validate.add_argument(
        "--check-allowlist",
        action="store_true",
        help="also verify every destination is permitted",
    )
    validate.set_defaults(func=cmd_validate)

    # -- merge --
    merge_cmd = sub.add_parser(
        "merge", help="apply a tenant override to a base artifact"
    )
    merge_cmd.add_argument("base", help="path to the base artifact")
    merge_cmd.add_argument("override", help="path to the tenant override")
    merge_cmd.add_argument("--output", help="write the merged artifact here")
    merge_cmd.set_defaults(func=cmd_merge)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except NotImplementedError as exc:
        print(f"not implemented: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
