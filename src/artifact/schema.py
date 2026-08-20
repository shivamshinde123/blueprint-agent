"""Typed artifact schema for the Blueprint Agent.

The artifact is the central design object of this system: a discovery run writes
one, and the replay engine executes it with no LLM in the decision loop. Because
replay is mechanical, *every* invariant the engine relies on has to be checked
here, at load time, before a browser is ever opened.

Design notes worth knowing before editing this file:

* ``extra="forbid"`` everywhere. A typo'd field name is a silent no-op in a
  permissive schema — the engine would just never see the value. We reject it.
* ``sensitive`` is validated as a *strict* bool. Pydantic would happily coerce
  the string ``"true"``, and a credential that is only "truthy" would slip past
  redaction. See :func:`InputParameter.check_sensitive_is_strict_bool`.
* The ``action`` field name appears in four contexts with four disjoint value
  sets (step / error map / interstitial / self-healing). The latter two are
  deliberately named ``dismiss_action`` and ``healing_action`` so the sets can
  never be confused. See PLAN.md §4.12.
* Cross-reference checks (``step_N`` keys, output keys, ``{{param}}`` templates)
  live in :meth:`Artifact.check_cross_references`. A dangling ``step_9`` key
  would otherwise fall through to ``_default`` and silently disable the
  per-step handling you designed. See PLAN.md §11 C13.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

# --------------------------------------------------------------------------
# Format patterns
# --------------------------------------------------------------------------

SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
SCREAMING_SNAKE_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STEP_KEY = re.compile(r"^step_(\d+)$")

#: ``{{ param_name }}`` — the replay engine substitutes input parameters before
#: resolving a locator or filling a field.
TEMPLATE = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")

DEFAULT_ERROR_KEY = "_default"


def template_params(text: str | None) -> set[str]:
    """Return the set of ``{{param}}`` names referenced in *text*."""
    if not text:
        return set()
    return set(TEMPLATE.findall(text))


class Base(BaseModel):
    """Shared config: reject unknown fields, validate on assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------
# Enums
# --------------------------------------------------------------------------


class SurfaceType(str, Enum):
    MODERN_WEB = "modern_web"
    LEGACY_WEB = "legacy_web"
    DESKTOP_WINDOWS = "desktop_windows"
    DESKTOP_MAC = "desktop_mac"


class RiskLevel(str, Enum):
    """``HIGH`` and ``CRITICAL`` always escalate before executing."""

    SAFE = "safe"
    LOW = "low"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def requires_human_confirmation(self) -> bool:
        return self in (RiskLevel.HIGH, RiskLevel.CRITICAL)


class ActionType(str, Enum):
    CLICK = "click"
    FILL = "fill"
    NAVIGATE = "navigate"
    EXTRACT = "extract"


class RecordedBy(str, Enum):
    AGENT = "agent"
    HUMAN_OPERATOR = "human_operator"


class ParameterType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"


class LocatorStrategy(str, Enum):
    ACCESSIBILITY_TREE = "accessibility_tree"
    SCREENSHOT = "screenshot"


class AccessibilityMethod(str, Enum):
    """Priority order, most specific first. See PLAN.md §4.5."""

    GET_BY_ROLE = "get_by_role"
    GET_BY_LABEL = "get_by_label"
    GET_BY_PLACEHOLDER = "get_by_placeholder"
    GET_BY_TEXT = "get_by_text"
    #: The control sitting in the same field group as a label with this text.
    #:
    #: Last resort, and the one that rescues real applications. A caption is
    #: only reachable by ``get_by_label`` when the markup actually wires it to
    #: its control, and plenty of apps never do: OrangeHRM renders a visible
    #: "Date of Birth" label whose input has no accessible name at all, so
    #: every other method returns nothing while a human reads it instantly.
    #:
    #: Still identity-based rather than positional -- it keys on the visible
    #: caption, which is what a person reads and what survives a redesign far
    #: better than coordinates do.
    GET_BY_FIELD_LABEL = "get_by_field_label"


