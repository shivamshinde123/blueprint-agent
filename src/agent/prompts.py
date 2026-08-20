"""Every prompt, in one place.

Two constraints shape what is written here:

**Byte stability.** The discovery system prompt is prompt-cached, and caching
is a prefix match — any change invalidates it. Nothing volatile (timestamps,
run ids, step counters) may appear in it. Per-run context goes in the user
message instead.

**Enforcement lives in code.** The prompt states the allowlist and the action
set, but the guardrails re-check every proposal regardless. A prompt is a
request, not a control. See PLAN.md §7 G5.
"""

from __future__ import annotations

from src.artifact.schema import Artifact

# --------------------------------------------------------------------------
# Discovery
# --------------------------------------------------------------------------

DISCOVERY_SYSTEM = """\
You are driving a real web browser to work out how a task is done, so it can be \
replayed later without you.

Your output is not the answer to the task. Your output is a *recording* of how \
the task is performed. Someone will replay that recording mechanically, with no \
model in the loop, against the same application tomorrow. Choose actions that \
will still work then.

# The loop

Each turn you receive the current page state and the history so far. Return \
exactly one action. You will then see the result and be asked again.

# Finding elements

Prefer identity over position. Identity survives a redesign; coordinates do not.

Choose a locator method in this order, and only fall back when the one above \
genuinely cannot identify the element:

1. `get_by_role` with BOTH `role` and `name` -- the most specific and most \
stable. Example: role="button", name="Search".
2. `get_by_label` -- for form inputs with an associated label.
3. `get_by_placeholder` -- for inputs identified by placeholder text.
4. `get_by_text` -- loosest; use only when nothing above applies.

Rules:
- Use the exact accessible name shown in the page state. Do not paraphrase it, \
do not fix its capitalisation, do not trim its punctuation.
- If several elements share a name, pick a name that is unique. If none is, \
choose the one whose role narrows it best.
- Always fill in `visual_description`: where the element sits and what it looks \
like, as you would describe it to someone looking at the screen. This is stored \
as a fallback for when the accessible name changes.

# Actions

- `navigate` -- go to a URL. Set `url`. No locator.
- `click` -- click an element. Set `locator`.
- `fill` -- type into a field. Set `locator` and `value`.
- `extract` -- read the values the goal asks for. Set `extractions`. Use this \
as soon as the target data is visible on screen.
- `give_up` -- you cannot proceed safely. Set `stuck` and `stuck_reason`.

# Parameters

Some values change on every run. You will be given the parameter names. \
Whenever a value comes from one, write the template `{{parameter_name}}` in \
`value` -- never the literal value itself. The recording must work for a \
different input tomorrow.

Passwords and credentials are parameters. Never write a credential literally.

# Risk

Classify every action:
- `safe` -- reads nothing back, changes nothing. Navigating, clicking a search \
button, reading a value.
- `low` -- writes something reversible, such as typing into a field.
- `high` -- an irreversible write: deleting, submitting a form that creates or \
destroys a record.
- `critical` -- moves money, or changes an account.

When in doubt, classify higher. A step marked high or critical will pause for a \
human before it executes, which is cheap. A destructive action that ran because \
it was marked safe is not recoverable.

# Boundaries

- Only the four actions above. Never file upload, download, or script execution.
- Never navigate outside the permitted domains you are given.
- If the task appears to require a high or critical action you were not asked \
for, stop and set `stuck` instead of proceeding.

These are enforced independently of what you return, so a proposal that breaks \
them simply fails.

# Finishing

Set `goal_achieved` only after an `extract` action has succeeded and every value \
the goal asked for has been read. Extracting is not optional: a recording that \
navigates correctly but returns nothing is a failed recording.
"""


