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


class ProposedLocator(Base):
    """How the model believes the target element can be found.

    Kept flat rather than nested per method: strict JSON schema mode handles
    flat optional fields far more reliably than deep unions, and the recorder
    converts this into the artifact's richer locator shape.
    """

    method: LocatorMethodName
    #: Required for get_by_role, e.g. "button", "textbox", "link".
    role: str | None = Field(default=None, description="ARIA role, for get_by_role")
    #: Accessible name. May contain {{param}} for labels that vary per run.
    name: str | None = Field(default=None, description="Accessible name or label")
    #: Literal text, for get_by_text and get_by_placeholder.
    value: str | None = Field(default=None, description="Text or placeholder value")


class ProposedExtraction(Base):
    """One field to read off the page."""

    output_key: str = Field(description="snake_case key for the returned value")
    locator: ProposedLocator
    extract_method: str = Field(description="get_value, text_content, or inner_text")
    expected_type: str = Field(description="string, integer, currency, or boolean")


class AgentDecision(Base):
    """One observe -> decide -> act turn."""

    reasoning: str = Field(
        description="Why this action, in one or two sentences. Recorded as evidence."
    )
    action: DecisionAction

    #: Populated for click / fill / extract. Null for navigate and give_up.
    locator: ProposedLocator | None = None

    #: For fill. Use {{param_name}} when the value comes from an input
    #: parameter -- never the literal value.
    value: str | None = None

    #: For navigate.
    url: str | None = None

    #: For extract. One entry per field the goal asks for.
    extractions: list[ProposedExtraction] | None = None

    #: Human-readable description of the element, stored as the Layer 2
    #: fallback hint so it can be re-located visually later.
    visual_description: str | None = Field(
        default=None,
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
    stuck_reason: str | None = None


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