class ConditionType(str, Enum):
    URL_CONTAINS = "url_contains"
    URL_NOT_CONTAINS = "url_not_contains"
    ELEMENT_VISIBLE = "element_visible"
    PAGE_CONTAINS_TEXT = "page_contains_text"
    ELEMENT_HAS_VALUE = "element_has_value"
    ALL_EXTRACTIONS_NON_EMPTY = "all_extractions_non_empty"


#: Conditions that target an element and therefore require ``locators``.
_ELEMENT_CONDITIONS = frozenset(
    {ConditionType.ELEMENT_VISIBLE, ConditionType.ELEMENT_HAS_VALUE}
)
#: Conditions that match a string and therefore require ``value``.
_VALUE_CONDITIONS = frozenset(
    {
        ConditionType.URL_CONTAINS,
        ConditionType.URL_NOT_CONTAINS,
        ConditionType.PAGE_CONTAINS_TEXT,
    }
)


class OnFail(str, Enum):
    HARD_FAILURE = "hard_failure"
    RETRY = "retry"
    ESCALATE_HUMAN = "escalate_human"
    #: Legal only inside ``session_recovery.recovery_post_condition``.
    ON_RECOVERY_FAIL = "on_recovery_fail"


class ErrorCategory(str, Enum):
    RECOVERABLE = "recoverable"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"


class ErrorAction(str, Enum):
    RETRY = "retry"
    STOP = "stop"
    ESCALATE_HUMAN = "escalate_human"
    RETURN_OUTCOME = "return_outcome"
    RE_LOGIN = "re_login"


class ErrorTypeKey(str, Enum):
    """Fixed set. Never invent a custom error-type key."""

    ELEMENT_NOT_FOUND = "element_not_found"
    CHECKPOINT_FAILED = "checkpoint_failed"
    EXTRACTION_EMPTY = "extraction_empty"
    WRONG_PAGE_STATE = "wrong_page_state"
    TIMEOUT = "timeout"
    SESSION_EXPIRED = "session_expired"


class ExtractMethod(str, Enum):
    GET_VALUE = "get_value"
    TEXT_CONTENT = "text_content"
    INNER_TEXT = "inner_text"


class ExpectedType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    CURRENCY = "currency"
    BOOLEAN = "boolean"


class ReplayMode(str, Enum):
    """``STRICT`` is the default and the mode used for graded evidence runs.

    ``ASSISTED`` permits a bounded number of vision calls for *locator
    resolution only* — the step sequence still comes entirely from the
    artifact, so the LLM is never in the decision loop. See PLAN.md §11 C10.
    """

    STRICT = "strict"
    ASSISTED = "assisted"


class DismissAction(str, Enum):
    CLICK = "click"
    WAIT_FOR_HIDDEN = "wait_for_hidden"


class OnTimeout(str, Enum):
    HARD_FAILURE = "hard_failure"
    CONTINUE = "continue"


class AfterRecovery(str, Enum):
    RESTART_FROM_CURRENT_STEP = "restart_from_current_step"
    RESTART_FROM_BEGINNING = "restart_from_beginning"


class OnRecoveryFail(str, Enum):
    ESCALATE_HUMAN = "escalate_human"
    HARD_FAILURE = "hard_failure"


class HealingAction(str, Enum):
    #: Writes a sidecar patch — never mutates the versioned artifact (C8).
    UPDATE_ARTIFACT = "update_artifact"
    FLAG_ONLY = "flag_only"


class FlagStepAs(str, Enum):
    HEALED = "healed"
    NEEDS_REVIEW = "needs_review"


