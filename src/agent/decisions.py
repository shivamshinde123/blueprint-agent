"""Structured shapes the model returns during discovery.

Every one of these is sent to the gateway as a strict JSON schema, so a
response either validates or raises. That matters more here than almost
anywhere else in the system: a malformed decision mid-discovery means either a
crash halfway through a live flow, or a repair heuristic quietly guessing what
the model meant and acting on the guess.

Parameterisation is handled by *provenance*, not string matching: the model is
told which parameters exist and emits ``{{param_name}}`` directly in a value it
takes from one. The recorder therefore never has to search the artifact for a
literal that looks like a secret — which would rewrite unrelated fields that
happen to contain the same text. See PLAN.md §11 C7.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionAction(str, Enum):
    CLICK = "click"
    FILL = "fill"
    NAVIGATE = "navigate"
    EXTRACT = "extract"
    #: Nothing sensible left to do. Triggers escalation signal 3.
    GIVE_UP = "give_up"


class LocatorMethodName(str, Enum):
    GET_BY_ROLE = "get_by_role"
    GET_BY_LABEL = "get_by_label"
    GET_BY_PLACEHOLDER = "get_by_placeholder"
    GET_BY_TEXT = "get_by_text"


#: Sentinel for "not supplied", used instead of ``None`` throughout the
#: decision models.
#:
#: Every ``str | None`` field becomes a union in the emitted JSON schema, and
#: Anthropic caps a strict schema at 16 union-typed parameters -- "exponential
#: compilation cost". These models nest a locator inside a decision *and*
#: inside each extraction, so optional fields multiply fast: adding one more
#: pushed the count to 17 and every discovery call started failing with a 400.
#:
#: Empty string means absent. It costs one normalisation step at the boundary
#: and keeps the schema flat.
UNSET = ""
#: Same idea for the one optional integer.
UNSET_INDEX = -1


class ProposedLocator(Base):
    """How the model believes the target element can be found.

    Kept flat rather than nested per method: strict JSON schema mode handles
    flat fields far more reliably than deep unions, and the recorder converts
    this into the artifact's richer locator shape.
    """

    method: LocatorMethodName
    #: Required for get_by_role, e.g. "button", "textbox", "link".
    role: str = Field(default=UNSET, description="ARIA role, for get_by_role")
    #: Accessible name. May contain {{param}} for labels that vary per run.
    name: str = Field(default=UNSET, description="Accessible name or label")
    #: Literal text, for get_by_text and get_by_placeholder.
    value: str = Field(default=UNSET, description="Text or placeholder value")
    #: Address the element by the shape of its text, with get_by_text.
    pattern: str = Field(
        default=UNSET,
        description=(
            "With get_by_text: a regex matching the element's text by SHAPE. "
            "Use when a value has no label, no role, and the only text "
            r"identifying it is the value itself -- '\$[\d,.]+' finds a price "
            "on any product page. Never write the value: that only ever finds "
            "this run's answer. Empty string if not used."
        ),
    )
    #: Which match to take, when position is more stable than the name.
    nth: int = Field(
        default=UNSET_INDEX,
        description=(
            "0-based index to select by position instead of by name, or -1 for "
            "none. Use when the element's text comes from this run's data -- "
            "the first suggestion in a typeahead list, the first row of a "
            "results table -- so the locator stays correct for a different "
            "input."
        ),
    )


class ProposedExtraction(Base):
    """One field to read off the page."""

    output_key: str = Field(description="snake_case key for the returned value")
    locator: ProposedLocator
    extract_method: str = Field(description="get_value, text_content, or inner_text")
    pattern: str = Field(
        default=UNSET,
        description=(
            "Regex isolating the value inside the element's text, for when one "
            "element holds several values. Describe the SHAPE of the value, "
            r"never the value itself: '\$[\d,.]+' matches any price; "
            r"'\$29\.99' matches only this run's answer and is rejected. "
            "At most one capture group -- the group is taken if present, "
            "otherwise the whole match. Empty string if not needed."
        ),
    )
    expected_type: str = Field(description="string, integer, currency, or boolean")


class AgentDecision(Base):
    """One observe -> decide -> act turn."""

    reasoning: str = Field(
        description="Why this action, in one or two sentences. Recorded as evidence."
    )
    action: DecisionAction

    #: Populated for click / fill / extract. Null for navigate and give_up.
    #: The one union the schema keeps -- an object has no natural empty value.
    locator: ProposedLocator | None = None

    #: For fill. Use {{param_name}} when the value comes from an input
    #: parameter -- never the literal value. Empty string when not filling.
    value: str = UNSET

    #: For navigate. Empty string when not navigating.
    url: str = UNSET

    #: For extract. One entry per field the goal asks for; empty otherwise.
    extractions: list[ProposedExtraction] = Field(default_factory=list)

    #: Human-readable description of the element, stored as the Layer 2
    #: fallback hint so it can be re-located visually later.
    visual_description: str = Field(
        default=UNSET,
        description="What the element looks like and where it sits on screen",
    )

    #: Risk classification. The model is instructed to err high; the replay
    #: engine pauses for a human on high and critical.
    risk_level: str = Field(
        default="safe", description="safe, low, high, or critical"
    )

    #: True once the goal is met and every required output has been extracted.
    goal_achieved: bool = False

    #: True when the model cannot see a safe way forward. Escalation signal 3.
    stuck: bool = False

    #: Why it is stuck, shown to the human operator.
    stuck_reason: str = UNSET


class VisionLocate(Base):
    """Layer 2: where an element is, in viewport pixels.

    Coordinates are viewport-relative because that is what ``mouse.click``
    consumes. The screenshot handed to the model is deliberately viewport-only
    for the same reason. See PLAN.md §11 C1.
    """

    found: bool = Field(description="False if the element is not visible on screen")
    x: int = Field(default=0, description="Centre x, in viewport pixels")
    y: int = Field(default=0, description="Centre y, in viewport pixels")
    confidence: str = Field(default="medium", description="low, medium, or high")
    visual_description: str = Field(
        default="", description="What was identified, for the evidence log"
    )
    reasoning: str = Field(default="", description="How the element was identified")


class OutcomeProbe(Base):
    """What a deliberately-invalid run showed on screen.

    A successful discovery run never encounters "no results", so business
    outcomes cannot be observed from the happy path. The negative probe runs
    the recorded flow once with a bad input and asks the model to name what it
    sees. See PLAN.md §11 C6.
    """

    detected: bool = Field(description="True if a recognisable outcome message appeared")
    outcome_code: str = Field(
        default="", description="SCREAMING_SNAKE_CASE, e.g. EMPLOYEE_NOT_FOUND"
    )
    detect_text: str = Field(
        default="",
        description="Exact on-screen text to match at replay time, verbatim",
    )
    outcome_message: str = Field(default="", description="Human-readable explanation")
    at_step_id: int = Field(default=0, description="Step where the message appeared")
