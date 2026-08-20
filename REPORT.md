# REPORT — Blueprint Agent

Design write-up for the computer-use automation take-home.

> **The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the agent invokes it in production.**

**Repo:** <https://github.com/shivamshinde123/blueprint-agent> · **Targets:**
saucedemo.com (modern web) and demo.guru99.com (legacy web) · **Model:**
`anthropic/claude-sonnet-5` via OpenRouter · **346 tests, lint clean**

Every claim here is backed by a committed evidence file or a test. Where
something is designed but not demonstrated, it says so explicitly.

### Contents

1. [Architecture](#1-architecture)
2. [Artifact Schema](#2-artifact-schema) — the longest section, and the centre of the design
3. [Determinism and Error Handling](#3-determinism-and-error-handling)
4. [Heterogeneity and Multi-Tenant](#4-heterogeneity-and-multi-tenant)
5. [Escalation and Handoff](#5-escalation-and-handoff)
6. [Safety](#6-safety)
7. [Cuts](#7-cuts)
8. [Appendix: what running it taught](#appendix-what-running-it-taught)

---

## 1. Architecture

### The two phases, and why the boundary is where it is

**Discovery** puts a model in the loop against a live browser: observe the page,
decide one action, execute it, record what worked, repeat until the goal is met
or a stopping condition fires. It runs once per capability.

**Replay** reads the recorded artifact and executes it mechanically. In strict
mode — the default — no model is contacted at all.

```
PHASE 1 — DISCOVERY (model in the loop, runs once)

  goal + URL ─▶ observe ─▶ decide ─▶ act ─▶ record step
                   ▲                            │
                   └────────────────────────────┘
                                │ goal achieved
                                ▼
                          artifact.json
                                │
PHASE 2 — REPLAY (no model in the decision loop, runs forever)
                                │
  params ─▶ validate ─▶ execute steps ─▶ success | business_outcome | failure
```

The boundary matters more than either half. A system that keeps a model in the
production path inherits its latency, its cost and its variance on *every*
invocation. Freezing the successful run into a typed artifact converts a
probabilistic capability into a deterministic one — which is the only way an
agent can invoke it a thousand times a day and be trusted.

The seam is the artifact. Everything upstream of it may be non-deterministic;
nothing downstream is.

### Demonstrated end to end

```
recorded with  "Sauce Labs Backpack"   -> $29.99   6/6 steps, 0 model calls
replayed with  "Sauce Labs Bike Light" -> $9.99    6/6 steps, 0 model calls
```

Each returning its own correct description. Evidence:
`evidence/discovery_run_saucedemo.json`,
`evidence/replay_run_sauce_labs_backpack.json`,
`evidence/replay_run_sauce_labs_bike_light.json`.

**The second line is the claim that matters.** Replaying with the value it was
recorded with proves only that the transcript replays. Replaying with a
*different* value is what separates a reusable capability from a recording, and
§3 describes the three ways I got that wrong before getting it right.

### Component map

| Module | Responsibility |
|---|---|
| `src/agent/discovery.py` | The observe → decide → act loop and the step recorder |
| `src/agent/observation.py` | Reading the page; the Layer 1 / Layer 2 signal; before/after diffing |
| `src/agent/prompts.py` | Every prompt, in one place, byte-stable for caching |
| `src/agent/decisions.py` | Typed shapes the model returns, sent as strict JSON schema |
| `src/artifact/schema.py` | The artifact: 21 enums, 24 models, all validation |
| `src/artifact/reusability.py` | The one rule that keeps a capability reusable |
| `src/artifact/validator.py` | Pre-flight: load, parameters, escalation, browser match |
| `src/artifact/merge.py` | Base artifact + per-tenant override |
| `src/replay/engine.py` | The deterministic executor |
| `src/replay/locator.py` | **The only module that knows how an element is found** |
| `src/replay/conditions.py` | Evaluating checkpoints; waits on state, never a clock |
| `src/replay/error_handler.py` | The three error categories and retry accounting |
| `src/safety/` | Allowlist, risk gating, redaction |
| `src/escalation/` | Handoff manager and operator console |
| `src/session/browser.py` | The pinned browser context; the single place pinning is enforced |
| `src/llm/client.py` | The only module aware a model provider exists |

Two of those are deliberately narrow: `locator.py` is the seam that makes the
desktop story in §4 credible, and `llm/client.py` is why swapping models is a
config change.

### Target applications

**saucedemo.com** — a modern React store; exercises the accessibility-tree path.

Chosen after abandoning **OrangeHRM**, which I originally selected. Its demo
dataset resets (the employee I was asked to look up ceased to exist
mid-session), its employee search is a typeahead that silently rejects any value
not chosen from its dropdown, and 7 of 15 profile page loads timed out during
probing. Building a graded deliverable on it was a reliability risk with no
compensating benefit.

**demo.guru99.com** — a genuinely legacy bank: table-based layout, form inputs
with no accessible name. It exists here to force the screenshot fallback to do
real work, and it did (§5, §Appendix).

**A local zero-ARIA mock** (`mock/legacy_bank.html`) backs the legacy story in
CI, so the Layer 2 tests do not depend on a public demo site being up. Two tests
assert its premise rather than assuming it: a well-marked-up page resolves via
`get_by_role`/`get_by_label`/`get_by_placeholder`, and the mock resolves via
none of them. If the mock ever drifts into being accessible, that test fails and
says so.

### Model access

Reached through **OpenRouter**, an OpenAI-compatible gateway, so trying a
different model is a config change rather than a rewrite:

```bash
BLUEPRINT_MODEL=google/gemini-3-pro uv run python main.py discover ...
```

Nothing outside `src/llm/` imports a provider SDK.

Routing is **pinned** to one upstream provider with gateway fallback disabled.
A gateway silently substituting a different provider mid-run is exactly the
wrong failure mode for a determinism-focused system — two runs of nominally
identical configuration could be served by different infrastructure. The serving
provider and generation id are recorded per call, so any discovery run is
attributable after the fact.

### Trade-offs taken

| Decision | Cost | Why anyway |
|---|---|---|
| Strict mode holds *no* model client | Cannot self-heal mid-run | Makes "zero model calls" structural, not promised — no later edit can accidentally introduce one |
| Reject an artifact that embeds run data | Discovery fails more often | A silently-wrong capability is worse than a failed recording |
| Accessibility-first locators | More effort than CSS selectors | Identity survives a redesign; position and DOM structure do not |
| One model for text and vision | Not the cheapest per call | One integration, one failure mode, one thing to reason about |
| Validate everything at load | Longer, stricter schema | A malformed artifact fails before a browser opens, not at step 6 with five real actions already taken |

---

## 2. Artifact Schema

The artifact is the central design object. Everything else is either producing
one or consuming one.

### 2.1 What it has to be

Five properties, each of which drove concrete schema decisions:

| Property | Why | How the schema delivers it |
|---|---|---|
| **Executable** | Replay must run it with no model | Every field the engine needs is explicit; nothing is inferred |
| **Reusable** | It must work for inputs it never saw | `{{param}}` templates; the reusability rule (§3) |
| **Reviewable** | A human must be able to audit it | Human-readable descriptions, explicit nulls, no magic |
| **Versioned** | It is a capability, not a script | semver, `schema_version`, provenance with a content hash |
| **Safe by construction** | It drives banking software | Risk levels, sensitivity flags, load-time validation |

### 2.2 Anatomy

```
artifact.json
├── capability_contract     identity, inputs, outputs, target
├── replay_config          browser pinning, mode, timeouts, budgets
├── session_recovery       detect and handle auth expiry
├── known_interstitials    popups auto-dismissed before every step
├── steps[]                the ordered flow
├── business_outcomes[]    known valid non-error results
├── error_map{}            _default + per-step failure handling
├── self_healing{}         what happens when Layer 2 fires
└── provenance             which run produced this, and from what
```

### 2.3 Capability contract

Real, from `artifacts/lookup_product_price_v1.0.0.json`:

```json
{
  "capability_id": "lookup_product_price",
  "version": "1.0.0",
  "schema_version": "1.0.0",
  "description": "Log in to the store, open the product with the given name, and get its price and description.",
  "recorded_by": "agent",
  "created_at": "2026-08-20",
  "surface_type": "modern_web",
  "target": {
    "app_name": "saucedemo",
    "url": "https://www.saucedemo.com",
    "surface_type": "modern_web"
  },
  "input_parameters": {
    "product_name":  { "type": "string", "required": true, "sensitive": false },
    "auth_username": { "type": "string", "required": true, "sensitive": true  },
    "auth_password": { "type": "string", "required": true, "sensitive": true  }
  },
  "output_schema": {
    "description": "string",
    "price": "currency"
  }
}
```

| Field | Rule | Rationale |
|---|---|---|
| `capability_id` | `snake_case` | It is an identifier an agent calls, not prose |
| `version` | strict semver | Minor for locator changes, major for flow changes |
| `schema_version` | strict semver | The *schema* contract, versioned separately from the artifact |
| `surface_type` | `modern_web` \| `legacy_web` \| `desktop_windows` \| `desktop_mac` | Routes locator resolution (§4) |
| `recorded_by` | `agent` \| `human_operator` | Provenance for review |
| `input_parameters[].sensitive` | **strict boolean** | See below |
| `output_schema` | must equal the set of extracted keys | Otherwise a "success" quietly omits a promised field |

**`sensitive` is validated as a strict boolean, not merely typed as one.**
Pydantic would happily coerce the string `"no"` — which is truthy — and quietly
disable redaction on a real password. The validator rejects `"true"`, `"yes"`,
`1`, `0` and every other non-boolean.

### 2.4 Replay configuration

```json
"replay_config": {
  "browser": {
    "viewport": { "width": 1280, "height": 720 },
    "device_scale_factor": 1,
    "is_mobile": false,
    "headless": false,
    "locale": "en-US",
    "timezone_id": "UTC",
    "reduced_motion": "reduce",
    "enforce_strictly": true
  },
  "mode": "strict",
  "default_timeout_ms": 8000,
  "interstitial_probe_timeout_ms": 250,
  "max_retries_per_step": 3,
  "retry_wait_ms": 1000,
  "max_llm_calls_per_replay": 3
}
```

**Why the browser block is this large.** Layer 2 stores pixel coordinates, and
pixels move for more reasons than window size:

- `device_scale_factor` — a DPR of 2 doubles every coordinate.
- `headless` — headless and headful Chromium rasterise fonts differently and
  disagree about scrollbar width.
- `locale` — changes text length, which reflows layout.
- `reduced_motion` — a screenshot taken mid-transition disagrees with one taken
  after it settles.

At replay the live browser is measured *from the page* and compared against this
block; a mismatch is refused before any action. Measuring from the page rather
than trusting the launch options is deliberate — the point is to catch a browser
that did not honour the request.

**`interstitial_probe_timeout_ms` is separate from `default_timeout_ms`, and
much shorter.** Interstitials are probed before *every* step; at the full action
timeout, three interstitials across eight steps would add minutes of dead
waiting to a flow that should take seconds.

**`mode`** is `strict` by default: no model calls at all. `assisted` permits a
bounded number of vision calls for *locator resolution only* — the step sequence
still comes entirely from the artifact, so the model is never in the decision
loop.

### 2.5 Steps

| Field | Type | Required | Notes |
|---|---|---|---|
| `step_id` | int | ✅ | Sequential from 1; referenced by `error_map.step_N` |
| `action` | `click` \| `fill` \| `navigate` \| `extract` | ✅ | |
| `description` | string | ✅ | Shown to the operator during a handoff — written for them |
| `fragile` | bool | ✅ | `true` skips Layer 1 entirely |
| `fragile_reason` | string | when fragile | Why the accessibility tree was useless here |
| `risk_level` | `safe` \| `low` \| `high` \| `critical` | ✅ | high/critical always pause for a human |
| `pre_condition` | Condition \| **explicit null** | ➖ | Never omitted — an explicit null signals deliberate |
| `locators` | Locators \| null | ➖ | Null on extract steps, which carry per-extraction locators |
| `url` | string | navigate only | |
| `value` | string | fill only | May contain `{{param}}` |
| `extractions` | Extraction[] | extract only | |
| `post_condition` | Condition | ✅ | **Required on every step** |
| `step_wait_ms` | int | ➖ | Post-checkpoint settle only; not a sleep |

A real `click` step, showing parameterisation, a two-method chain, an explicit
`nth`, and the free Layer 2 fallback:

```json
{
  "step_id": 5,
  "action": "click",
  "description": "Login succeeded and inventory page is shown. Now click the product link matching the parameterized product name to open its detail page.",
  "fragile": false,
  "risk_level": "safe",
  "pre_condition": {
    "condition": "element_visible",
    "locators": { "...": "same as below" },
    "timeout_ms": 8000,
    "on_fail": "retry"
  },
  "locators": {
    "primary": {
      "strategy": "accessibility_tree",
      "available": true,
      "methods": [
        { "method": "get_by_role", "role": "link", "name": "{{product_name}}", "nth": 0 },
        { "method": "get_by_text", "value": "{{product_name}}" }
      ]
    },
    "fallback": {
      "strategy": "screenshot",
      "coordinates": { "x": 178, "y": 274 },
      "scroll_y": 0,
      "viewport": { "width": 1280, "height": 720 },
      "visual_description": "The product title link in the inventory list, displayed as blue text under the product image, matching the given product name."
    }
  },
  "post_condition": {
    "condition": "page_contains_text",
    "value": "Go back",
    "timeout_ms": 8000,
    "on_fail": "retry"
  }
}
```

Note `"name": "{{product_name}}"` — the locator *depends on* the input rather
than naming one product. That single choice is what lets the artifact find the
Bike Light having been recorded against the Backpack.

**`post_condition` is mandatory on every step.** Never proceed without
confirming the action did something. **`pre_condition` is written as an explicit
`null`** where none makes sense, so a reader can tell the omission was
deliberate rather than forgotten.

### 2.6 The locator model

This is where most of the design effort went, because it is where real
applications fight back.

#### Two layers

```
Layer 1  accessibility tree   get_by_role → get_by_label → get_by_placeholder
                              → get_by_text → get_by_field_label
Layer 2  screenshot + vision  viewport screenshot → coordinates → mouse.click
```

**Golden rule: find elements by identity, not position.** Identity survives a
redesign; coordinates do not.

#### Layer 1 methods

| Method | Matches on | Example |
|---|---|---|
| `get_by_role` | ARIA role **and** accessible name | `{"method":"get_by_role","role":"button","name":"Login"}` |
| `get_by_label` | An associated `<label>` | `{"method":"get_by_label","name":"Employee Id"}` |
| `get_by_placeholder` | Placeholder text | `{"method":"get_by_placeholder","value":"Username"}` |
| `get_by_text` | Visible text, or a **regex** | `{"method":"get_by_text","pattern":"\\$[\\d,.]+"}` |
| `get_by_field_label` | The control beside an *unwired* caption | `{"method":"get_by_field_label","name":"Date of Birth"}` |

The last two exist because real markup demanded them.

**`get_by_field_label`.** A caption is only reachable by `get_by_label` when the
markup actually wires it to its control, and plenty of applications never do.
One target renders a visible "Date of Birth" label whose input carries no
accessible name at all — every accessible-name method returned nothing while a
human reads it instantly. This walks from the label to the nearest ancestor
containing a control, then back down to it, which is how a person reads a form:
the box under or beside the words. Still identity-based; it keys on the caption
a person sees.

**Shape addressing** — `get_by_text` with a `pattern`. The last resort for a
value with **no label, no role, and whose own text *is* the value being read**.
A price in a bare `<div>` cannot be named without naming the price, which is
circular and banned. `\$[\d,.]+` finds it on any product page, for any price,
without naming one.

#### Ambiguity is resolved and recorded

Real pages repeat labels. One search form exposed **two** visible inputs sharing
the placeholder `"Type for hints..."`. Refusing the step is unhelpful; guessing
afresh every run is worse. The chosen index is recorded:

```json
{ "method": "get_by_role", "role": "link", "name": "{{product_name}}", "nth": 0 }
```

An explicit `nth` also lets `get_by_role` legitimately omit a name — *the first
option in a suggestion list* is a stable choice when the option's text is this
run's data. The validator permits role-without-name only when `nth` is present,
so a positional choice is always a recorded decision rather than an accident of
ordering.

#### The fallback is recorded for every step, and costs nothing

When Layer 1 resolves an element, Playwright already holds the handle, so
`bounding_box()` yields the centre point **with no vision call**. A complete
safety net for free was too good to skip.

Two details are load-bearing:

- Coordinates are **viewport-relative**, from a viewport-only screenshot. A
  full-page screenshot stitches the whole scrollable document, so its y-values
  do not map to `mouse.click` — the click would land somewhere else entirely,
  silently.
- `scroll_y` is stored alongside, because a coordinate only addresses the right
  element at the scroll offset it was captured at.

#### A fragile step

When no accessibility method can resolve an element — the normal case on a
legacy surface — the step is recorded fragile, with Layer 1 switched off so
replay does not burn a timeout re-trying a layer already proved useless:

```json
{
  "step_id": 3,
  "action": "fill",
  "fragile": true,
  "fragile_reason": "no accessibility method could resolve this element; the surface exposes no usable name, role or label for it",
  "locators": {
    "primary":  { "strategy": "accessibility_tree", "available": false, "methods": [] },
    "fallback": { "strategy": "screenshot", "coordinates": {"x": 412, "y": 268},
                  "scroll_y": 0, "viewport": {"width":1280,"height":720},
                  "visual_description": "User ID input, first field in the login table" }
  }
}
```

The schema enforces the coupling: `fragile: true` requires a reason, requires
`available: false`, and requires a fallback — because without one there is no
way to resolve the element at all.

### 2.7 Conditions

| `condition` | Operand | Meaning |
|---|---|---|
| `url_contains` | `value` | URL contains the string |
| `url_not_contains` | `value` | URL does not — used in recovery |
| `page_contains_text` | `value` | Text present on the page |
| `element_visible` | `locators` | The element is on screen |
| `element_has_value` | `locators` + `value` | The field holds the value |
| `all_extractions_non_empty` | neither | Every extracted value is non-empty |

```json
{
  "condition": "element_has_value",
  "value": "{{auth_username}}",
  "locators": { "primary": { "methods": [
      {"method":"get_by_placeholder","value":"Username"} ], "available": true } },
  "timeout_ms": 4000,
  "on_fail": "retry"
}
```

`on_fail` is one of `hard_failure`, `retry`, `escalate_human`, or
`on_recovery_fail` — the last legal only inside `session_recovery`.

**Every condition waits for a *state* with a timeout. The replay engine sleeps
nowhere.** A fixed wait is both slower than necessary when the page is quick and
unreliable when it is not.

`page_contains_text` checks the visible body text **and** the accessibility
tree. Checkpoints are synthesised from the accessibility snapshot, where an
accessible name can come from an `alt` attribute that never renders as text — a
recorded checkpoint of `"Go back"` on a page whose button *reads* "Back to
products" failed against body text alone while describing the page perfectly.
Both are honest presentations of a page, so both count; a test confirms absent
text still fails.

`element_has_value` never echoes the actual value into its `observed` field —
it is used on password fields, and `observed` is written to the evidence log.

### 2.8 Extractions

```json
{
  "output_key": "price",
  "locators": {
    "primary": { "strategy": "accessibility_tree", "available": true,
      "methods": [ { "method": "get_by_text", "pattern": "\\$[\\d,.]+" } ] },
    "fallback": { "strategy": "screenshot", "coordinates": {"x":566,"y":307},
      "scroll_y": 0, "viewport": {"width":1280,"height":720},
      "visual_description": "value for price" }
  },
  "extract_method": "text_content",
  "expected_type": "currency",
  "required": true,
  "pattern": "\\$[\\d,.]+"
}
```

| Field | Purpose |
|---|---|
| `output_key` | `snake_case`; must match `output_schema` exactly |
| `extract_method` | `get_value` (input value) \| `text_content` \| `inner_text` |
| `expected_type` | `string` \| `integer` \| `currency` \| `boolean` |
| `pattern` | Regex isolating the value **inside** the element's text |
| `required` | Empty + required → hard failure |

**Two different patterns appear here, and they do different jobs.** The one
inside `locators` says *which element* to find; the one on the extraction says
*which part of its text* is the value. Both describe the **shape** of a value
and never the value, and the reusability check holds them to it: `\$[\d,.]+` is
fine, `\$29\.99` is rejected.

`pattern` exists because the one-element-one-value assumption fails on real
pages. With one capture group the group is taken; otherwise the whole match. The
schema validates that it compiles and carries at most one group, so it is never
ambiguous which part is the value.

**`currency` has a real normalisation contract**, because otherwise the declared
type is decorative and the caller receives an unparseable string:

```json
"price": { "raw": "$29.99", "normalized": "29.99" }
```

Symbols and thousands separators are stripped, the result is parsed as
`Decimal`, and accounting negatives are handled — `($1,200.00)` normalises to
`-1200.00`. Unparseable input is a hard failure, not a silent pass-through.

Here is the model's own recorded reasoning for that extract step, which arrived
at the same diagnosis I did after probing the DOM by hand:

> *"The description and price live in separate DOM elements
> (inventory_details_desc and inventory_details_price) even though the
> accessibility snapshot merges their text with the product name."*

### 2.9 Business outcomes

A known, valid, non-error result. "No records found" is an **answer**.

```json
{
  "step_ids": [7, 8],
  "name": "Cart is empty",
  "detect": { "condition": "page_contains_text", "value": "Your cart is empty",
              "timeout_ms": 4000, "on_fail": "retry" },
  "outcome_code": "CART_EMPTY",
  "outcome_message": "The product was not added, so the cart holds no line item.",
  "is_error": false,
  "return_value": { "item_name": null, "item_price": null }
}
```

Deliberately **no `action` field**: the engine always returns the outcome
automatically, so an action field would only be an opportunity to record a wrong
value. `is_error` is validated as always `false`. `step_ids` is a list so one
outcome can be checked at several steps without duplication. `return_value` must
match `output_schema` **exactly**, so a caller receives the same shape whatever
the result type.

### 2.10 Error map

Keys are `_default` or `step_N`; per-step entries override the default for that
step. Error-type keys are a fixed enum — never invented.

```json
"error_map": {
  "_default": {
    "element_not_found": { "category": "recoverable", "action": "retry",
      "max_retries": 3, "retry_wait_ms": 1000, "on_exhausted": "escalate_human" },
    "checkpoint_failed": { "category": "recoverable", "action": "retry",
      "max_retries": 2, "retry_wait_ms": 1000, "on_exhausted": "hard_failure" },
    "session_expired":   { "category": "recoverable", "action": "re_login",
      "max_retries": 1, "on_exhausted": "escalate_human" },
    "wrong_page_state":  { "category": "hard_failure", "action": "stop",
      "capture_screenshot": true,
      "message": "The page was not in the expected state and could not be recovered." }
  },
  "step_8": {
    "extraction_empty": { "category": "hard_failure", "action": "stop",
      "capture_screenshot": true,
      "message": "The cart rendered but a line item field was empty." }
  }
}
```

A `hard_failure` handler **must** set `capture_screenshot: true` — a failure
with no visual evidence is not debuggable after the fact, and the schema
enforces it. A `recoverable` handler **must** declare `on_exhausted`, so the
engine always knows what happens when retries run out.

### 2.11 Session recovery

A pre-flight check before every step. Recovery steps are a mini-script run as
one atomic unit — they deliberately omit `step_id`, `risk_level`, `fragile` and
per-step conditions, using one shared `recovery_post_condition` instead. They
are not part of the main flow and are never referenced by the error map.

```json
"session_recovery": {
  "detect_conditions": [
    { "condition": "url_contains", "value": "/auth/login", "on_fail": "retry" }
  ],
  "recovery_steps": [
    { "action": "fill",  "value": "{{auth_username}}", "locators": { "...": "..." } },
    { "action": "fill",  "value": "{{auth_password}}", "locators": { "...": "..." } },
    { "action": "click", "locators": { "...": "..." } }
  ],
  "recovery_post_condition": {
    "condition": "url_not_contains", "value": "/auth/login",
    "timeout_ms": 8000, "on_fail": "on_recovery_fail"
  },
  "after_recovery": "restart_from_current_step",
  "max_recovery_attempts": 1,
  "on_recovery_fail": "escalate_human"
}
```

`after_recovery` is almost always `restart_from_current_step` — the step that
triggered the check gets retried. `on_recovery_fail` is `escalate_human`,
because a session that cannot be re-established is something a person should
look at.

### 2.12 Known interstitials

Checked and dismissed before **every** step. Uses `dismiss_action`, deliberately
**not** `action`, because the value set is different:

```json
{
  "name": "loading_spinner",
  "detect": { "condition": "element_visible", "timeout_ms": 250, "on_fail": "retry",
              "locators": { "...": "..." } },
  "dismiss": { "dismiss_action": "wait_for_hidden", "timeout_ms": 8000,
               "on_timeout": "hard_failure", "locators": { "...": "..." } }
}
```

### 2.13 Self-healing

```json
"self_healing": {
  "enabled": false,
  "on_layer2_used": { "healing_action": "update_artifact",
                      "flag_step_as": "needs_review",
                      "create_review_request": true,
                      "review_message": "Coordinates were re-derived by vision." }
}
```

`update_artifact` writes a **sidecar patch** to `artifacts/heal/`, never an
in-place edit. An artifact that rewrites itself mid-run is no longer
reproducible, silently invalidates the evidence of earlier runs, and races with
concurrent replays. The versioned artifact changes only through human review.

### 2.14 Provenance

```json
"provenance": {
  "source_run_id": "discovery-20260820T184551-80e769",
  "model_id": "anthropic/claude-sonnet-5",
  "steps_hash": "80cd996d80fbff99",
  "notes": "Recorded by a live discovery run."
}
```

Ties the artifact to the run that produced it and the model that produced it,
with a hash of the steps so a reviewer can tell whether a flow changed. Cheap to
add, and it is what makes "versioned and reviewable" mean something.

Note what is **not** here: the values the run used or produced. Storing them to
re-check reusability later would mean writing a person's date of birth into a
file that ships in the evidence folder.

### 2.15 The result schema — exactly three shapes

Shared base: `result_type`, `capability_id`, `outputs`, `steps_completed`,
`total_steps`, `layer2_used`, `llm_calls`, `duration_ms`, `evidence`.

```jsonc
// success
{ "result_type": "success",
  "outputs": { "description": "carry.allTheThings()…",
               "price": { "raw": "$29.99", "normalized": "29.99" } },
  "steps_completed": 6, "total_steps": 6,
  "layer2_used": false, "llm_calls": 0 }

// business outcome — a valid answer, not an error
{ "result_type": "business_outcome",
  "outcome_code": "CART_EMPTY",
  "outcome_message": "The product was not added, so the cart holds no line item.",
  "is_error": false,
  "outputs": { "item_name": null, "item_price": null } }

// failure — expected AND observed, or it is not debuggable
{ "result_type": "failure",
  "failure_category": "hard_failure",
  "failed_at_step": 7,
  "error_type": "checkpoint_failed",
  "message": "results table never appeared",
  "expected": "page contains 'Records Found'",
  "observed": "page still showed the search form",
  "outputs": null,
  "evidence": { "screenshot": "…", "dom_snapshot": "…" } }
```

The `expected`/`observed` pair is what makes a failure actionable. A failure is
never returned without both.

### 2.16 The `action` trap

The field name `action` appears in four contexts with four **disjoint** value
sets. This is the single most likely source of an invalid artifact, so two of
them are deliberately renamed:

| Context | Field name | Allowed values |
|---|---|---|
| `steps[]` | `action` | `click`, `fill`, `navigate`, `extract` |
| `error_map` | `action` | `retry`, `stop`, `escalate_human`, `return_outcome`, `re_login` |
| `known_interstitials` | **`dismiss_action`** | `click`, `wait_for_hidden` |
| `self_healing` | **`healing_action`** | `update_artifact`, `flag_only` |
| `business_outcomes` | *(none)* | the engine returns automatically |

### 2.17 Validation: what is rejected, and why it matters

`extra="forbid"` throughout. A typo'd field in a permissive schema is a silent
no-op — the engine simply never sees the value.

Cross-reference checks catch the failures that **do not announce themselves**:

| Check | The silent failure it prevents |
|---|---|
| `error_map.step_N` resolves to a real step | A stale `step_9` falls through to `_default`, so the per-step handling never runs |
| `output_schema` == set of extracted keys | Replay "succeeds" and hands back a result missing a promised field |
| `business_outcome.return_value` matches `output_schema` | Callers get an inconsistent shape depending on outcome |
| Every `{{param}}` is declared | The template substitutes to nothing and types literal braces into a field |
| No duplicate `output_key` | One extraction silently overwrites another |
| `step_ids` sequential from 1 | Error-map keys drift out of alignment |

Plus shape rules per action: navigate requires `url` and forbids `locators`;
fill requires `value` and `locators`; extract forbids top-level `locators` and
requires `extractions`; step 1 cannot have a `pre_condition` because no page
exists yet.

**Roughly 160 of the 346 tests are schema and cross-reference tests.** That is
deliberate: replay is mechanical, so anything not caught at load becomes a
silent wrong action against a live application.

---

## 3. Determinism and Error Handling

### Determinism is structural, not sampled

The obvious approach is `temperature=0`. It is not available — current models
reject sampling parameters outright, and OpenRouter's capability listing for
`anthropic/claude-sonnet-5` omits `temperature`, `top_p` and `seed` entirely.

This sharpens the design rather than weakening it. **Determinism lives in the
artifact and the model-free replay path, not in the sampler.** Discovery may be
probabilistic because its output is reviewed, versioned and frozen. Replay is
deterministic because it makes no decisions at all.

In strict mode `replay()` hands the engine a `None` client. Zero model calls is
a property of the wiring rather than a promise the code might later break, and
there is a test asserting exactly that.

### The property nothing else checked

An artifact can have a valid schema, resolvable locators, passing checkpoints
and correct cross-references — and still be worthless: it works for the exact
values it was recorded with and silently returns the wrong answer for anything
else.

Three separate instances shipped past a green suite:

| Where the run's data leaked | Recorded | Consequence |
|---|---|---|
| Checkpoint | `page_contains_text: "Anderson"` after a search | The surname *that search* returned. Passes for one record, fails for all others |
| Click locator | `option "Peter Mac Anderson"` | The typeahead suggestion for one specific record |
| Extraction locator | `get_by_text("$29.99")` for a price | **The worst.** Circular — finds the element only when the answer is already known. Replay *succeeds* and reports $29.99 for every product. Nothing signals a problem |

`src/artifact/reusability.py` states the rule once:

> **No locator and no checkpoint may contain a value that came from this run.**

Used twice: as immediate feedback while recording, so the model can choose
differently while the page is still in front of it, and as a backstop at
assembly that refuses to write a violating artifact. `{{template}}` references
are the correct way to depend on an input and are never violations. The check
runs with the values in memory and persists none of them.

Writing it as a *testable rule* immediately exposed two holes that twelve live
runs had not:

1. **Substring matching missed `"Peter Mac Anderson"` for input `"Peter
   Anderson"`** — the application interpolated a middle name, so neither string
   contains the other. Fixed with token overlap.
2. **Escaping hid a literal** — `\$29\.99` did not register as containing
   `$29.99`, because backslashes defeat a substring test and the leftover tokens
   (`29`, `99`) fall below the significance floor. Patterns are now unescaped
   before comparison.

### Checkpoints that actually assert something

A checkpoint generated from the state observed *after* an action can be
trivially true — `page_contains_text: "Search"` on a page with a permanent
Search link passes whether or not the click worked.

Candidates are therefore derived from a before/after **diff**, tested against
the pre-action snapshot, and discarded if they already held. They are further
ranked away from data-bearing roles (`cell`, `row`, `option`) toward structural
ones, and rejected outright if they overlap a parameter value.

Where nothing observably changed, a weak checkpoint is recorded **and flagged**,
so a reviewer sees it rather than discovering it when replay passes a broken
step.

### The three error categories

**Ordering matters as much as classification.** Business outcomes are checked
**before** anything is treated as a failure:

```
session recovery → dismiss interstitials → pre-condition → risk gate
  → resolve element → act → CHECK BUSINESS OUTCOMES → post-condition → log
```

| Category | Example | Behaviour |
|---|---|---|
| Expected business outcome | "No records found" | Returned as a valid result, `is_error: false` |
| Recoverable | Session timeout, slow load | Retried per the error map, or recovered and resumed |
| Hard failure | Step genuinely broken | Stop, screenshot, report `expected` vs `observed` |

With the ordering reversed, "no records found" fails its post-condition first
and gets reported as a crash — which makes the capability useless to the agent
calling it. A test asserts the ordering.

### Retry accounting

Counters are keyed per `(step, error type)` and **reset on success**, so a flaky
step that recovers does not carry its history into a later re-entry after
session recovery and exhaust a budget it just refilled.

An error with **no** error-map entry fails rather than continuing. Silently
proceeding past an unclassified error is how a replay "succeeds" having done the
wrong thing.

A `ReplayFailure` already carries its own error type and is **not**
re-classified when it propagates — re-classifying it routed an
`extraction_empty` to the `wrong_page_state` handler and silently bypassed the
per-step policy the artifact declared.

---

## 4. Heterogeneity and Multi-Tenant

*Design, as the brief intends — the implementation targets web.*

### The seam

The artifact separates **what to do** from **how to find it here**. The recorded
flow — click Search, fill Member ID, extract Balance — never changes across
surfaces. The locator strategy changes completely.

| Surface | Locator strategy | Tool |
|---|---|---|
| Modern web | ARIA roles and names | Playwright |
| Legacy web | Screenshot coordinates | Playwright |
| Windows desktop | UI Automation `automation_id` | pywinauto / WinAppDriver |

Today `locators` carries `primary`/`fallback` for web. The extension is
per-surface-type keys:

```jsonc
"locators": {
  "modern_web":      { "strategy": "accessibility_tree", "methods": [...] },
  "legacy_web":      { "strategy": "screenshot", "coordinates": {...} },
  "desktop_windows": { "strategy": "ui_automation", "automation_id": "SearchButton",
                       "control_type": "Button" }
}
```

with a router reading `surface_type` and dispatching to the right resolver.

**Adding desktop support touches exactly two things**: swap Playwright for
pywinauto *in the locator resolver*, and add the `desktop_windows` block. The
error map, business outcomes, session recovery, risk gating and escalation are
all flow-level and stay identical — they never touch an element, only a step.

That claim is credible only because the seam exists in running code:
`src/replay/locator.py` is the sole module that knows how an element is found,
and the engine calls it through one function. The desktop resolver is the part
not written.

### Multi-tenant reuse

Hundreds of tenants run the same vendor application configured differently. One
base artifact holds the canonical flow; a small per-tenant file says only what
differs, merged at load time:

```json
{
  "base_artifact": "golden_artifact.json",
  "tenant_id": "bank_a",
  "tenant_name": "Bank A",
  "overrides": {
    "target.url": "https://bank-a.saucedemo.example.com",
    "steps[0].url": "https://bank-a.saucedemo.example.com/",
    "steps[6].locators.primary.methods[0].name": "Basket",
    "steps[6].locators.fallback.coordinates.y": 512,
    "known_interstitials[+]": { "name": "bank_a_welcome_banner", "…": "…" }
  },
  "notes": "Coordinate offset of +60px accounts for Bank A's branding header."
}
```

Dot-notation addresses the artifact the way a reader would describe it;
`[+]` appends. A new tenant on the same underlying application is onboarded with
one file and **no re-recording**.

The merged result is held to exactly the same validation as any hand-written
artifact, so an override cannot produce something the engine would refuse. An
override path that matches nothing is an **error, not a no-op** — silently doing
nothing is how a tenant runs for weeks against the wrong button. The tenant id
is folded into `capability_id`, so an evidence log cannot be mistaken for
another tenant's run.

### Detecting drift

1. **Replay failure as signal.** `element_not_found` on a step that previously
   passed is strong evidence the UI changed; the log captures the expected
   locator and what was actually on screen.
2. **Canary runs.** Scheduled replays with known-good inputs; a failing canary
   suspends that tenant's artifact from production until reviewed.

The second is designed, not built.

---

## 5. Escalation and Handoff

### Three triggers

1. **Stuck during discovery** — a dead end (the page unchanged across
   consecutive *unproductive* turns), or the model reporting it cannot proceed
   safely.
2. **Replay cannot recover** — retries exhausted with
   `on_exhausted: escalate_human`, or the assisted-mode vision budget spent.
3. **A high or critical risk action** — always, before executing.

### Demonstrated on a live legacy site

```json
{
  "session_id": "1a7d56c9c41f",
  "capability_id": "create_customer",
  "step_id": 9,
  "reason": "dead end: page unchanged for 3 consecutive turns",
  "url_before": "https://demo.guru99.com/V4/manager/addcustomerpage.php",
  "screenshot_before": "evidence/screenshots/handoff_step9_before_51dbf6.png",
  "operator_url": "http://127.0.0.1:8080/operator?session_id=1a7d56c9c41f"
}
```

From `evidence/interventions.json`. The agent logged into Guru99, navigated to
the New Customer form, could not make progress, paused, wrote the record, and
blocked waiting for a person. `resumed_at` is null because nobody clicked Resume
before the run's timeout.

### The same live session

The requirement is specific: the human operates the session the automation was
using, not a fresh one. The browser is **never closed, refreshed or
re-navigated** across a handoff. The automation blocks on an `asyncio.Event`;
the operator works in the same window, at the exact state it stopped at.

A test asserts this rather than assuming it: JavaScript state, form contents and
page identity all survive, and work the operator does in the window is simply
there when the automation resumes.

### One detail worth calling out

The event is cleared **at the start** of `escalate_to_human`, immediately before
the await.

An `asyncio.Event` stays set until something clears it, and a resume can arrive
when nothing is waiting — an operator double-clicking Resume, a retried request.
That leaves the event set, so the next genuine escalation returns instantly: the
automation proceeds with nobody having looked at the screen, while the evidence
log still records a handoff.

I originally justified this with a different scenario and wrote a test for it —
then mutation-tested the claim and found the test had no teeth, because clearing
*after* the await passes it too. The test now models the stray-resume case and
fails under both incorrect placements.

### Two resume semantics

| Trigger | Who performs the step | Resume point |
|---|---|---|
| Stuck / unrecoverable | The **human**, by hand | Automation resumes at step N+1 |
| Risk confirmation | The **automation**, after authorisation | Executes step N, then continues |

Conflating these would either double-execute a step or skip it entirely.

### The operator console

A minimal FastAPI page on `localhost:8080`. Real-time co-browsing is out of
scope by the brief's own note; what must be real is the **mechanism and the
control-transfer model**. The operator does not work in this page — they work in
the browser window the automation opened, which is still sitting where it
stopped. The page tells them what happened, shows the screen as it was, and
hands control back.

It escapes user-supplied text, refuses a resume on a session that is not paused
(409), and blocks path traversal on screenshot serving.

---

## 6. Safety

### Five guardrails

**1. Allowlist.** Permitted domains, route patterns and action types, checked
before every action in *both* phases. Matching is on the **parsed hostname**, so
`saucedemo.com.evil.com` is refused rather than passing a substring check.
`file://` is rejected outright. A missing allowlist file refuses to run rather
than defaulting to permissive.

Upload, download and script execution are blocked unconditionally — **enforced
in code, not merely discouraged in the prompt**. A refused proposal is fed back
so the model can correct itself, up to a limit.

**2. Risk classification.** Four levels. `safe` and `low` execute automatically;
`high` and `critical` **always** pause for human confirmation, with no
configuration flag to disable it. An artifact containing a risky step refuses to
start without an escalation handler, because the alternative is pausing mid-flow
with no way to resume. An unrecognised classification resolves to `low`, never
`safe`.

**3. Redaction.** Replacement happens *before* the write, never after. It covers
literal secret values and `{{sensitive_param}}` templates, walks nested
structures via `model_dump()` so fields added to the schema later are covered,
and replaces longest-first so an overlapping shorter secret cannot leave the
remainder of a longer one behind. Secrets under four characters are not
substring-redacted — a two-character secret would match half the log and destroy
it — with the template and parameter-name paths still covering them.

Every committed evidence log shows `***REDACTED***` where credentials were used:
the redaction path working on real files, not only in tests.

**4. Not capturing secrets in the first place.** Playwright tracing, video and
HAR recording are never enabled **and are not configurable**. All three persist
raw page content including credential fields as they are typed, and redaction
cannot reach inside a video file. Credentials are read from `.env` via
`--credentials` rather than passed on the command line, where they would land in
shell history and the process list.

**5. Fail-fast pre-flight.** Schema validation, cross-references, required
parameters, allowlist and escalation availability are all checked before a
browser opens. Discovering a violation at step 6 means five real actions already
ran against a live system.

### Limits of this model

Stated plainly rather than implied away:

- **A failure screenshot captures whatever was on screen**, which can include
  PII. Redaction cannot reach inside a PNG. Screenshots are excluded from
  version control for this reason; production would need an OCR redaction pass.
- **The accessibility snapshot sent to the model contains whatever the page
  shows**, including any PII on it. This cannot be solved at this scope — the
  agent must see the page to drive it.
- The allowlist governs *destinations and action types*. It does not constrain
  what the model reasons about once a permitted page is open.
- **Risk classification is the model's judgement**, checked by a human only at
  the gate. A write mis-classified as `safe` executes without a pause. Mitigated
  by instructing the model to err high and by resolving unparseable values to
  `low`, but the residual risk is real.
- A capability that *should* be risky but was recorded as safe stays safe on
  every replay. Nothing re-evaluates risk at replay time.

---

## 7. Cuts

Deliberate, each with what I would do instead.

**Real-time co-browsing.** Out of scope by the brief's own note. The operator
uses the same physical browser window the automation opened. Production would
stream the session.

**Desktop and legacy-web surface routing.** Designed (§4), not built. The seam
is real and single-location; the resolver for a second surface is not written.

**Business outcomes are not auto-recorded — the one genuine code gap.** A
successful discovery run never encounters "no results", so outcomes cannot be
observed from the happy path. The fix — a *negative probe* that re-runs the
recorded flow once with a deliberately invalid input and records what the
application reports — is designed but not implemented. Consequence: a bad input
currently produces a structured `failure` rather than a clean business outcome.
**The engine supports outcomes fully; the recorder does not yet populate them.**

**No mid-replay resume.** If the process dies at step 5 there is no checkpoint to
resume from. Production would persist runtime outputs and step index per step.

**The run log is written only at the end.** A run killed while blocked on a
handoff loses its entire log; only `interventions.json`, which is appended at
escalation time, survives. Found by exactly that happening. The log should be
flushed incrementally, or at minimum when the run pauses.

**Self-healing writes a sidecar patch that nothing consumes.** Healed
coordinates go to `artifacts/heal/` with a review request rather than mutating
the versioned artifact. The review workflow that folds a patch back in is not
built.

**Discovery reliability is one data point per flow.** It succeeded; I have not
measured a success rate and it should not be described as reliable.

### What I would build next, in order

1. The negative probe, to complete the error-handling trichotomy with evidence.
2. Incremental evidence flushing, so escalation evidence survives interruption.
3. A canary runner, since drift detection is currently a design claim.
4. The desktop resolver, to prove the surface seam rather than assert it.
5. An artifact review UI — the schema is deliberately reviewable and nothing yet
   takes advantage of that.

---

## Appendix: what running it taught

Roughly two dozen bugs were found by running the system against live
applications. The instructive part is that they cluster, and that the largest
cluster was invisible to a green test suite.

**Run-specific data in artifacts** (three instances, §3) — the reason
`reusability.py` exists.

**Timing.** A single-page application renders *after* the click, not after
`networkidle`. One search went from 342 named nodes to 57 in that gap, so a
snapshot taken at network-idle showed the previous page and the step read as
"nothing changed" — which sent the agent into clicking the same button
repeatedly.

**Judging a page instead of an element.** Whether to fall back to vision was
measured page-wide. That fails twice over: a table-based page produces plenty of
named nodes because `cell` and `row` inherit names from their text, and a legacy
page's *navigation* is richly named while its form inputs carry nothing. Guru99's
login page reports sixteen named interactive nodes — all chrome — while its
`uid` and `password` inputs have no accessible name at all. Both cases report a
healthy tree for a page Layer 1 cannot drive. The fallback is now decided **per
element**, when the locator actually fails.

**Limits with no gradient.** A strict JSON schema caps at 16 union-typed
parameters; every `str | None` is a union, and the decision models nest a
locator inside the decision *and* inside every extraction, so optionals
multiply. Adding one field took the count to 17 and every discovery call began
failing at once, with an error that reads like a model problem. Sentinel
defaults took it back to 1, pinned by a test that fails with the offending paths
listed.

**Dead code is not tested code.** `resolve_by_vision` — the entire Layer 2 path
— had 100% of its *schema* tested and 0% of its behaviour. When first executed
it failed twice for two independent construction bugs. "Built and unit-tested"
was doing a great deal of work in my own status reporting, and for that component
it meant almost nothing.

**When it did run, it behaved well.** Asked to find a "Manager Login" link that
was not on screen, the vision fallback returned a reasoned refusal — enumerating
what it could see and reporting the element absent — rather than inventing
coordinates. A hallucinated coordinate produces a click on whatever happens to
be there, which is worse than a clean failure.

**Tests that check the wrong artefact.** All of the above passed 320 tests,
because those tests validated Python models rather than the JSON schema those
models emit, and schema correctness rather than reusability. The most productive
hour of the project was spent offline turning "I hope this generalises" into a
rule with tests — which found two flaws in itself within that hour that twelve
live runs had not.