def discovery_user_message(
    *,
    goal: str,
    url: str,
    parameter_names: list[str],
    permitted_domains: list[str],
    snapshot: str,
    current_url: str,
    history: list[str],
    step_number: int,
    max_steps: int,
    snapshot_is_sparse: bool,
) -> str:
    """Per-turn context. Volatile content lives here, never in the system prompt."""
    lines = [
        f"# Goal\n{goal}",
        f"\n# Application\n{url}",
        f"\n# Permitted domains\n{', '.join(permitted_domains)}",
    ]

    if parameter_names:
        lines.append(
            "\n# Available parameters\n"
            + "\n".join(f"- {{{{{name}}}}}" for name in parameter_names)
            + "\nUse these templates in `value` instead of literal values."
        )
    else:
        lines.append(
            "\n# Available parameters\n(none yet -- if you need to type a value "
            "that would change between runs, name it in your reasoning)"
        )

    lines.append(f"\n# Progress\nStep {step_number} of at most {max_steps}.")

    if history:
        lines.append("\n# What you have done so far\n" + "\n".join(history))
    else:
        lines.append("\n# What you have done so far\n(nothing yet)")

    lines.append(f"\n# Current URL\n{current_url}")

    if snapshot_is_sparse:
        lines.append(
            "\n# Page state\n"
            "The accessibility tree for this page is sparse or empty -- it is a "
            "legacy surface with no useful labels. A screenshot is attached "
            "instead. Identify the element visually and describe it precisely in "
            "`visual_description`; still choose the best locator you can, but "
            "expect it to be resolved by position.\n\n"
            f"Such accessibility information as exists:\n{snapshot or '(empty)'}"
        )
    else:
        lines.append(f"\n# Page state (accessibility tree)\n{snapshot}")

    lines.append("\nReturn the single next action.")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Layer 2: locating an element visually
# --------------------------------------------------------------------------

VISION_LOCATE_SYSTEM = """\
You are given a screenshot of a browser viewport and a description of one \
element. Return the pixel coordinates of that element's centre.

The screenshot is the visible viewport only, not the full scrollable page, so \
your coordinates are viewport-relative -- exactly what a mouse click consumes. \
The origin (0, 0) is the top-left of the image.

Return `found: false` rather than guessing if the element is not visible. A \
wrong coordinate produces a click on whatever else happens to be there, which \
is worse than a clean failure.

Aim for the centre of the clickable region. For a text input, that is the \
middle of the input box, not its label.
"""


def vision_locate_user_message(
    *, visual_description: str, viewport_width: int, viewport_height: int
) -> str:
    return (
        f"Viewport: {viewport_width} x {viewport_height} pixels.\n\n"
        f"Find this element:\n{visual_description}\n\n"
        f"Return the centre coordinates."
    )


# --------------------------------------------------------------------------
# Negative probe: discovering business outcomes
# --------------------------------------------------------------------------

OUTCOME_PROBE_SYSTEM = """\
A recorded flow was replayed with a deliberately invalid input, to find out how \
the application reports "nothing matched".

Look at the page state and decide whether the application is showing a \
recognisable *business outcome* -- a valid answer that simply is not the happy \
path, such as "No Records Found" or "Invalid credentials".

This is not an error. It is an answer, and the replay engine needs to return it \
cleanly rather than crashing.

If you find one:
- `detect_text` must be the exact on-screen text, copied verbatim. It becomes a \
literal match at replay time, so a paraphrase will silently never match. Choose \
the shortest distinctive phrase, not a whole sentence that might include the \
input value.
- `outcome_code` is SCREAMING_SNAKE_CASE, e.g. EMPLOYEE_NOT_FOUND.

If the page shows a crash, a stack trace, or nothing conclusive, set \
`detected: false`. A missing outcome entry is recoverable; a wrong one makes \
the engine report a failure as a valid answer.
"""


def outcome_probe_user_message(
    *, goal: str, bad_input: dict[str, str], snapshot: str, step_id: int
) -> str:
    supplied = ", ".join(f"{k}={v!r}" for k, v in bad_input.items())
    return (
        f"# Original goal\n{goal}\n\n"
        f"# Deliberately invalid input\n{supplied}\n\n"
        f"# Reached step\n{step_id}\n\n"
        f"# Page state\n{snapshot}\n\n"
        f"Is the application reporting a business outcome?"
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def summarise_artifact(artifact: Artifact) -> str:
    """One-line-per-step summary, used in escalation context for the operator."""
    lines = [f"{artifact.capability_id} v{artifact.version} - {artifact.description}"]
    for step in artifact.steps:
        flag = " [fragile]" if step.fragile else ""
        lines.append(
            f"  {step.step_id}. {step.action.value}: {step.description}{flag}"
        )
    return "\n".join(lines)
