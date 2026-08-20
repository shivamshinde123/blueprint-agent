"""Does this artifact work for inputs it was not recorded with?

This is the property the whole system exists to deliver, and it is the one that
nothing else checks. An artifact can have a valid schema, resolvable locators,
passing checkpoints and correct cross-references while still being worthless:
it works for the exact values it was recorded with and silently returns the
wrong answer for anything else.

Three real examples, all from live runs, all of which passed every other check:

* ``page_contains_text: "Anderson"`` as a checkpoint after a search -- the
  surname that search happened to return. Passes for one employee, fails for
  every other.
* ``get_by_role(option, name="Peter Mac Anderson")`` to click a typeahead
  suggestion -- the suggestion text for one specific record.
* ``get_by_text("$29.99")`` to extract a price. Circular: it finds the element
  only when the answer is already known, so the capability reports $29.99 for
  every product in the catalogue. This one is the worst, because replay
  *succeeds* -- there is no failure anywhere to notice.

The rule is simple enough to state in one line, and that is the point:

    No locator and no checkpoint may contain a value that came from this run.

``{{template}}`` references are the correct way to depend on an input and are
never violations. The check runs at record time, with the values still in
memory -- deliberately, so nothing has to be persisted. Storing the recorded
values in the artifact to re-check later would mean writing a person's date of
birth into a file that ships in the evidence folder.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from src.artifact.schema import TEMPLATE, Artifact, Condition, Locators

#: Values shorter than this are ignored. A two-character value collides with
#: ordinary page text ("ID", "OK") and would flag everything.
MIN_SIGNIFICANT_LENGTH = 3

#: Values that are pure structure rather than data. A capability legitimately
#: navigates to a path or asserts a status word that happens to match an input.
_STRUCTURAL = re.compile(r"^[/#?.\s-]*$")


@dataclass(frozen=True, slots=True)
class Violation:
    """One place an artifact embedded a value from its recording run."""

    where: str
    text: str
    value_name: str

    def __str__(self) -> str:
        return (
            f"{self.where}: {self.text!r} contains the value recorded for "
            f"{self.value_name!r}"
        )


def _tokens(text: str) -> set[str]:
    """Significant words, for comparing values the application reworded."""
    return {
        word
        for word in re.split(r"[^0-9a-z]+", text.lower())
        if len(word) >= MIN_SIGNIFICANT_LENGTH
    }


def embeds(text: str | None, value: str) -> bool:
    """Does *text* hard-code *value* rather than referencing it?

    The shared predicate. Used both as a backstop at assembly and for immediate
    feedback during recording, so there is exactly one definition of the rule.

    Substring matching alone is not enough, and the gap is not hypothetical.
    A real recording used ``option "Peter Mac Anderson"`` to click a typeahead
    suggestion for the input ``"Peter Anderson"`` -- the application had
    interpolated a middle name. Neither string contains the other, so a plain
    substring test reports no problem while the locator is hard-wired to one
    record. Token overlap catches the family of cases where an application
    reformats, reorders, or pads the value it was given.
    """
    if not text or not value:
        return False

    needle = value.strip().lower()
    if len(needle) < MIN_SIGNIFICANT_LENGTH or _STRUCTURAL.match(needle):
        return False

    # A template is the correct way to depend on an input. Strip the references
    # out before looking, so `{{employee_name}}` never reads as the literal.
    haystack = TEMPLATE.sub("", text).strip().lower()
    if len(haystack) < MIN_SIGNIFICANT_LENGTH:
        return False

    if needle in haystack or haystack in needle:
        return True

    # Every significant word of the value present in the text, or the reverse.
    value_words, text_words = _tokens(needle), _tokens(haystack)
    if not value_words or not text_words:
        return False
    return value_words <= text_words or text_words <= value_words


def locator_texts(locators: Locators | None) -> Iterator[tuple[str, str]]:
    """Every human-supplied string a locator matches on."""
    if locators is None:
        return
    for method in locators.primary.methods:
        for field_name, text in (("name", method.name), ("value", method.value)):
            if text:
                yield f"{method.method.value}.{field_name}", text
    if locators.fallback and locators.fallback.visual_description:
        # The description is a hint for a vision model, not a matcher, so a
        # name appearing here is a much weaker signal -- but still worth
        # surfacing, since it is what the fallback searches for.
        yield "fallback.visual_description", locators.fallback.visual_description


def condition_texts(condition: Condition | None) -> Iterator[tuple[str, str]]:
    if condition is None:
        return
    if condition.value:
        yield f"{condition.condition.value}.value", condition.value
    yield from locator_texts(condition.locators)


def check_reusable(
    artifact: Artifact,
    recorded_values: dict[str, Any],
    *,
    sensitive: set[str] | None = None,
    include_fallback_descriptions: bool = False,
) -> list[Violation]:
    """Find every place the artifact hard-codes a value from its own run.

    ``recorded_values`` is what the run actually used and produced: the input
    parameters plus the values the extractions returned. Nothing here is
    written to disk.
    """
    values = {
        name: str(value).strip()
        for name, value in recorded_values.items()
        if value is not None and str(value).strip()
    }
    if not values:
        return []

    secret_names = sensitive or set()
    violations: list[Violation] = []

    def scan(where: str, pairs: Iterator[tuple[str, str]]) -> None:
        for field_name, text in pairs:
            if field_name == "fallback.visual_description" and not include_fallback_descriptions:
                continue
            for value_name, value in values.items():
                if not embeds(text, value):
                    continue
                # A locator embedding a credential is the worst case of this
                # bug, and printing the offending text would write the
                # credential into a log. Name the field, never the secret.
                shown = "***REDACTED***" if value_name in secret_names else text
                violations.append(
                    Violation(f"{where} ({field_name})", shown, value_name)
                )

    for step in artifact.steps:
        scan(f"step {step.step_id} locator", locator_texts(step.locators))
        scan(f"step {step.step_id} pre-condition", condition_texts(step.pre_condition))
        scan(f"step {step.step_id} checkpoint", condition_texts(step.post_condition))

        for extraction in step.extractions or []:
            scan(
                f"step {step.step_id} extraction {extraction.output_key!r}",
                locator_texts(extraction.locators),
            )

    for outcome in artifact.business_outcomes:
        scan(f"business outcome {outcome.outcome_code}", condition_texts(outcome.detect))

    return violations


def assert_reusable(
    artifact: Artifact,
    recorded_values: dict[str, Any],
    *,
    sensitive: set[str] | None = None,
) -> None:
    """Raise unless the artifact is free of its own run's data."""
    violations = check_reusable(artifact, recorded_values, sensitive=sensitive)
    if not violations:
        return
    raise NotReusable(violations)


class NotReusable(RuntimeError):
    """The artifact only works for the values it was recorded with."""

    def __init__(self, violations: list[Violation]) -> None:
        self.violations = violations
        detail = "\n  - ".join(str(v) for v in violations)
        super().__init__(
            "this recording embeds values from its own run, so it would only "
            "work for the input it was recorded with:\n  - "
            + detail
            + "\n\nLocators and checkpoints must identify elements by something "
            "stable -- a label, a role, a position, or a {{parameter}} "
            "reference -- never by the data this run happened to see."
        )
