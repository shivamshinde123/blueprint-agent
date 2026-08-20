# PLAN.md — Blueprint_Agent

**Status:** Framework complete (Phases 0–5). 305 tests passing, lint clean. Awaiting target flows for the first real discovery run.
**Last updated:** 2026-08-20
**Source of truth:** [Notion project page](https://app.notion.com/p/3bd3166155398177b587cb3d9ab26701) (10 sub-pages) + the assignment PDF.

---

## 0. About This Document

This is the single build plan for the take-home. It consolidates all 10 Notion design pages into one executable spec, and — critically — **records the places where the Notion design has gaps, contradictions, or bugs, along with the decision taken for each** (§11).

Precedence when sources disagree:

1. The assignment PDF (the grading contract)
2. This document (§11 decisions override Notion where noted)
3. The Notion pages (design rationale and detail)

Notion stays the narrative design record. This file is what you code against.

---

## 1. Mission & Grading Contract

> **The model discovers. The artifact becomes a reusable capability. Deterministic replay is how the AI agent invokes it in production.**

Banks run legacy software with no API. The only interface is the UI. Build a system that (a) drives that UI with an LLM once, (b) freezes the successful run into a reusable typed artifact, and (c) replays that artifact mechanically in production with no LLM in the decision loop.

### What is graded, in the evaluator's stated order

| # | Criterion | Where it is earned | Primary risk |
|---|---|---|---|
| 1 | System design | Artifact schema + replay contract (§4, §6) | Schema that is verbose but not *reviewable* |
| 2 | Core loop correctness | Real discovery run + deterministic replay (§5, §6) | Discovery that only works once |
| 3 | Robustness & error handling | Three error categories (§6.4) | Treating business outcomes as crashes |
| 4 | Human-in-the-loop escalation | Real pause/handoff/resume (§8) | Shipping a stub — explicit auto-fail |
| 5 | Generalization | Surface abstraction + multi-tenant (§10) | Hand-waving with no concrete seam |
| 6 | Safety & data handling | Allowlist + redaction (§7) | Secrets leaking via a channel you didn't consider |
| 7 | Code quality | Typed, readable, runnable | Un-runnable README |
| 8 | Communication | REPORT.md, 7 sections (§16) | Not stating the cuts |

### Non-negotiables (explicit in the PDF)

- **The discovery run must be real.** At least one genuine LLM-driven run against a live surface, with evidence on disk. Cannot be faked or mocked.
- **The evidence folder must contain an error-scenario replay.**
- **Escalation must be a real mechanism**, not a TODO.
- Everything else may be mocked *if the seam is real and the cut is documented*.

**Depth over breadth:** a thin-but-real version of all six components beats a polished version of two.

---

## 2. Environment Readiness (verified on this machine)

Re-verified 2026-08-20 after Phase 0 setup. Environment is **ready**; all previously
blocking gaps are closed. Dependency management is `uv` (D4), lockfile committed.

| Component | Status | Detail |
|---|---|---|
| Python | ✅ 3.12.4 | Anaconda distribution; 3.10 also on the `py` launcher |
| pip | ✅ 25.2 | |
| git | ✅ 2.54.0.windows.1 | Repo initialized; pushed to [shivamshinde123/blueprint-agent](https://github.com/shivamshinde123/blueprint-agent) |
| GitHub CLI | ✅ 2.88.1 | Authenticated as `shivamshinde123`; public repo created |
| Node | ✅ v22.18.0 | Not required; incidental |
| uv | ✅ 0.9.6 | Project + dependency manager (D4) |
| `openai` | ✅ 3.3.1 | Client for the OpenRouter gateway (D5). The `anthropic` SDK was removed |
| `pydantic` | ✅ 2.13.4 | v2 — `@field_validator` / `@model_validator` |
| `playwright` | ✅ 1.62.0 | Chromium installed |
| `fastapi` / `uvicorn` | ✅ | Operator console |
| `python-dotenv` | ✅ | |
| `pytest` / `pytest-asyncio` | ✅ 9.1.1 / 1.4.0 | `asyncio_mode = "auto"` |
| `OPENROUTER_API_KEY` | ⚠️ **still blank** | In `.env` (gitignored). Only needed for `discover` — `validate` and `replay --mode strict` run without it |

### Gaps closed

- [x] Dependency manager chosen: **uv** (D4), `.venv` + `uv.lock` committed
- [x] `playwright` + `pytest-asyncio` installed
- [x] `playwright install chromium`
- [x] `.gitignore` written **before** `.env` existed; `.env` verified ignored
- [x] Guru99 demo credentials received and stored in `.env`
- [x] `git init`
- [x] Public GitHub repo created and pushed — <https://github.com/shivamshinde123/blueprint-agent>
- [ ] **`OPENROUTER_API_KEY`** → `.env` *(only blocker remaining, and only for Phase 2)*

---

## 3. Architecture

### 3.1 The two phases

```
PHASE 1 — DISCOVERY (LLM in the loop, runs once)
  goal + URL ──▶ observe (a11y tree | screenshot)
                    │
                    ▼
                 decide (Claude, structured output)
                    │
                    ▼
                 act (Playwright)  ──▶ record step
                    │
                    └──▶ loop until goal met / stop condition
                                    │
                                    ▼
                              artifact.json

PHASE 2 — REPLAY (no LLM in the decision loop, runs forever)
  artifact.json + params ──▶ validate ──▶ execute steps mechanically
                                              │
                                              ▼
                                    success | business_outcome | failure
```

### 3.2 The two interaction layers

| | Layer 1 — Accessibility tree | Layer 2 — Screenshot + vision |
|---|---|---|
| Mechanism | `get_by_role` → `get_by_label` → `get_by_placeholder` → `get_by_text` | Viewport screenshot → Claude → pixel coordinates → `mouse.click` |
| Speed | Very fast | 1–3 s per call |
| Cost | Free | Vision API call |
| Determinism | High (identity-based) | Medium (position-based) |
| Needs pinned viewport | No | **Yes, critically** |
| Used in replay | Always first | Only if Layer 1 fails **and** mode is `assisted` |

**Golden rule: find elements by identity, not position.** Identity is stable across renders; position is not.

### 3.3 Module map

```
src/
├── agent/
│   ├── discovery.py      observe→decide→act loop, step recorder
│   ├── prompts.py        every LLM prompt, in one place
│   └── decisions.py      Pydantic models for structured LLM output
├── llm/
│   └── client.py         provider-agnostic gateway client (D5)
├── replay/
│   ├── engine.py         the deterministic executor
│   ├── locator.py        Layer 1 chain + Layer 2 fallback
│   └── error_handler.py  the three error categories
├── artifact/
│   ├── schema.py         Pydantic v2 models (BUILD FIRST)
│   ├── validator.py      cross-reference + load-time checks
│   └── merge.py          base + tenant override merge (design demo)
├── safety/
│   ├── guardrails.py     allowlist, risk gating
│   └── redaction.py      sensitive-value scrubbing
├── escalation/
│   ├── handoff.py        SessionHandoffManager
│   └── console.py        FastAPI operator console
├── session/
│   └── browser.py        pinned browser context factory
└── evidence/
    └── logger.py         structured run log writer
```

### 3.4 Full project layout

```
Blueprint_Agent/
├── README.md              setup + exact demo commands
├── REPORT.md              7-section design writeup
├── PLAN.md                this file
├── .env                   NEVER committed
├── .env.example           committed, no real values
├── .gitignore
├── pyproject.toml         uv project + deps + pytest config
├── uv.lock
├── main.py                CLI entry point
├── config/
│   ├── allowlist.json     permitted domains, routes, actions
│   └── tenants/           base + per-tenant override demo
├── artifacts/             saved capability artifacts (root level, NOT in evidence/)
├── evidence/
│   ├── discovery_run_orangehrm.json
│   ├── replay_run_orangehrm.json
│   ├── replay_error_run.json
│   ├── interventions.json
│   └── screenshots/
├── mock/                  local legacy surface (see §11 C11)
├── src/                   (as above)
└── tests/
```

---

## 4. Authoritative Artifact Schema

The central design artifact. Nine top-level sections.

```
artifact.json
├── capability_contract    identity, inputs, outputs, target
├── replay_config          browser pinning, mode, timeouts, budgets
├── session_recovery       detect + handle auth expiry
├── known_interstitials    auto-dismissed popups/spinners
├── steps[]                the ordered flow
├── business_outcomes[]    known valid non-error results
├── error_map{}            _default + step_N failure handling
├── self_healing{}         what happens when Layer 2 fires
└── provenance             run id, schema version, hash  ← added, §11 C16
```

### 4.1 Top level

| Field | Type | Req | Format / allowed | Notes |
|---|---|---|---|---|
| `capability_id` | string | ✅ | `snake_case` only | e.g. `lookup_employee_profile` |
| `version` | string | ✅ | semver `M.m.p` | `1.0.0` — never `1.0` or `v1.0.0` |
| `schema_version` | string | ✅ | semver | Schema contract version, distinct from artifact version |
| `created_at` | string | ✅ | `YYYY-MM-DD` | |
| `surface_type` | enum | ✅ | `modern_web` \| `legacy_web` \| `desktop_windows` \| `desktop_mac` | Routes locator resolution (§10) |
| `recorded_by` | enum | ✅ | `agent` \| `human_operator` | No version strings here |
| `description` | string | ✅ | free text | Human-readable purpose |
| `target` | object | ✅ | `{app_name, url, surface_type}` | |

### 4.2 `input_parameters` — map of name → object

| Field | Type | Req | Allowed | Notes |
|---|---|---|---|---|
| `type` | enum | ✅ | `string` \| `integer` \| `boolean` | |
| `required` | bool | ✅ | `true` \| `false` | |
| `sensitive` | bool | ➖ | `true` \| `false` (default `false`) | **Strict bool.** Reject `"true"`, `"yes"`, `1` — validator enforces |
| `description` | string | ➖ | | |

### 4.3 `replay_config`

Renamed from Notion's `viewport`-only block — coordinate stability needs more than width and height (§11 C2).

```jsonc
{
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
  "mode": "strict",                    // default; see §11 C10
  "default_timeout_ms": 8000,
  "interstitial_probe_timeout_ms": 250, // §11 C9 — must NOT be default_timeout_ms
  "max_retries_per_step": 3,
  "retry_wait_ms": 1000,
  "max_llm_calls_per_replay": 3        // assisted mode only
}
```

**Mode semantics**

- `strict` — zero LLM calls. Layer 1 fails → escalate to human. **Default, and the mode used for the graded evidence run.**
- `assisted` — one bounded vision call per failing step, capped at `max_llm_calls_per_replay` across the whole run. Budget exhausted → escalate.

`max_llm_calls_per_replay: 3` means *3 failing steps across the run*, not 3 screenshots total per step.

**No blind sleeps.** The engine never sleeps between steps. Every step waits on a *condition* with a timeout. `step_wait_ms` exists only for apps that need post-checkpoint settling, and its use requires a comment justifying it.

### 4.4 `steps[]`

| Field | Type | Req | Allowed | Notes |
|---|---|---|---|---|
| `step_id` | int | ✅ | ≥1, sequential, unique | Referenced by `error_map.step_N` |
| `action` | enum | ✅ | `click` \| `fill` \| `navigate` \| `extract` | |
| `description` | string | ✅ | | Shown to the human during escalation — write it for them |
| `fragile` | bool | ✅ | | `true` → skip Layer 1 entirely |
| `fragile_reason` | string | ⚠️ | required iff `fragile` | Why the a11y tree was useless here |
| `risk_level` | enum | ✅ | `safe` \| `low` \| `high` \| `critical` | When in doubt, go higher |
| `pre_condition` | object \| null | ➖ | Condition | **Explicit `null`**, never omitted — signals deliberate |
| `locators` | object \| null | ➖ | primary + fallback | `null` only on `extract` steps |
| `extractions` | array | ⚠️ | required iff `action == extract` | |
| `post_condition` | object | ✅ | Condition | **Required on every step.** Never proceed unverified |
| `step_wait_ms` | int | ➖ | | Post-checkpoint settle only. Not a sleep |

**Risk enforcement:** `safe` and `low` execute automatically. `high` and `critical` **pause and require human confirmation before executing** — always, no exceptions, no config flag to disable.

### 4.5 `locators`

```jsonc
{
  "primary": {
    "strategy": "accessibility_tree",
    "available": true,                 // false on fragile steps
    "methods": [                       // tried in array order
      { "method": "get_by_role", "role": "button", "name": "Search" },
      { "method": "get_by_text", "value": "Search" }
    ]
  },
  "fallback": {
    "strategy": "screenshot",
    "coordinates": { "x": 730, "y": 420 },
    "scroll_y": 0,                     // §11 C1 — required for correctness
    "viewport": { "width": 1280, "height": 720 },
    "visual_description": "Search button in the member lookup form"
  }
}
```

Method priority is fixed: `get_by_role` (most specific — role *and* accessible name) → `get_by_label` → `get_by_placeholder` → `get_by_text` (loosest).

**Scoping for ambiguity:** when several elements share a name, scope to a parent region using a11y methods (find region `Member Search`, then the button within it). Never reach for raw DOM/CSS selectors — that defeats the surface-abstraction argument in §10.

**Dynamic labels:** parameterize with `{{param_name}}` in the locator `name`; the engine substitutes before resolution.

### 4.6 Conditions (pre & post)

| Field | Type | Allowed |
|---|---|---|
| `condition` | enum | `url_contains`, `url_not_contains`, `element_visible`, `page_contains_text`, `element_has_value`, `all_extractions_non_empty` |
| `value` | string | the string to match |
| `locators` | object | when the condition targets an element |
| `timeout_ms` | int | wait budget for the condition |
| `on_fail` | enum | `hard_failure`, `retry`, `escalate_human`, `on_recovery_fail` |

`on_recovery_fail` is legal **only** inside `session_recovery.recovery_post_condition`.

### 4.7 `extractions[]` (on `extract` steps)

| Field | Type | Allowed |
|---|---|---|
| `output_key` | string | `snake_case`; must match `output_schema` exactly |
| `locators` | object | per-extraction, not top-level |
| `extract_method` | enum | `get_value` (input value) \| `text_content` \| `inner_text` |
| `expected_type` | enum | `string` \| `integer` \| `currency` \| `boolean` |
| `required` | bool | empty + required → `hard_failure` |

**`currency` normalization contract** (§11 C14): strip currency symbols and thousands separators, parse as `Decimal`, store `{raw, normalized}`. Fail the extraction if it will not parse.

### 4.8 `known_interstitials[]`

Checked and dismissed before **every** step. Uses `dismiss_action`, deliberately **not** `action` — different value set.

| Field | Allowed |
|---|---|
| `name` | free text |
| `detect` | `{condition, locators}` — probed with `interstitial_probe_timeout_ms`, not the action timeout |
| `dismiss.dismiss_action` | `click` \| `wait_for_hidden` |
| `dismiss.on_timeout` | `hard_failure` \| `continue` |

Three baseline entries: `cookie_consent_banner` (click Accept), `loading_spinner` (`wait_for_hidden` on the progressbar), `session_expiry_warning` (click Stay Logged In).

### 4.9 `session_recovery`

A pre-flight check before every step. Recovery steps are a **mini-script run as one atomic unit** — they deliberately omit `step_id`, `risk_level`, `fragile`, and per-step conditions, using one shared `recovery_post_condition` instead. They are not part of the main flow and are never referenced by `error_map`.

| Field | Allowed |
|---|---|
| `detect_conditions[]` | e.g. `url_contains: /auth/login` |
| `recovery_steps[]` | `{action, locators, value}` triples |
| `recovery_post_condition` | `url_not_contains: /auth/login`, `on_fail: on_recovery_fail` |
| `after_recovery` | `restart_from_current_step` (almost always) \| `restart_from_beginning` |
| `max_recovery_attempts` | `1` |
| `on_recovery_fail` | `escalate_human` (always — a person should see a broken session) |

### 4.10 `business_outcomes[]`

Known-valid non-error results. **No `action` field** — the engine always returns them automatically; an `action` field would only be an opportunity to write a wrong value.

| Field | Allowed |
|---|---|
| `step_ids[]` | ints — one outcome can be checked at several steps |
| `detect` | `page_contains_text: "No Records Found"` |
| `outcome_code` | `SCREAMING_SNAKE_CASE` — e.g. `EMPLOYEE_NOT_FOUND` |
| `outcome_message` | human-readable |
| `is_error` | always `false` |
| `return_value` | must match `output_schema` shape exactly, nulls allowed |

Baseline for OrangeHRM: `EMPLOYEE_NOT_FOUND`, `AUTH_FAILED`.

### 4.11 `error_map{}`

Keys: `_default` first (underscore marks it as the fallback), then `step_N`. Per-step entries override `_default` for that step.

Error type keys are a **fixed enum** — never invent one: `element_not_found`, `checkpoint_failed`, `extraction_empty`, `wrong_page_state`, `timeout`, `session_expired`.

| Field | Allowed |
|---|---|
| `category` | `recoverable` \| `business_outcome` \| `hard_failure` |
| `action` | `retry` \| `stop` \| `escalate_human` \| `return_outcome` \| `re_login` |
| `max_retries` | int — overrides the global default |
| `retry_wait_ms` | int — the retry delay (distinct from `step_wait_ms`) |
| `on_exhausted` | `hard_failure` \| `escalate_human` — required for `recoverable` |
| `capture_screenshot` | bool — **always `true` for `hard_failure`** |

### 4.12 ⚠️ `action` means four different things

The single most likely source of an invalid artifact. The field name repeats; the value sets do not overlap.

| Context | Field name | Allowed values |
|---|---|---|
| `steps[]` | `action` | `click`, `fill`, `navigate`, `extract` |
| `error_map` | `action` | `retry`, `stop`, `escalate_human`, `return_outcome`, `re_login` |
| `known_interstitials` | `dismiss_action` | `click`, `wait_for_hidden` |
| `self_healing` | `healing_action` | `update_artifact`, `flag_only` |
| `business_outcomes` | *(none)* | — engine returns automatically |

### 4.13 Result schema — exactly three shapes

Shared base: `result_type`, `capability_id`, `outputs`, `steps_completed`, `total_steps`, `layer2_used`, `layer2_llm_calls`, `duration_ms`, `evidence{log_file, screenshot}`.

| `result_type` | Additional fields | Meaning |
|---|---|---|
| `success` | `outputs` populated, `steps_completed == total_steps` | Flow completed, data extracted |
| `business_outcome` | `outcome_code`, `outcome_message`, `is_error: false`, `outputs` = `return_value` | A valid answer that isn't the happy path |
| `failure` | `failure_category`, `failed_at_step`, `error_type`, `message`, `expected`, `observed`, `outputs: null`, `evidence.dom_snapshot` | Something actually broke |

The `expected` / `observed` pair is what makes a failure debuggable. Never return a failure without both.

---

## 5. Discovery Engine

### 5.1 Loop

1. Open Chromium with the **pinned context** (§4.3) and navigate to the target URL.
2. Read the accessibility-tree snapshot.
3. If the snapshot is useful → send snapshot + goal + history to Claude. If it is empty or sparse → take a **viewport** screenshot and send that instead.
4. Claude returns a structured decision (§5.3).
5. Execute via Playwright.
6. **Record the step** — including a Layer 2 fallback captured for free from `bounding_box()` (§11 C4).
7. Ask Claude: goal achieved? If not, loop.

### 5.2 Stopping conditions

| # | Condition | Threshold |
|---|---|---|
| 1 | Goal achieved | LLM confirms + all outputs extracted |
| 2 | Max steps | 25 actions |
| 3 | Dead end | a11y snapshot identical 3 consecutive times |
| 4 | Global timeout | 300 s for the whole run |

Dead-end detection is also escalation Signal 1 (§8.5).

### 5.3 The decision call

Use **structured outputs** so decisions are schema-valid by construction — no JSON repair code, no parse-failure retries.

```python
class AgentDecision(BaseModel):
    action: Literal["click", "fill", "navigate", "extract"]
    reasoning: str
    target_description: str
    value: str | None = None
    locator_method: Literal["get_by_role","get_by_label","get_by_placeholder","get_by_text"] | None = None
    locator_role: str | None = None
    locator_name: str | None = None
    goal_achieved: bool
    stuck: bool                 # escalation Signal 3

# src/llm/ owns all provider knowledge; the agent never imports an SDK.
decision = llm.decide(
    system=DISCOVERY_SYSTEM_PROMPT,   # stable prefix -> prompt-cached
    user=render_observation(snapshot, goal, history),
    schema=AgentDecision,
    image_png=screenshot,             # only on the Layer 2 path
).value                               # validated AgentDecision
```

`LLMClient.decide` derives a strict JSON schema from the Pydantic model and
sends it as `response_format`, so the response either validates or raises —
there is no JSON-repair path to get subtly wrong.

**API facts that constrain the design** (verified against OpenRouter's live catalogue for `anthropic/claude-sonnet-5`):

- No `temperature` / `top_p` / `seed` in the model's supported parameters. You *cannot* pin the sampler for determinism. This is fine, and it sharpens the project's thesis: **determinism lives in the artifact, not in the model** (§11 C3).
- Reasoning depth is `reasoning: {effort: ...}`, set from `settings.DISCOVERY_EFFORT`.
- The system prompt carries the full schema spec and is byte-stable → prompt-cache it via a `cache_control` breakpoint. Verify with `usage.prompt_tokens_details.cached_tokens > 0`.
- Log the generation id **and the serving provider** into the evidence file for every call — free traceability, and the audit trail C19 requires.

### 5.4 Recording rules

1. **Parameterize by provenance, not string matching** (§11 C7). Only the `value` of a `fill` step where the agent itself injected a known parameter becomes `{{param}}`. Never regex the artifact for the literal string.
2. **Always capture a Layer 2 fallback**, even when Layer 1 worked — `bounding_box()` centre point plus `scroll_y`. Costs nothing, and gives every step a safety net.
3. **Every step gets `risk_level`, `pre_condition` (explicitly `null` if none), and `post_condition`.** A step missing these fails Pydantic validation at load.
4. **Post-conditions must be non-trivial** (§11 C5): derive from a before/after snapshot diff and reject any candidate that already held before the action.
5. Step 1 is `navigate`: `pre_condition` is always `null`, `post_condition` is `url_contains` (§11 C17).

### 5.5 Post-discovery assembly

```
recorded steps
  → parameterize (provenance-based)
  → attach visual descriptions to all fallbacks
  → build error_map (_default + per-step overrides)
  → NEGATIVE PROBE PASS → business_outcomes   ← §11 C6
  → add session_recovery (if a login step exists)
  → add known_interstitials (any encountered)
  → stamp provenance {source_run_id, schema_version, content hash}
  → validate against Pydantic
  → save artifacts/<capability>_v<semver>.json
```

The negative probe re-runs the flow once with a deliberately invalid parameter to *observe* the not-found text rather than guessing it. It populates `business_outcomes` **and** produces the required error-scenario evidence in a single pass.

---

## 6. Replay Engine

### 6.1 Pre-flight (before the browser opens — fail fast)

1. Load and Pydantic-validate the artifact. Invalid type or enum → reject with a clear error.
2. Cross-reference validation (§11 C13): every `error_map.step_N` and every `business_outcomes.step_ids` entry must resolve to a real `step_id`. Every `output_key` must exist in `output_schema`.
3. All `required` input parameters present.
4. Target URL passes the allowlist.
5. Any `high`/`critical` step → a handoff manager must be configured, else `ConfigError`.
6. `replay_config.browser` matches the launch context; mismatch → configuration error **before** any action.

### 6.2 Per-step sequence

```
read step
  → session_recovery check      (detect → recover → retry current step)
  → dismiss known interstitials (short probe timeout)
  → verify pre_condition
  → risk gate                   (high/critical → pause, require human confirm)
  → resolve element             (Layer 1 chain → Layer 2 if allowed)
  → execute action
  → check business_outcomes     ← BEFORE treating anything as failure
  → verify post_condition
  → log step to evidence
  → next
```

Ordering matters: **business outcomes are checked before failure handling.** "No Records Found" is an answer, not a crash.

### 6.3 Locator chain

```
fragile == true ──────────────────────────▶ Layer 2 directly
otherwise → try each primary method in order
    found ────────────────────────────────▶ act
    all failed →
        mode == strict   ─────────────────▶ escalate_human (0 LLM calls)
        mode == assisted →
            budget exhausted ─────────────▶ escalate_human
            else → screenshot → Claude → coords → act
                 → self-healing: write SIDECAR PATCH, never mutate artifact (§11 C8)
```

### 6.4 The three error categories

| Category | Example | Behaviour |
|---|---|---|
| **Expected business outcome** | "Member not found" | Return as a valid result. `is_error: false`. Not a crash |
| **Recoverable** | Session timeout, slow load | Auto-retry per `error_map`, or recover and continue |
| **Hard failure** | Step genuinely broken | Stop, screenshot, report `expected` vs `observed` |

Getting this trichotomy visibly right is grading criterion #3.

---

## 7. Safety Model

### G1 — Allowlist (`config/allowlist.json`)

Checked before every action, in **both** discovery and replay.

- **Domains:** `opensource-demo.orangehrmlive.com`, `demo.guru99.com`, `localhost` (mock)
- **Route patterns:** `/web/index.php/pim/`, `/web/index.php/auth/`, `/V4/`
- **Action types:** `click`, `fill`, `navigate`, `extract` only. File upload, download, and script execution are blocked unconditionally — even if the LLM asks.

### G2 — Risk classification

| Level | Meaning | Replay behaviour |
|---|---|---|
| `safe` | Read-only | Execute automatically |
| `low` | Reversible write | Execute automatically, log it |
| `high` | Irreversible write | **Pause, escalate, require confirmation** |
| `critical` | Financial transaction | **Always escalate. Never auto-execute** |

### G3 — Sensitive data redaction

Any parameter with `sensitive: true` must never reach the artifact, the evidence logs, screenshot metadata, or stdout. Replacement happens **before** the write, never after — no sensitive value touches disk.

- `redact_sensitive(artifact, params)` → params dict with sensitive values → `REDACTED`
- `redact_step_log(step, redacted_params)` → templated values in step logs → `REDACTED`, serialized via Pydantic v2 `model_dump()`

**Channels redaction alone does not cover** (§11 C12) — these need separate handling, and the residue is a stated limit in REPORT §Safety:

| Channel | Mitigation |
|---|---|
| Playwright trace / video / HAR | Disabled outright in the browser factory |
| Screenshot of a filled credential field | No screenshots between focus and submit on `sensitive` fill steps |
| PII visible on screen in a failure screenshot | Documented limit; would need an OCR redaction pass in production |
| a11y snapshot sent to the LLM containing on-screen PII | **Cannot be fully solved** at this scope. Stated honestly in REPORT |

### G4 — Pre-run validation

The §6.1 fail-fast sequence. Never take an irreversible action only to fail mid-flow.

### G5 — Discovery-phase prompt restrictions

The LLM has more latitude during discovery, so the system prompt hard-codes: only the four permitted actions; no navigation outside allowlisted domains; escalate rather than proceed on any `high`/`critical` action. Proposals violating these are rejected by the agent and re-prompted — the guard is in code, not just in the prompt.

---

## 8. Human-in-the-Loop Escalation

Grading criterion #4, and the one the PDF explicitly says cannot be a stub.

### 8.1 Triggers

1. **Stuck during discovery** — dead end, or the LLM sets `stuck: true`
2. **Replay cannot recover** — retries exhausted with `on_exhausted: escalate_human`, or Layer 2 budget exceeded
3. **High/critical risk action** — always, before executing

### 8.2 Control transfer

```
AUTOMATION_IN_CONTROL
      │ stuck / risky step
      ▼
   pause, save context, notify operator, expose live session
      ▼
HUMAN_IN_CONTROL ── performs manual steps ── signals resume
      ▼
AUTOMATION_IN_CONTROL (resumes)
```

**Exactly one controller at any moment.** The `control` field is `"automation"` or `"human"` — never ambiguous.

### 8.3 `SessionHandoffManager`

Holds browser, page, context, `session_id`, `capability_id`, `control`, and an `asyncio.Event`.

`escalate_to_human()`:
1. `control = "human"`
2. **`event.clear()` — at the START, before the await.** If a second escalation occurs in the same run and the event was not cleared, `await` returns immediately and the automation resumes without ever waiting for the human. This is the single most likely bug in this module.
3. Build the operator console URL (`localhost:8080?session_id=…`)
4. Assemble the intervention request: session id, capability, step id + description, reason, screenshot path, console URL, timestamp
5. Save to `evidence/interventions.json`
6. Print the URL
7. `await event.wait()` — blocks

`resume_from_human()`: `control = "automation"` → `event.set()` → `log_handoff_complete()`.

### 8.4 Operator console (FastAPI, `localhost:8080`)

- `GET /operator` — HTML: capability, current step, reason for stopping, screenshot, **Resume** button
- `POST /resume/{session_id}` — looks up the manager in the module-level registry, calls `resume_from_human()`

Helpers in `src/escalation/handoff.py`: `capture_screenshot(page, step_id, label)`, `save_intervention_request(d)`, `register_session(id, mgr, ctx)`, `get_session_manager(id)`, `get_session_context(id)`, `log_handoff_complete()`.

**The browser is never closed or refreshed during a handoff.** The human works in the same physical window, at the exact state the automation left it. Preserved across the boundary: browser instance, cookies and session state, runtime variables extracted so far, and the evidence log.

### 8.5 Two resume semantics — do not conflate

| Trigger | Who does the step | Resume point |
|---|---|---|
| Stuck / unrecoverable (1, 2) | **The human** does it manually | Automation resumes at step **N+1** |
| Risk confirmation (3) | **The automation** does it, after authorization | Automation executes step **N**, then continues |

### 8.6 Stuck detection signals

1. **Dead-end loop** — `StuckDetector` compares consecutive a11y snapshots; 3 identical → escalate. Counter resets on any change.
2. **Retry exhaustion** — per-step error counts vs `error_map` `max_retries` (falling back to `_default`, then to global `max_retries_per_step`); at the cap with `on_exhausted: escalate_human` → escalate.
3. **LLM stuck flag** — `decision.stuck == true` during discovery.

### 8.7 Recorded per handoff

session id, capability id, step id, reason, handoff-start and resume timestamps, human-control duration, screenshots before and after, and the browser URL at both points → `evidence/interventions.json`.

**Scope cut (from the PDF):** real-time co-browsing is out of scope. The mechanism and control-transfer model are real; the operator UI is minimal.

---

## 9. Evidence & Observability

### 9.1 Run log

Top level: `run_id`, `capability_id`, `mode`, `started_at`, `finished_at`, `result_type`, `layer2_llm_calls`, `outputs`, `screenshot`.

Per step: `step_id`, `action`, `description`, `timestamp`, the specific locator used, `layer_used`, `pre_condition` result, `post_condition` result, outcome, `duration_ms`.

`layer_used` values: `null` (navigate steps — no locator), `"accessibility_tree"`, `"screenshot"`.

### 9.2 Required deliverables

| Path | Content |
|---|---|
| `artifacts/*.json` | The capability artifacts (**root level, not inside evidence/**) |
| `evidence/discovery_run_orangehrm.json` | Real LLM-driven discovery |
| `evidence/replay_run_orangehrm.json` | Deterministic replay — should show `layer2_llm_calls: 0` |
| `evidence/replay_error_run.json` | Error/exceptional-state detection |
| `evidence/interventions.json` | Escalation records |
| `evidence/screenshots/*.png` | discovery final, replay final, error failure |

### 9.3 Error-scenario run

Produced by the negative probe (§5.5): replay with a non-existent employee name → detect `No Records Found` → return `business_outcome`, not a crash. If time allows, also do an injected-failure variant (point a locator at a wrong name) to demonstrate the `hard_failure` path with retries and full debug output — the two together show both halves of the trichotomy.

---

## 10. Heterogeneity & Multi-Tenant — *design only, not built*

### 10.1 The seam

The artifact separates **WHAT** (the recorded flow — click Search, fill Member ID, extract Balance) from **HOW** (finding that element on this surface). The flow never changes across surfaces; the locator strategy changes completely.

| Surface | Locator strategy | Tool |
|---|---|---|
| Modern web | ARIA roles/labels | Playwright |
| Legacy web | Screenshot coordinates | Playwright |
| Windows desktop | UI Automation `automation_id` | pywinauto / WinAppDriver |

**Current build:** `locators` has `primary`/`fallback` keys (web only).
**Future design:** `locators` keyed by surface type (`modern_web`, `legacy_web`, `desktop_windows`); a surface router reads `surface_type` and dispatches to the right resolver.

Adding desktop support touches exactly two things: swap Playwright for pywinauto **in the locator resolver**, and add the `desktop_windows` locator block. The error map, business outcomes, session recovery, and escalation are all flow-level and stay identical. That is the whole argument, and it is only credible because the seam already exists in the built code.

### 10.2 Multi-tenant reuse

Hundreds of tenants, same vendor app, configured differently. One base artifact + small per-tenant override files.

```jsonc
{
  "base_artifact": "lookup_employee_orangehrm_v1.0.0.json",
  "tenant_id": "bank_a",
  "overrides": {
    "target.url": "https://bankA.orangehrm.com",
    "steps[4].locators.primary.methods[0].name": "Find",
    "steps[4].locators.fallback.coordinates.y": 480,
    "known_interstitials[+]": { "name": "bank_a_welcome_banner", "...": "..." }
  }
}
```

Merged at load time, before execution. A new tenant on the same underlying app is onboarded with one override file and **no re-recording**.

**Drift detection:** (1) a replay failing `element_not_found` on a step that previously passed is a strong UI-change signal — the log captures expected locator vs. what was actually on screen; (2) scheduled canary runs with known-good inputs; a failing canary suspends that tenant's artifact from production until reviewed.

---

## 11. Corrections & Improvements to the Notion Design

Each item: what the design says, why it's a problem, and the decision now in force. **These override Notion.**

### C1 — Full-page screenshot coordinates are not clickable coordinates 🔴
Notion (Element Interaction, Discovery) says "take a full page screenshot" and then click the returned coordinates. `page.mouse.click(x, y)` operates in **viewport** space. A full-page screenshot stitches the entire scrollable page, so any `y` below the fold maps to a point that cannot be clicked — silently clicking the wrong element.
**Decision:** Layer 2 always uses `screenshot(full_page=False)`. If the target is below the fold, scroll first, record `scroll_y` in the fallback locator, and have replay restore that scroll offset before clicking.

### C2 — Pinning width×height is not enough for coordinate stability 🔴
Notion pins 1280×720 only. Coordinates also shift with device pixel ratio, headless vs. headful rendering (scrollbars, font rasterization), locale-driven text length, and CSS animations mid-screenshot.
**Decision:** `replay_config.browser` pins `device_scale_factor: 1`, `is_mobile: false`, `headless`, `locale`, `timezone_id`, and `reduced_motion: "reduce"`. Replay verifies the full block, not just the viewport.

### C3 — "Determinism" cannot come from the sampler 🟡
A natural instinct is `temperature=0`. On Opus 5 / Sonnet 5, sampling parameters are **rejected with a 400**.
**Decision:** Never send sampling params. Determinism is structural — the artifact is the deterministic object and replay makes zero LLM decisions. Use `output_config.effort` to tune depth. This strengthens the REPORT argument rather than weakening it.

### C4 — Contradiction: are fallback coordinates always stored? 🟡
The Discovery page says coordinates are left `null` when the a11y tree worked. The Element Interaction and Target Apps pages say coordinates are stored for **every** step as a safety net.
**Decision:** always store them — and note they are **free**: when Layer 1 resolves an element, Playwright already holds the handle, so `bounding_box()` yields the centre point with **no vision call**. Every step gets a real Layer 2 net at zero cost.

### C5 — Post-conditions can be trivially true 🟡
If a checkpoint is generated from the state observed *after* acting, it may have been true beforehand too (e.g. `page_contains_text: "Search"` on a page with a permanent Search link). Such a checkpoint verifies nothing and will pass even when the click failed.
**Decision:** derive post-conditions from an a11y snapshot **diff**, preferring something that changed. At record time, evaluate the candidate against the *pre-action* snapshot and reject it if it already held.

### C6 — `business_outcomes` cannot be observed on a happy-path run 🔴
Notion says discovery "adds business_outcomes for any known non-error results observed during discovery." A successful run never sees "No Records Found," so this list would always be empty or hallucinated.
**Decision:** add an explicit **negative probe pass** after successful discovery — re-run the parameterized flow with a deliberately invalid input and record the actual on-screen text. Populates `business_outcomes` from observation *and* produces the required error-scenario evidence in one pass.

### C7 — Parameterization by string replacement is unsafe 🟡
Replacing every occurrence of `"Admin"` with `{{auth_username}}` will also rewrite unrelated fields — a locator named `"Admin Settings"` becomes `"{{auth_username}} Settings"` and breaks replay in a way that is very hard to trace.
**Decision:** parameterize by **provenance**. The agent knows, at fill time, that it injected a parameter value; only that `fill` step's `value` is templated. No post-hoc scanning of the artifact.

### C8 — Self-healing must not mutate the versioned artifact 🟡
Notion's `healing_action: update_artifact` writes new coordinates into the artifact during replay. That makes the artifact non-reproducible, silently invalidates the evidence for prior runs, and means two concurrent replays can race on the same file.
**Decision:** write healed coordinates to a **sidecar patch** (`artifacts/heal/<capability>_<run_id>.patch.json`) plus a review request. The versioned artifact changes only through human review, with a version bump. Preserves the "versioned and reviewable" property being graded.

### C9 — Interstitial probing will dominate runtime 🟡
Three interstitials checked before every step, each at the 8000 ms default timeout, across ~8 steps, is up to ~3 minutes of pure waiting on a flow that should take seconds.
**Decision:** separate `interstitial_probe_timeout_ms: 250` from `default_timeout_ms: 8000`. Detection is a fast probe; only an actually-present interstitial gets the full dismiss timeout.

### C10 — Default replay mode should be `strict`, not `assisted` 🟡
The PDF's hard requirement is that the LLM is not in the decision loop. Notion defaults to `assisted`, so the headline evidence file would show non-zero LLM calls and invite exactly the wrong question.
**Decision:** default to `strict`; the graded replay evidence run uses `strict` and shows `layer2_llm_calls: 0`. `assisted` remains available as an opt-in resilience demo. REPORT argues the distinction explicitly: Layer 2 is **locator resolution**, not decision-making — the step sequence is fixed by the artifact either way.

### C11 — Guru99 is a single point of failure for a graded deliverable 🟠
`demo.guru99.com` is a public demo with emailed credentials that expire, periodic data resets, and regular downtime. If it is down on submission day, the entire Layer 2 demonstration disappears.
**Decision:** build a small **local legacy mock** (`mock/legacy_bank.html` — table-based layout, zero ARIA, served by the same FastAPI process on `:8081`). Guaranteed available, guaranteed no accessible names, no credential dependency, and it makes the graders' run reproducible. Still attempt the real Guru99 run and include it if it succeeds. Documented under REPORT §Cuts as a deliberate reproducibility choice, not a dodge.

### C12 — Redaction covers values, not channels 🟠
`sensitive: true` redaction handles logged *values*. Secrets and PII also escape via Playwright traces/videos/HAR, screenshots taken while a credential field is focused, failure screenshots of PII-bearing screens, and the a11y snapshot sent to the LLM.
**Decision:** disable trace/video/HAR in the browser factory; suppress screenshots between focus and submit on `sensitive` fill steps; document the failure-screenshot and a11y-snapshot exposure as stated limits in REPORT §Safety. Naming a limit you cannot close is worth more than implying full coverage.

### C13 — `step_N` keys fail silently 🟡
`error_map.step_5` is coupled to step ordering. Insert a step, or typo the number, and the entry silently falls through to `_default` with no warning — the per-step handling you designed simply never runs.
**Decision:** load-time cross-reference validation. Every `error_map.step_N` key and every `business_outcomes.step_ids` entry must resolve to an existing `step_id`, and every `output_key` must exist in `output_schema`. Unresolvable → reject the artifact.

### C14 — `expected_type: currency` has no normalization contract 🟡
Extracting a balance yields `"$1,234.56"`. Without a defined rule, type validation is decorative and downstream callers get an unparseable string.
**Decision:** strip currency symbols and thousands separators, parse as `Decimal`, and store `{raw, normalized}`. Unparseable → `hard_failure`.

### C15 — Test plan is under-specified and non-hermetic 🟡
Notion lists two test files, and `pytest-asyncio` — required for async Playwright tests — is absent from the dependency list and from this machine.
**Decision:** add `pytest-asyncio`; tests run against **local fixture HTML**, never live demo sites, so the suite passes on a grader's machine with no network and no credentials. Test plan in §14.

### C16 — Artifacts lack provenance 🟢
Nothing ties an artifact to the discovery run that produced it — which weakens the "versioned and reviewable" property being graded.
**Decision:** add a `provenance` block: `source_run_id`, `schema_version`, `model_id`, and a content hash of the steps array. Cheap to add, directly serves criterion #1.

### C17 — Step 1 cannot have an element pre-condition 🟢
A `navigate` step runs before any page exists.
**Decision:** step 1 `pre_condition` is always explicitly `null`; its `post_condition` is `url_contains`.

### C18 — No resume for an interrupted replay 🟢
If the process dies mid-flow there is no checkpoint to resume from.
**Decision:** accepted cut. Documented in REPORT §Cuts, with the note that production would checkpoint runtime outputs + step index per step.

---

## 12. Decisions — resolved 2026-08-20

| # | Decision | Chosen | Notes |
|---|---|---|---|
| **D1** | Model ID | **`anthropic/claude-sonnet-5`** | Reached via OpenRouter (D5). Set in `src/settings.py:MODEL_SLUG`, overridable via `BLUEPRINT_MODEL`. Handles both the a11y-tree text loop and the vision fallback, so discovery needs one model. Recommendation had been Opus 5 for reasoning depth; Sonnet 5 is cheaper at the same 1M context and vision support, and swapping is now a one-line env change |
| **D2** | Legacy surface | **Both** | Guru99 credentials received and in `.env`. Local zero-ARIA mock still to be built (§11 C11) as the reproducible fallback |
| **D3** | Repo name | **`blueprint-agent`** | Matches the working directory; `pyproject.toml` name set |
| **D4** | Python env | **uv** | `uv sync` + `uv run`; `.venv/` gitignored, `uv.lock` committed. Replaces the `requirements.txt` flow the plan originally assumed |
| **D5** | Model gateway | **OpenRouter** | OpenAI-compatible endpoint via the `openai` SDK; the `anthropic` SDK is removed. One key, every model, so comparing candidates for the discovery loop is a config change. All provider knowledge is confined to `src/llm/` |

### Consequences of D1 + D5 worth remembering

Verified against OpenRouter's live model catalogue for `anthropic/claude-sonnet-5`
(1M context, `text`/`image`/`file` input, $2/$10 per MTok, cache read $0.20/MTok):

- **Structured outputs are supported** (`structured_outputs`, `response_format`), so the discovery decision loop keeps its schema-valid-by-construction guarantee. This was the one capability that could have blocked the switch.
- **Sampling parameters are absent** from the model's supported set — no `temperature`, `top_p`, or `seed`. Identical to the first-party constraint, so §11 C3 stands unchanged: determinism is structural, not sampler-based.
- **Reasoning depth** maps to OpenRouter's unified `reasoning: {effort: ...}` rather than Anthropic's `output_config.effort`. Set from `settings.DISCOVERY_EFFORT`.
- **Prompt caching** works via a pass-through `cache_control` breakpoint on the system prompt; cache hits show up as `usage.prompt_tokens_details.cached_tokens`. The discovery system prompt carries the full schema spec and is byte-stable, so it is worth caching.
- **Routing is pinned** (`provider.order = ["anthropic"]`, `allow_fallbacks: false`). A gateway silently substituting a different upstream provider mid-run is the wrong failure mode for this project specifically; opt back in with `BLUEPRINT_ALLOW_PROVIDER_FALLBACK=1`.

### New correction

**C19 — A model gateway can silently re-route.** 🟡
OpenRouter may fall back to a different upstream provider when the preferred
one is unavailable. For a system whose thesis is deterministic replay, an
invisible provider swap mid-discovery undermines the evidence: two runs of the
"same" configuration could be served by different infrastructure.
**Decision:** pin `provider.order` and disable fallbacks by default; record the
serving provider and generation id in the evidence log for every call, so the
discovery run is attributable after the fact.

**C20 — "Sparse accessibility tree" is the wrong test for a legacy page.** 🔴
The design assumes a legacy surface yields an *empty or near-empty*
accessibility tree, and uses named-node count as the Layer 1 / Layer 2 switch.
It does not. A table-based page produces plenty of named nodes, because `cell`
and `row` inherit an accessible name from their text content. The local mock
yields **fourteen** — `cell "LOGIN"`, `cell "Password :"`, `row "LOGIN"` —
while exposing no button and no named input at all. Under a named-node count
the mock reads as a *rich* page, Layer 1 is attempted, every locator fails, and
the screenshot fallback never fires. Caught by the test asserting the mock is
sparse, not by reading.
**Decision:** judge sparseness on **named nodes with interactive roles**
(`button`, `link`, `textbox`, `combobox`, …), not named nodes in general.
Threshold is 2: any page you can meaningfully drive exposes at least a control
and a target. `Observation.interactive_count` is the switch; `named_node_count`
is kept for diagnostics only.

---

## 13. Work Breakdown

### Phase 0 — Setup (no application code)

- [x] Request Guru99 demo credentials *(received, stored in `.env`)*
- [ ] Obtain Anthropic API key → `.env` *(still blank; blocks Phase 2 only)*
- [x] Resolve D1–D4 (§12)
- [x] `git init`
- [ ] Create public GitHub repo (`gh repo create blueprint-agent --public`)
- [x] `.gitignore` — written **before** `.env` existed; `git check-ignore` verified
- [x] `.env.example` committed with no values; `.env` populated locally
- [x] `uv init` + `uv add` deps + `playwright install chromium`; `uv.lock` committed
- [x] Create the §3.4 folder structure
- [x] **`src/artifact/schema.py`** — 21 enums + 24 models, all validators (§13.1)
- [x] `tests/fixtures/golden_artifact.json` — realistic 8-step OrangeHRM capability
- [x] `tests/test_schema_validation.py` + `tests/test_cross_references.py` — **79 tests passing**
- [x] `src/settings.py` — model id, paths, discovery limits, credential accessors
- [x] `config/allowlist.json`
- [x] `main.py` CLI — `validate` fully working; `discover` / `replay` stubbed
- [x] `README.md` + `REPORT.md` shells with required headings
- [x] First commit

### Phase 1 — Foundation ✅ complete

- [x] `src/artifact/validator.py` — load, parameter checks, escalation availability, browser match
- [x] `src/session/browser.py` — pinned context factory (§4.3); trace/video/HAR never enabled (§11 C12); viewport-only screenshots (§11 C1)
- [x] `src/safety/guardrails.py` — allowlist + risk gate. **`check_origin` vs `check_url` split**: `target.url` names the app and is normally a bare origin, so route patterns must not apply to it
- [x] `src/safety/redaction.py` — literal values + `{{template}}` names, longest-first, `model_dump()` walk
- [x] `src/evidence/logger.py` — run log with `llm_calls_made` and per-call provider attribution (§11 C19)
- [x] `mock/legacy_bank.html` + `mock/server.py` on `:8081` (§11 C11), with tests asserting Layer 1 genuinely fails on it
- [x] **176 tests passing**, no network required

### Phase 2 — Discovery ✅ complete

- [ ] `src/agent/decisions.py` — `AgentDecision` model for `messages.parse`
- [ ] `src/agent/prompts.py` — all prompts; system prompt byte-stable for caching
- [ ] `src/agent/discovery.py` — observe→decide→act, stopping conditions, step recorder
- [ ] Free Layer 2 capture via `bounding_box()` (§11 C4)
- [ ] Provenance-based parameterization (§11 C7)
- [ ] Diff-based post-condition generation (§11 C5)
- [ ] **▶ First real discovery run on OrangeHRM → save evidence**
- [ ] Negative probe pass → `business_outcomes` (§11 C6)

### Phase 3 — Replay ✅ complete

- [ ] `src/replay/locator.py` — Layer 1 chain, Layer 2 fallback, scroll restore (§11 C1)
- [ ] `src/replay/engine.py` — pre-flight, per-step sequence, three result types
- [ ] `src/replay/error_handler.py` — the three categories, retry/exhaustion
- [ ] Session recovery + interstitial dismissal (fast probe, §11 C9)
- [ ] Extraction + `currency` normalization (§11 C14)
- [ ] **▶ First real replay in `strict` mode → save evidence, confirm `layer2_llm_calls: 0`**

### Phase 4 — Escalation ✅ complete

- [ ] `src/escalation/handoff.py` — `SessionHandoffManager` (**`event.clear()` first**, §8.3)
- [ ] Helper functions + module-level session registry
- [ ] `src/escalation/console.py` — FastAPI `GET /operator`, `POST /resume/{id}`
- [ ] Both resume semantics (§8.5)
- [ ] `StuckDetector` — all three signals
- [ ] **▶ Demonstrate a real handoff → `evidence/interventions.json`**

### Phase 5 — Generalization ✅ (evidence runs pending flows)

- [ ] `src/artifact/merge.py` + `config/tenants/` override demo (§10.2)
- [ ] Self-healing sidecar patch writer (§11 C8)
- [ ] **▶ End-to-end on the legacy surface — proves Layer 2 does real work**
- [ ] **▶ Error-scenario replay → `evidence/replay_error_run.json`**

### Phase 6 — Write-up

- [ ] `REPORT.md` — all 7 sections (§16)
- [ ] `README.md` — setup + exact copy-pasteable demo commands
- [ ] Verify every evidence file exists and is non-empty
- [ ] Fresh-clone test: clone to a new directory and follow the README verbatim
- [ ] Confirm no secret is present anywhere in git history

**Build order rationale:** schema first because everything depends on it; discovery before replay because you need a real artifact to replay; error handling before escalation because escalation is a special case of it; safety last because it wraps everything.

### 13.1 `schema.py` inventory

**Enums:** `SurfaceType`, `RiskLevel`, `ActionType`, `RecordedBy`, `LocatorStrategy`, `AccessibilityMethod`, `ConditionType`, `OnFail`, `ErrorCategory`, `ErrorAction`, `ErrorTypeKey` — all `(str, Enum)`.

**Models:** `InputParameter`, `AccessibilityLocatorMethod`, `PrimaryLocator`, `ScreenshotCoordinates`, `ScreenshotRegion`, `ScreenshotLocator`, `Locators`, `Condition`, `Extraction`, `Step`, `BrowserConfig`, `ReplayConfig`, `SessionRecovery`, `Interstitial`, `BusinessOutcome`, `ErrorHandler`, `SelfHealing`, `Provenance`, `Artifact`.

**Validators** (Pydantic v2 `@field_validator` / `@model_validator` — never v1 `@validator`):

| Target | Rule |
|---|---|
| `InputParameter.sensitive` | must be exactly `bool` — reject `"true"`, `"yes"`, `1` |
| `Extraction.output_key` | `snake_case`; no spaces, hyphens, uppercase |
| `Artifact.capability_id` | `snake_case` |
| `Artifact.version` | three dot-separated integers |
| `Step` (model) | `fragile` → `fragile_reason` required |
| `Step` (model) | `action == extract` → `locators` must be `None` **and** `extractions` required |
| `Step` (model) | `post_condition` required on every step |
| `Artifact` (model) | cross-references resolve (§11 C13) |

---

## 14. Test Plan

All tests hermetic — local fixture HTML, no network, no credentials. Requires `pytest-asyncio`.

| File | Covers |
|---|---|
| `test_schema_validation.py` | Every validator in §13.1; golden valid artifact loads; each malformed variant rejected with a clear message |
| `test_cross_references.py` | §11 C13 — dangling `step_N`, bad `step_ids`, `output_key` missing from `output_schema` |
| `test_redaction.py` | Sensitive values never appear in artifact, step log, or result; `model_dump()` path covered |
| `test_allowlist.py` | Off-domain navigation blocked; disallowed action types blocked; localhost mock permitted |
| `test_locator_chain.py` | Method priority order; fragile skips Layer 1; strict mode escalates instead of calling the LLM |
| `test_replay_engine.py` | Against a local fixture page: success path, business outcome, hard failure with retries; `expected`/`observed` populated |
| `test_error_handler.py` | Category routing; per-step overrides beat `_default`; `on_exhausted` honored |
| `test_handoff.py` | `event.clear()`-before-await — **two sequential escalations in one run must both block** (§8.3) |
| `test_currency.py` | §11 C14 normalization, including an unparseable value → `hard_failure` |
| `test_tenant_merge.py` | Base + override merge; dot-notation paths; array append |

`fixtures/` holds a modern page (rich ARIA) and a legacy page (zero ARIA) so both layers are testable offline.

---

## 15. Risk Register

| Risk | L | Impact | Mitigation |
|---|---|---|---|
| Guru99 down / credentials expired at submission | High | Layer 2 demo lost | Local legacy mock (§11 C11) |
| OrangeHRM demo resets or changes UI mid-build | Med | Discovery artifact goes stale | Artifacts are cheap to re-record; keep the discovery run repeatable |
| Escalation judged a stub | Med | **Explicit auto-fail criterion** | Build a genuinely working handoff early (Phase 4), with evidence |
| Coordinate drift makes Layer 2 flaky | Med | Fallback unconvincing | Full browser pinning (§11 C2) + viewport-only screenshots + scroll offset (§11 C1) |
| Discovery burns budget looping | Low | Cost, time | 25-step cap, 300 s timeout, dead-end detection |
| Secret committed to a public repo | Low | **Severe** | `.gitignore` first commit; history scan before submission |
| Over-building one component | Med | Fails "depth over breadth" | Timebox each phase; thin-but-real everywhere before polishing anything |

---

## 16. REPORT.md Outline

Exactly the seven PDF headings. Each maps to a section here.

| § | Heading | Draw from | Must state |
|---|---|---|---|
| 1 | Architecture | §3, §11 C11 | Two phases; why two target apps; the local-mock reproducibility choice |
| 2 | Artifact Schema | §4, §11 C1/C2/C4/C8/C16 | Why the two-layer locator; why fallbacks are always stored and free; versioning and reviewability |
| 3 | Determinism & Error Handling | §6, §11 C3/C5/C10 | Determinism is structural, not sampler-based; Layer 2 = locator resolution ≠ decision-making; the three categories |
| 4 | Heterogeneity & Multi-Tenant | §10 | The WHAT/HOW seam; desktop = swap the resolver only; base + override; drift detection |
| 5 | Escalation & Handoff | §8 | Three triggers; same live session; two resume semantics; what gets recorded |
| 6 | Safety | §7, §11 C12 | Five guardrails **and the limits** — a11y snapshots and failure screenshots can still carry PII |
| 7 | Cuts | §11 C11/C18, §8.7 | Co-browsing out of scope; no mid-replay resume; desktop is design-only; local mock rationale |

§3 and §6 are where the corrections in §11 turn into visible design judgment. Write them last, when the evidence files exist to cite.

---

## 17. Definition of Done

- [ ] Public GitHub repo; no secret in any commit
- [ ] `README.md` works verbatim from a fresh clone
- [ ] `REPORT.md` — all 7 sections, substantive
- [ ] ≥1 **real** LLM discovery run with evidence on disk
- [ ] Deterministic replay of that artifact, `strict` mode, `layer2_llm_calls: 0`
- [ ] Error-scenario replay returning a structured `business_outcome`
- [ ] Human escalation demonstrated end-to-end with an intervention record
- [ ] Layer 2 shown doing real work on a legacy surface
- [ ] Allowlist enforced; sensitive values redacted everywhere they could surface
- [ ] Artifact is typed, versioned, cross-reference-validated, and readable by a human
- [ ] Test suite green with no network access
- [ ] Every cut in §11 and §16 explicitly stated in REPORT.md