class ResultType(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILURE = "failure"


# --------------------------------------------------------------------------
# Capability contract
# --------------------------------------------------------------------------


class InputParameter(Base):
    type: ParameterType
    required: bool
    #: Credentials, tokens, PII. Drives redaction across artifact, logs, and
    #: evidence. Validated as a strict bool — see the class docstring rationale.
    sensitive: bool = False
    description: str | None = None
    example: Any | None = None

    @field_validator("sensitive", "required", mode="before")
    @classmethod
    def check_sensitive_is_strict_bool(cls, v: Any) -> Any:
        """Reject ``"true"``, ``"yes"``, ``1`` — only real booleans pass.

        Pydantic coerces truthy strings by default. A credential marked
        ``sensitive: "true"`` would still be a bool after coercion, but the
        habit of accepting loose values is how ``sensitive: "no"`` (truthy!)
        ends up disabling redaction on a password.
        """
        if not isinstance(v, bool):
            raise ValueError(
                f"must be a JSON boolean (true/false), got {type(v).__name__}: {v!r}"
            )
        return v


class Target(Base):
    app_name: str
    url: str
    surface_type: SurfaceType


class Viewport(Base):
    width: int
    height: int


class Provenance(Base):
    """Ties an artifact back to the run that produced it (PLAN.md §11 C16)."""

    source_run_id: str
    model_id: str | None = None
    #: Hash of the steps array, so a reviewer can tell whether a flow changed.
    steps_hash: str | None = None
    notes: str | None = None


# --------------------------------------------------------------------------
# Locators
# --------------------------------------------------------------------------


class AccessibilityLocatorMethod(Base):
    method: AccessibilityMethod
    role: str | None = None
    #: Accessible name. May contain ``{{param}}`` templates for dynamic labels.
    name: str | None = None
    value: str | None = None
    #: Which match to take when the name is genuinely ambiguous.
    #:
    #: Real pages repeat labels: OrangeHRM's PIM search exposes *two* inputs
    #: placeholdered "Type for hints..." (Employee Name and Supervisor Name),
    #: both visible. Leaving that unresolved means either refusing the step or
    #: silently guessing on every replay. Recording the index makes the choice
    #: explicit, reviewable, and identical on every run.
    nth: int | None = None

    @model_validator(mode="after")
    def check_required_fields_for_method(self) -> AccessibilityLocatorMethod:
        if self.method is AccessibilityMethod.GET_BY_ROLE:
            if not self.role:
                raise ValueError("get_by_role requires 'role'")
            if not self.name and self.nth is None:
                raise ValueError(
                    "get_by_role requires 'name', or an explicit 'nth' — a "
                    "role on its own matches whatever happens to come first, "
                    "which is not a decision anyone recorded"
                )
        elif self.method is AccessibilityMethod.GET_BY_TEXT:
            if not self.value:
                raise ValueError("get_by_text requires 'value'")
        elif not (self.name or self.value):
            raise ValueError(f"{self.method.value} requires 'name' or 'value'")
        return self


class PrimaryLocator(Base):
    strategy: LocatorStrategy = LocatorStrategy.ACCESSIBILITY_TREE
    methods: list[AccessibilityLocatorMethod] = []
    #: ``False`` on fragile steps: the a11y tree gave nothing useful during
    #: discovery, so replay should not waste time trying it.
    available: bool = True

    @field_validator("strategy")
    @classmethod
    def check_strategy(cls, v: LocatorStrategy) -> LocatorStrategy:
        if v is not LocatorStrategy.ACCESSIBILITY_TREE:
            raise ValueError("primary locator strategy must be accessibility_tree")
        return v

    @model_validator(mode="after")
    def check_methods_match_availability(self) -> PrimaryLocator:
        if self.available and not self.methods:
            raise ValueError(
                "primary locator is marked available but lists no methods; "
                "set available=false for a fragile step"
            )
        if not self.available and self.methods:
            raise ValueError(
                "primary locator is marked unavailable but lists methods; "
                "remove the methods or set available=true"
            )
        return self


class ScreenshotCoordinates(Base):
    x: int
    y: int


class ScreenshotRegion(Base):
    x: int
    y: int
    width: int
    height: int


class ScreenshotLocator(Base):
    """Layer 2 fallback.

    Coordinates are **viewport-relative**, captured from a viewport-only
    screenshot. A full-page screenshot stitches the whole scrollable document,
    so its y-values do not map to ``mouse.click`` and would silently click the
    wrong element. ``scroll_y`` records the scroll offset the coordinates were
    captured at so replay can restore it. See PLAN.md §11 C1.
    """

    strategy: LocatorStrategy = LocatorStrategy.SCREENSHOT
    coordinates: ScreenshotCoordinates | None = None
    region: ScreenshotRegion | None = None
    #: Scroll offset in effect when the coordinates were captured.
    scroll_y: int = 0
    #: Viewport the coordinates are valid for. Replay refuses to use
    #: coordinates captured at a different viewport.
    viewport: Viewport
    #: Natural-language description used to re-locate the element visually if
    #: the stored coordinates produce an unexpected result.
    visual_description: str

    @field_validator("strategy")
    @classmethod
    def check_strategy(cls, v: LocatorStrategy) -> LocatorStrategy:
        if v is not LocatorStrategy.SCREENSHOT:
            raise ValueError("fallback locator strategy must be screenshot")
        return v

    @model_validator(mode="after")
    def check_has_a_target(self) -> ScreenshotLocator:
        if self.coordinates is None and self.region is None:
            raise ValueError("screenshot locator needs 'coordinates' or 'region'")
        return self


class Locators(Base):
    primary: PrimaryLocator
    #: Populated for *every* step, including ones the a11y tree resolved —
    #: ``bounding_box()`` gives the centre point for free once Playwright holds
    #: the element handle, so the safety net costs no vision call (C4).
    fallback: ScreenshotLocator | None = None

    @property
    def referenced_params(self) -> set[str]:
        params: set[str] = set()
        for method in self.primary.methods:
            params |= template_params(method.name)
            params |= template_params(method.value)
        return params


# --------------------------------------------------------------------------
# Conditions
# --------------------------------------------------------------------------


class Condition(Base):
    condition: ConditionType
    value: str | None = None
    locators: Locators | None = None
    timeout_ms: int | None = None
    on_fail: OnFail = OnFail.HARD_FAILURE
    message: str | None = None

    @model_validator(mode="after")
    def check_condition_operands(self) -> Condition:
        if self.condition in _ELEMENT_CONDITIONS and self.locators is None:
            raise ValueError(f"{self.condition.value} requires 'locators'")
        if self.condition in _VALUE_CONDITIONS and not self.value:
            raise ValueError(f"{self.condition.value} requires 'value'")
        if self.condition is ConditionType.ALL_EXTRACTIONS_NON_EMPTY and (
            self.value or self.locators
        ):
            raise ValueError(
                "all_extractions_non_empty takes neither 'value' nor 'locators'"
            )
        return self

    @property
    def referenced_params(self) -> set[str]:
        params = template_params(self.value)
        if self.locators:
            params |= self.locators.referenced_params
        return params


# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------


class Extraction(Base):
    output_key: str
    locators: Locators
    extract_method: ExtractMethod
    expected_type: ExpectedType
    required: bool = True

    @field_validator("output_key")
    @classmethod
    def check_snake_case(cls, v: str) -> str:
        if not SNAKE_CASE.match(v):
            raise ValueError(
                f"output_key must be snake_case (no spaces, hyphens, or "
                f"uppercase): {v!r}"
            )
        return v


class Step(Base):
    step_id: int
    action: ActionType
    description: str
    #: ``True`` -> skip the accessibility layer entirely, go straight to Layer 2.
    fragile: bool = False
    fragile_reason: str | None = None
    risk_level: RiskLevel
    #: Explicitly ``null`` when no pre-condition makes sense, never omitted —
    #: so a reader can tell the omission was deliberate.
    pre_condition: Condition | None = None
    locators: Locators | None = None
    #: Only on ``navigate`` steps.
    url: str | None = None
    #: Only on ``fill`` steps. May contain ``{{param}}`` templates.
    value: str | None = None
    #: Only on ``extract`` steps.
    extractions: list[Extraction] | None = None
    #: Required on every step. Never proceed without confirming the action worked.
    post_condition: Condition
    #: Settle time *after* post_condition passes. Not a blind sleep — only use
    #: when waiting on a condition is genuinely insufficient.
    step_wait_ms: int | None = None

    @field_validator("step_id")
    @classmethod
    def check_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError(f"step_id must be >= 1, got {v}")
        return v

    @model_validator(mode="after")
    def check_shape_for_action(self) -> Step:
        action = self.action

        if self.fragile and not self.fragile_reason:
            raise ValueError(
                f"step {self.step_id}: fragile=true requires 'fragile_reason' "
                f"explaining why the accessibility tree was unusable here"
            )
        if not self.fragile and self.fragile_reason:
            raise ValueError(
                f"step {self.step_id}: 'fragile_reason' set but fragile=false"
            )

        if action is ActionType.NAVIGATE:
            if not self.url:
                raise ValueError(f"step {self.step_id}: navigate requires 'url'")
            if self.locators is not None:
                raise ValueError(
                    f"step {self.step_id}: navigate takes no 'locators'"
                )
        else:
            if self.url is not None:
                raise ValueError(
                    f"step {self.step_id}: 'url' is only valid on navigate steps"
                )

        if action is ActionType.FILL:
            if self.value is None:
                raise ValueError(f"step {self.step_id}: fill requires 'value'")
            if self.locators is None:
                raise ValueError(f"step {self.step_id}: fill requires 'locators'")
        elif action is not ActionType.NAVIGATE and self.value is not None:
            raise ValueError(
                f"step {self.step_id}: 'value' is only valid on fill steps"
            )

        if action is ActionType.CLICK and self.locators is None:
            raise ValueError(f"step {self.step_id}: click requires 'locators'")

        if action is ActionType.EXTRACT:
            # Extract steps carry per-extraction locators instead of one
            # top-level locator, because a single step can pull several fields.
            if self.locators is not None:
                raise ValueError(
                    f"step {self.step_id}: extract steps must not set top-level "
                    f"'locators' — use per-extraction locators"
                )
            if not self.extractions:
                raise ValueError(
                    f"step {self.step_id}: extract requires a non-empty "
                    f"'extractions' list"
                )
        elif self.extractions is not None:
            raise ValueError(
                f"step {self.step_id}: 'extractions' is only valid on extract steps"
            )

        if self.fragile and self.locators is not None:
            if self.locators.primary.available:
                raise ValueError(
                    f"step {self.step_id}: fragile steps must set "
                    f"locators.primary.available=false"
                )
            if self.locators.fallback is None:
                raise ValueError(
                    f"step {self.step_id}: fragile steps require a screenshot "
                    f"fallback — there is no other way to resolve the element"
                )

        # PLAN.md §11 C17: a navigate step runs before any page exists.
        if self.step_id == 1 and self.pre_condition is not None:
            raise ValueError(
                "step 1 cannot have a pre_condition — no page exists yet; "
                "set it explicitly to null"
            )

        return self

    @property
    def referenced_params(self) -> set[str]:
        params = template_params(self.value) | template_params(self.url)
        if self.locators:
            params |= self.locators.referenced_params
        if self.pre_condition:
            params |= self.pre_condition.referenced_params
        params |= self.post_condition.referenced_params
        for extraction in self.extractions or []:
            params |= extraction.locators.referenced_params
        return params


# --------------------------------------------------------------------------
# Replay configuration
# --------------------------------------------------------------------------


class BrowserConfig(Base):
    """Everything that must match between discovery and replay.

    Pinning width and height alone is not enough: device pixel ratio, headless
    vs headful rendering, locale-driven text length, and CSS animations all
    move pixels. See PLAN.md §11 C2.
    """

    viewport: Viewport = Viewport(width=1280, height=720)
    device_scale_factor: int = 1
    is_mobile: bool = False
    headless: bool = False
    locale: str = "en-US"
    timezone_id: str = "UTC"
    reduced_motion: str = "reduce"
    enforce_strictly: bool = True


class ReplayConfig(Base):
    browser: BrowserConfig = BrowserConfig()
    mode: ReplayMode = ReplayMode.STRICT
    default_timeout_ms: int = 8000
    #: Deliberately much shorter than ``default_timeout_ms``. Interstitials are
    #: *probed* before every step; at the action timeout, three interstitials
    #: across eight steps would add minutes of pure waiting. See PLAN.md §11 C9.
    interstitial_probe_timeout_ms: int = 250
    max_retries_per_step: int = 3
    retry_wait_ms: int = 1000
    #: Assisted mode only: number of *failing steps* that may consume a vision
    #: call across the whole run, not screenshots per step.
    max_llm_calls_per_replay: int = 3

    @model_validator(mode="after")
    def check_probe_timeout_is_short(self) -> ReplayConfig:
        if self.interstitial_probe_timeout_ms >= self.default_timeout_ms:
            raise ValueError(
                "interstitial_probe_timeout_ms must be well below "
                "default_timeout_ms; probing every interstitial at the full "
                "action timeout dominates replay runtime (PLAN.md C9)"
            )
        return self


# --------------------------------------------------------------------------
# Session recovery
# --------------------------------------------------------------------------


class RecoveryStep(Base):
    """A step in the re-login mini-script.

    Recovery steps deliberately omit ``step_id``, ``risk_level``, ``fragile``,
    and per-step conditions: they run as a single atomic unit guarded by one
    shared ``recovery_post_condition``, are not part of the main flow, and are
    never referenced by the error map.
    """

    action: ActionType
    locators: Locators | None = None
    url: str | None = None
    value: str | None = None

    @property
    def referenced_params(self) -> set[str]:
        params = template_params(self.value) | template_params(self.url)
        if self.locators:
            params |= self.locators.referenced_params
        return params


class SessionRecovery(Base):
    detect_conditions: list[Condition]
    recovery_steps: list[RecoveryStep]
    recovery_post_condition: Condition
    after_recovery: AfterRecovery = AfterRecovery.RESTART_FROM_CURRENT_STEP
    max_recovery_attempts: int = 1
    on_recovery_fail: OnRecoveryFail = OnRecoveryFail.ESCALATE_HUMAN

    @model_validator(mode="after")
    def check_post_condition(self) -> SessionRecovery:
        if self.recovery_post_condition.on_fail is not OnFail.ON_RECOVERY_FAIL:
            raise ValueError(
                "recovery_post_condition.on_fail must be 'on_recovery_fail'"
            )
        return self


# --------------------------------------------------------------------------
# Interstitials
# --------------------------------------------------------------------------


class InterstitialDismiss(Base):
    #: Named ``dismiss_action`` (not ``action``) so its value set can never be
    #: confused with a step action. See PLAN.md §4.12.
    dismiss_action: DismissAction
    locators: Locators | None = None
    timeout_ms: int | None = None
    on_timeout: OnTimeout = OnTimeout.CONTINUE

    @model_validator(mode="after")
    def check_locators_present(self) -> InterstitialDismiss:
        if self.locators is None:
            raise ValueError("interstitial dismiss requires 'locators'")
        return self


class Interstitial(Base):
    name: str
    detect: Condition
    dismiss: InterstitialDismiss


# --------------------------------------------------------------------------
# Business outcomes and error handling
# --------------------------------------------------------------------------


class BusinessOutcome(Base):
    """A known, valid, non-error result — "No Records Found" is an answer.

    Deliberately has no ``action`` field: the engine always returns the outcome
    automatically, so an action field would only be an opportunity to write a
    wrong value.
    """

    #: An outcome can be checked at several steps without duplicating the entry.
    step_ids: list[int]
    name: str
    detect: Condition
    outcome_code: str
    outcome_message: str
    is_error: bool = False
    #: Must match ``output_schema`` shape exactly, so callers always receive a
    #: consistent structure. Values may all be null.
    return_value: dict[str, Any]

    @field_validator("outcome_code")
    @classmethod
    def check_screaming_snake(cls, v: str) -> str:
        if not SCREAMING_SNAKE_CASE.match(v):
            raise ValueError(
                f"outcome_code must be SCREAMING_SNAKE_CASE: {v!r}"
            )
        return v

    @field_validator("is_error")
    @classmethod
    def check_is_error_false(cls, v: bool) -> bool:
        if v:
            raise ValueError(
                "business outcomes are valid answers, not errors — is_error "
                "must be false"
            )
        return v

    @field_validator("step_ids")
    @classmethod
    def check_non_empty(cls, v: list[int]) -> list[int]:
        if not v:
            raise ValueError("step_ids must list at least one step")
        return v


class ErrorHandler(Base):
    category: ErrorCategory
    action: ErrorAction
    max_retries: int | None = None
    #: Delay between retries. Distinct from ``Step.step_wait_ms``, which is a
    #: post-checkpoint settle.
    retry_wait_ms: int | None = None
    on_exhausted: OnFail | None = None
    capture_screenshot: bool = False
    message: str | None = None

    @model_validator(mode="after")
    def check_category_requirements(self) -> ErrorHandler:
        if self.category is ErrorCategory.RECOVERABLE:
            if self.on_exhausted is None:
                raise ValueError(
                    "recoverable errors require 'on_exhausted' so the engine "
                    "knows what to do when retries run out"
                )
            if self.on_exhausted not in (OnFail.HARD_FAILURE, OnFail.ESCALATE_HUMAN):
                raise ValueError(
                    "on_exhausted must be 'hard_failure' or 'escalate_human'"
                )
        if self.category is ErrorCategory.HARD_FAILURE and not self.capture_screenshot:
            raise ValueError(
                "hard_failure handlers must set capture_screenshot=true — "
                "a failure without visual evidence is not debuggable"
            )
        return self


class SelfHealingOnLayer2(Base):
    healing_action: HealingAction
    flag_step_as: FlagStepAs
    create_review_request: bool = True
    review_message: str | None = None


class SelfHealing(Base):
    """What happens when Layer 2 resolves a step in assisted mode.

    ``update_artifact`` writes a **sidecar patch**, never an in-place edit: an
    artifact that rewrites itself mid-run is no longer reproducible, silently
    invalidates the evidence of earlier runs, and races with concurrent
    replays. See PLAN.md §11 C8.
    """

    enabled: bool = False
    on_layer2_used: SelfHealingOnLayer2 | None = None

    @model_validator(mode="after")
    def check_config_present_when_enabled(self) -> SelfHealing:
        if self.enabled and self.on_layer2_used is None:
            raise ValueError("self_healing is enabled but 'on_layer2_used' is missing")
        return self


# --------------------------------------------------------------------------
# The artifact
# --------------------------------------------------------------------------


class Artifact(Base):
    """A recorded capability: the flow, how to find things, and what can go wrong."""

    capability_id: str
    version: str
    schema_version: str = "1.0.0"
    description: str
    recorded_by: RecordedBy
    created_at: str
    surface_type: SurfaceType
    target: Target

    input_parameters: dict[str, InputParameter] = {}
    output_schema: dict[str, ExpectedType] = {}

    replay_config: ReplayConfig = ReplayConfig()
    session_recovery: SessionRecovery | None = None
    known_interstitials: list[Interstitial] = []

    steps: list[Step]

    business_outcomes: list[BusinessOutcome] = []
    #: Keys are ``_default`` or ``step_<id>``; inner keys are ``ErrorTypeKey``.
    error_map: dict[str, dict[ErrorTypeKey, ErrorHandler]] = {}
    self_healing: SelfHealing = SelfHealing()
    provenance: Provenance | None = None

    @field_validator("capability_id")
    @classmethod
    def check_capability_id(cls, v: str) -> str:
        if not SNAKE_CASE.match(v):
            raise ValueError(f"capability_id must be snake_case: {v!r}")
        return v

    @field_validator("version", "schema_version")
    @classmethod
    def check_semver(cls, v: str) -> str:
        if not SEMVER.match(v):
            raise ValueError(
                f"version must be semver major.minor.patch (e.g. 1.0.0), not {v!r}"
            )
        return v

    @field_validator("created_at")
    @classmethod
    def check_iso_date(cls, v: str) -> str:
        if not ISO_DATE.match(v):
            raise ValueError(f"created_at must be YYYY-MM-DD, got {v!r}")
        return v

    @field_validator("steps")
    @classmethod
    def check_steps_non_empty(cls, v: list[Step]) -> list[Step]:
        if not v:
            raise ValueError("artifact must contain at least one step")
        return v

    @model_validator(mode="after")
    def check_cross_references(self) -> Artifact:
        """Catch dangling references that would otherwise fail silently.

        The dangerous case is ``error_map`` keys: a typo'd or stale ``step_9``
        falls through to ``_default``, so the per-step handling you carefully
        designed simply never runs, with no error anywhere. See PLAN.md §11 C13.
        """
        errors: list[str] = []

        # --- steps are sequential and unique ---
        ids = [s.step_id for s in self.steps]
        if len(set(ids)) != len(ids):
            dupes = sorted({i for i in ids if ids.count(i) > 1})
            errors.append(f"duplicate step_id(s): {dupes}")
        expected = list(range(1, len(ids) + 1))
        if sorted(ids) != expected:
            errors.append(
                f"step_ids must be sequential from 1; got {sorted(ids)}, "
                f"expected {expected}"
            )
        known_ids = set(ids)

        # --- error_map keys resolve ---
        for key in self.error_map:
            if key == DEFAULT_ERROR_KEY:
                continue
            match = STEP_KEY.match(key)
            if not match:
                errors.append(
                    f"error_map key {key!r} must be '_default' or 'step_<id>'"
                )
                continue
            if int(match.group(1)) not in known_ids:
                errors.append(
                    f"error_map key {key!r} refers to a step that does not exist"
                )

        # --- business outcome step_ids resolve ---
        for outcome in self.business_outcomes:
            for sid in outcome.step_ids:
                if sid not in known_ids:
                    errors.append(
                        f"business outcome {outcome.outcome_code!r} references "
                        f"step_id {sid}, which does not exist"
                    )

        # --- outputs: extractions and output_schema must agree exactly ---
        extracted: set[str] = set()
        for step in self.steps:
            for extraction in step.extractions or []:
                if extraction.output_key in extracted:
                    errors.append(
                        f"output_key {extraction.output_key!r} is extracted more "
                        f"than once"
                    )
                extracted.add(extraction.output_key)

        declared = set(self.output_schema)
        if extracted - declared:
            errors.append(
                f"extracted keys missing from output_schema: "
                f"{sorted(extracted - declared)}"
            )
        if declared - extracted:
            errors.append(
                f"output_schema declares keys nothing extracts: "
                f"{sorted(declared - extracted)} — a success result would be "
                f"missing these fields"
            )

        # --- business outcome return_value matches output_schema shape ---
        for outcome in self.business_outcomes:
            got = set(outcome.return_value)
            if got != declared:
                errors.append(
                    f"business outcome {outcome.outcome_code!r} return_value keys "
                    f"{sorted(got)} do not match output_schema {sorted(declared)}; "
                    f"callers must always receive the same shape"
                )

        # --- {{param}} templates resolve to declared input parameters ---
        known_params = set(self.input_parameters)
        referenced: set[str] = set()
        for step in self.steps:
            referenced |= step.referenced_params
        if self.session_recovery:
            for recovery_step in self.session_recovery.recovery_steps:
                referenced |= recovery_step.referenced_params
            for cond in self.session_recovery.detect_conditions:
                referenced |= cond.referenced_params
            referenced |= self.session_recovery.recovery_post_condition.referenced_params
        if referenced - known_params:
            errors.append(
                f"steps reference undeclared parameters: "
                f"{sorted(referenced - known_params)}"
            )

        # --- risky steps need somewhere to escalate to ---
        risky = [s.step_id for s in self.steps if s.risk_level.requires_human_confirmation]
        if risky and not self.business_outcomes and not self.error_map:
            # Not fatal on its own, but a risky artifact with no error handling
            # at all is almost certainly incomplete.
            errors.append(
                f"steps {risky} are high/critical risk but the artifact declares "
                f"no error_map — replay would have no defined failure behaviour"
            )

        if errors:
            raise ValueError(
                "artifact failed cross-reference validation:\n  - "
                + "\n  - ".join(errors)
            )
        return self

    # -- convenience accessors used by the replay engine -------------------

    def step_by_id(self, step_id: int) -> Step:
        for step in self.steps:
            if step.step_id == step_id:
                return step
        raise KeyError(f"no step with step_id {step_id}")

    def outcomes_for_step(self, step_id: int) -> list[BusinessOutcome]:
        return [o for o in self.business_outcomes if step_id in o.step_ids]

    def error_handler(
        self, step_id: int, error_type: ErrorTypeKey
    ) -> ErrorHandler | None:
        """Per-step handler wins over ``_default``; ``None`` if neither exists."""
        step_key = f"step_{step_id}"
        for key in (step_key, DEFAULT_ERROR_KEY):
            handler = self.error_map.get(key, {}).get(error_type)
            if handler is not None:
                return handler
        return None

    def sensitive_parameters(self) -> set[str]:
        return {name for name, p in self.input_parameters.items() if p.sensitive}
