# REPORT — Blueprint Agent

Design write-up for the computer-use automation take-home.

> **The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the agent invokes it in production.**

**Repo:** <https://github.com/shivamshinde123/blueprint-agent> · **Target:**
saucedemo.com (modern) and demo.guru99.com (legacy) · **Model:**
`anthropic/claude-sonnet-5` via OpenRouter · **346 tests, lint clean**

Everything claimed here is backed by a committed evidence file or a test. Where
something is designed but not demonstrated, it says so.

---

## 1. Architecture

### The two phases, and why the boundary is where it is

**Discovery** puts a model in the loop against a live browser: observe the page,
decide one action, execute it, record what worked, repeat. It runs once per
capability.

**Replay** reads the recorded artifact and executes it mechanically. In strict
mode — the default — no model is contacted at all.

The boundary matters more than either half. A system that keeps a model in the
production path inherits its latency, its cost, and its variance on every
invocation. Freezing the *successful* run into a typed artifact converts a
probabilistic capability into a deterministic one, and that is the only way an
agent can call it a thousand times a day with a straight face.

The seam is the artifact. Everything upstream of it is allowed to be
non-deterministic; nothing downstream is.

### Demonstrated

```
recorded with  "Sauce Labs Backpack"   -> $29.99   6/6 steps, 0 model calls
replayed with  "Sauce Labs Bike Light" -> $9.99    6/6 steps, 0 model calls
```

Each with its own correct description.
Evidence: `evidence/discovery_run_saucedemo.json`,
`evidence/replay_run_sauce_labs_backpack.json`,
`evidence/replay_run_sauce_labs_bike_light.json`.

The second line is the one that matters. Replaying with the value it was
recorded with proves nothing — it only proves the transcript replays. Replaying
with a **different** value is what distinguishes a reusable capability from a
recording.

### Target applications

**saucedemo.com** — a modern React store, exercising the accessibility-tree
path. Chosen after abandoning OrangeHRM, which I originally selected: its demo
dataset resets (the employee I was asked to look up stopped existing
mid-session), its search is a typeahead that silently rejects values not picked
from its dropdown, and 7 of 15 profile loads timed out during probing. Building
a graded deliverable on it was a reliability risk with no upside.

**demo.guru99.com** — a genuinely legacy bank: table-based layout, form inputs
with no accessible name at all. It exists in this project to force the
screenshot fallback to do real work.

**A local zero-ARIA mock** (`mock/legacy_bank.html`) backs the legacy story in
CI. Two tests assert its premise rather than assuming it: a well-marked-up page
resolves via `get_by_role`/`get_by_label`/`get_by_placeholder`, and the mock
resolves via none of them. If the mock ever drifts into being accessible, that
test fails and says so.

### Model access

Reached through **OpenRouter**, an OpenAI-compatible gateway, so trying a
different model is a config change rather than a rewrite. Nothing outside
`src/llm/` imports a provider SDK.

Routing is **pinned** to one upstream provider with gateway fallback disabled.
A gateway silently substituting a different provider mid-run is precisely the
wrong failure mode for a determinism-focused system — two runs of nominally the
same configuration could be served by different infrastructure. The serving
provider and generation id are recorded per call so any discovery run is
attributable after the fact.

### Trade-offs taken

| Decision | Cost | Why anyway |
|---|---|---|
| Strict mode holds *no* model client | Cannot self-heal mid-run | Makes "zero model calls" structural rather than promised — no later edit can accidentally introduce one |
| Reject an artifact that embeds run data | Discovery fails more often | A silently-wrong capability is worse than a failed recording |
| Accessibility-first locators | More work than CSS selectors | Identity survives a redesign; position and DOM structure do not |
| One model for text and vision | Not the cheapest per call | One integration, one failure mode, one thing to reason about |

---

## 2. Artifact Schema

Nine top-level blocks: capability contract, replay config, session recovery,
known interstitials, steps, business outcomes, error map, self-healing,
provenance. Full field reference in `PLAN.md` §4.

### The two-layer locator

Every step records **how to find its element twice**:

```jsonc
"locators": {
  "primary":  { "strategy": "accessibility_tree",
                "methods": [ /* tried in priority order */ ] },
  "fallback": { "strategy": "screenshot",
                "coordinates": {...}, "scroll_y": 0,
                "viewport": {...}, "visual_description": "..." }
}
```

Layer 1 is `get_by_role` → `get_by_label` → `get_by_placeholder` →
`get_by_text`. Layer 2 is a screenshot and a vision call returning pixel
coordinates.

**The fallback is recorded for every step, including ones Layer 1 resolved
instantly — and it costs nothing.** When Layer 1 finds an element, Playwright is
already holding the handle, so `bounding_box()` yields the centre point with no
vision call. A complete safety net for free was too good to skip.

Two things about those coordinates are load-bearing:

- They are **viewport-relative**, taken from a viewport-only screenshot. A
  full-page screenshot stitches the whole scrollable document, so its y-values
  do not map to `mouse.click` — the click would land somewhere else entirely,
  silently.
- `scroll_y` is recorded alongside them, because a coordinate only addresses the
  right element at the scroll position it was captured at.

### Two locator strategies real markup forced

**`get_by_field_label`** — the control sitting in the same field group as a
visible caption. A caption is only reachable by `get_by_label` when the markup
actually wires it to its control, and plenty of applications never do: a form
renders a visible "Date of Birth" label whose input carries no accessible name,
so every accessible-name method returns nothing while a human reads it
instantly. Still identity-based — it keys on the caption a person reads.

**Shape addressing** — `get_by_text` with a regex. The last resort for a value
with no label, no role, and whose own text *is* the value being read. A price in
a bare `<div>` cannot be named without naming the price, which is circular.
`\$[\d,.]+` finds it on any product page, for any price, without naming one.

### Why the artifact is reviewable as well as executable

- Every step carries a human-readable `description`, shown to the operator
  during a handoff.
- `pre_condition` is written as explicit `null` rather than omitted, so a reader
  can tell the omission was deliberate.
- The `action` field name appears in four contexts with four disjoint value
  sets; the latter two are deliberately named `dismiss_action` and
  `healing_action` so they can never be confused.
- `provenance` ties the artifact to the run that produced it — run id, model,
  and a hash of the steps.

### Validation that fails at load, not mid-flow

`extra="forbid"` throughout: a typo'd field in a permissive schema is a silent
no-op, and the engine would simply never see the value.

Cross-reference checks catch the failures that do not announce themselves — a
stale `step_9` key in the error map falling through to `_default` so the
per-step handling never runs; an `output_schema` field no extraction produces,
where replay "succeeds" and hands the caller a result missing a promised field;
a `{{param}}` that no input declares.

`sensitive` is validated as a *strict* boolean. Pydantic would happily coerce
the string `"no"` — which is truthy — and quietly disable redaction on a real
password.

---

## 3. Determinism and Error Handling

### Determinism is structural, not sampled

The obvious approach is `temperature=0`. It is not available: current models
reject sampling parameters outright, and OpenRouter's capability listing for
`anthropic/claude-sonnet-5` omits them.

This turns out to sharpen the design rather than weaken it. **Determinism lives
in the artifact and the model-free replay path, not in the sampler.** Discovery
is allowed to be probabilistic because its output is reviewed, versioned, and
frozen. Replay is deterministic because it makes no decisions at all.

In strict mode `replay()` hands the engine a `None` client. Zero model calls is
therefore a property of the wiring rather than a promise the code might later
break — and there is a test asserting it.

### The one property nothing else checks

An artifact can have a valid schema, resolvable locators, passing checkpoints
and correct cross-references, and still be worthless: it works for the exact
values it was recorded with and silently returns the wrong answer for anything
else.

Three separate instances shipped past a green test suite:

| Where the run's data leaked | What was recorded | Consequence |
|---|---|---|
| Checkpoint | `page_contains_text: "Anderson"` after a search | The surname that search happened to return. Passes for one record, fails for every other |
| Click locator | `option "Peter Mac Anderson"` | The typeahead suggestion for one specific record |
| Extraction locator | `get_by_text("$29.99")` to read a price | **The worst.** Circular — finds the element only when the answer is already known. Replay *succeeds* and reports $29.99 for every product. Nothing signals a problem |

`src/artifact/reusability.py` states the rule once:

> No locator and no checkpoint may contain a value that came from this run.

It is used twice: as immediate feedback while recording, so the model can pick a
different locator while the page is still in front of it, and as a backstop at
assembly that refuses to write a violating artifact. `{{template}}` references
are the correct way to depend on an input and are never violations. The check
runs with the values in memory and persists none of them — storing them to
re-check later would mean writing a person's date of birth into a file that
ships in the evidence folder.

Writing it as a testable rule immediately exposed two holes that twelve live
runs had not: substring matching missed `"Peter Mac Anderson"` for the input
`"Peter Anderson"` (the application interpolated a middle name, so neither
string contains the other), and regex escaping hid a literal — `\$29\.99` did
not register as containing `$29.99`. Both are fixed and tested.

### The three error categories

Ordering matters as much as classification. **Business outcomes are checked
before anything is treated as a failure.**

| Category | Example | Behaviour |
|---|---|---|
| Expected business outcome | "No records found" | Returned as a valid result, `is_error: false`. Not a crash |
| Recoverable | Session timeout, slow load | Retried per the error map, or recovered and resumed |
| Hard failure | The step is genuinely broken | Stop, screenshot, report `expected` versus `observed` |

With the ordering reversed, "no records found" fails its post-condition first
and gets reported as a crash — which makes the capability useless to the agent
calling it. There is a test asserting exactly that ordering.

Retry counters are keyed per `(step, error type)` and **reset on success**, so a
flaky step that recovers does not carry its history into a later re-entry after
session recovery and exhaust a budget it just refilled. An error with no
error-map entry fails rather than continuing: silently proceeding past an
unclassified error is how a replay "succeeds" having done the wrong thing.

### Checkpoints that assert something

A checkpoint generated from the state observed *after* an action can be
trivially true. Candidates are therefore derived from a before/after diff,
tested against the pre-action snapshot, and discarded if they already held.
Candidates are also ranked away from data-bearing roles (`cell`, `row`,
`option`) toward structural ones, and rejected outright if they overlap a
parameter value.

Where nothing observably changed, a weak checkpoint is recorded **and flagged**,
so a reviewer sees it rather than discovering it when replay passes a broken
step.

### Waiting on state, never on a clock

Every condition waits for a *state* with a timeout. The replay engine sleeps
nowhere. Interstitials get a deliberately short probe budget (250 ms) separate
from the action timeout: at the full timeout, three interstitials across eight
steps would add minutes of dead waiting to a flow that should take seconds.

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
per-surface-type keys, with a router reading `surface_type` and dispatching to
the right resolver.

**Adding desktop support touches exactly two things**: swap Playwright for
pywinauto *in the locator resolver*, and add the `desktop_windows` locator
block. The error map, business outcomes, session recovery, risk gating and
escalation are all flow-level and stay identical — they never touch an element,
only a step. That claim is only credible because the seam already exists in
running code: `src/replay/locator.py` is the only module that knows how an
element is found, and the engine calls it through one function.

### Multi-tenant reuse

Hundreds of tenants run the same vendor application configured differently. One
base artifact holds the canonical flow; a small per-tenant file says only what
differs, merged at load time:

```jsonc
"overrides": {
  "target.url": "https://bank-a.example.com",
  "steps[6].locators.primary.methods[0].name": "Basket",
  "steps[6].locators.fallback.coordinates.y": 512,
  "known_interstitials[+]": { "name": "bank_a_welcome_banner", ... }
}
```

A new tenant on the same underlying application is onboarded with one file and
**no re-recording**. The merged result is held to exactly the same validation as
any hand-written artifact, so an override cannot produce something the engine
would refuse.

An override path that matches nothing is an **error, not a no-op** — silently
doing nothing is how a tenant runs for weeks against the wrong button. The
tenant id is folded into `capability_id`, so an evidence log cannot be mistaken
for another tenant's run.

### Detecting drift

1. **Replay failure as signal.** `element_not_found` on a step that previously
   passed is strong evidence the UI changed. The log captures the expected
   locator and what was actually on screen.
2. **Canary runs.** Scheduled replays with known-good inputs; a failing canary
   suspends that tenant's artifact from production until reviewed.

---

## 5. Escalation and Handoff

### Three triggers

1. **Stuck during discovery** — a dead end (the page unchanged across
   consecutive unproductive turns), or the model reporting it cannot proceed
   safely.
2. **Replay cannot recover** — retries exhausted with `on_exhausted:
   escalate_human`, or the assisted-mode vision budget spent.
3. **A high or critical risk action** — always, before executing.

### The same live session

The requirement is specific: the human operates the session the automation was
using, not a fresh one. So the browser is **never closed, refreshed, or
re-navigated** across a handoff. The automation blocks on an `asyncio.Event`;
the operator works in the same window, at the exact state it stopped at.

A test asserts this rather than assuming it: JavaScript state, form contents and
page identity all survive a handoff, and work the operator does in the window is
simply there when the automation resumes.

### One detail worth calling out

The event is cleared **at the start** of `escalate_to_human`, immediately before
the await.

An `asyncio.Event` stays set until something clears it, and a resume can arrive
when nothing is waiting — an operator double-clicking Resume, a retried request.
That leaves the event set, and the next genuine escalation returns instantly:
the automation proceeds with nobody having looked at the screen, while the
evidence log still records a handoff.

I originally justified this with a different scenario and wrote a test for it,
then mutation-tested the claim and found my test had no teeth — clearing *after*
the await passes it too. The test now models the stray-resume case and fails
under both incorrect placements.

### Two resume semantics

| Trigger | Who performs the step | Resume point |
|---|---|---|
| Stuck / unrecoverable | The **human**, by hand | Automation resumes at step N+1 |
| Risk confirmation | The **automation**, after authorisation | Executes step N, then continues |

Conflating these would either double-execute a step or skip it.

### What is recorded

Session id, capability, step, reason, timestamps either side, duration of human
control, the URL before and after, and screenshots either side — appended to
`evidence/interventions.json`.

### The operator console

A minimal FastAPI page on `localhost:8080`. Real-time co-browsing is out of
scope by the brief's own scope note; what has to be real is the **mechanism and
the control-transfer model**. The operator does not work in this page — they
work in the browser window the automation opened, which is still sitting where
it stopped. The page tells them what happened, shows the screen as it was, and
hands control back.

It escapes user-supplied text, refuses a resume on a session that is not paused
(409), and blocks path traversal on screenshot serving.

---

## 6. Safety

### Five guardrails

**1. Allowlist** — permitted domains, route patterns, and action types, checked
before every action in *both* phases. Matching is on the parsed hostname, so
`saucedemo.com.evil.com` is refused rather than passing a substring check.
`file://` is rejected outright. A missing allowlist file refuses to run rather
than defaulting to permissive.

Upload, download and script execution are blocked unconditionally — **enforced
in code, not merely discouraged in the prompt**. A refused proposal is fed back
so the model can correct itself, up to a limit.

**2. Risk classification** — four levels. `safe` and `low` execute
automatically; `high` and `critical` **always** pause for human confirmation,
with no configuration flag to disable it. An artifact containing a risky step
refuses to start without an escalation handler configured, because the
alternative is pausing mid-flow with no way to resume.

An unrecognised risk classification resolves to `low`, never `safe`.

**3. Redaction** — replacement happens *before* the write, never after. It
covers literal secret values and `{{sensitive_param}}` templates, walks nested
structures via `model_dump()` so schema fields added later are covered, and
replaces longest-first so an overlapping shorter secret cannot leave the
remainder of a longer one behind.

Every committed evidence log shows `***REDACTED***` where credentials were used
— the redaction path working on real files, not only in tests.

**4. Not capturing secrets in the first place** — Playwright tracing, video, and
HAR recording are never enabled and are not configurable. All three persist raw
page content including credential fields as they are typed, and redaction cannot
reach inside a video file. Credentials are read from `.env` via `--credentials`
rather than passed on the command line, where they would land in shell history
and the process list.

**5. Fail-fast pre-flight** — schema validation, cross-references, required
parameters, allowlist, and escalation availability are all checked before a
browser opens. Discovering a violation at step 6 means five real actions already
ran against a live system.

### Limits of this model

Stated plainly rather than implied away:

- **A failure screenshot captures whatever was on screen**, which can include
  PII. Redaction cannot reach inside a PNG. Screenshots are deliberately
  excluded from version control for this reason; production would need an OCR
  redaction pass.
- **The accessibility snapshot sent to the model contains whatever the page
  shows**, including any PII on it. This cannot be solved at this scope — the
  agent has to see the page to drive it.
- The allowlist governs *destinations and action types*. It does not constrain
  what the model reasons about once a permitted page is open.
- Risk classification is the model's judgement, checked by a human only at the
  gate. A write mis-classified as `safe` executes without a pause. The
  mitigation is that the prompt instructs erring high and an unparseable value
  resolves to `low`, but the residual risk is real.

---

## 7. Cuts

Deliberate, and each with what I would do instead given more time.

**Real-time co-browsing.** Out of scope by the brief's own note. The operator
uses the same physical browser window the automation opened. Production would
stream the session.

**Desktop and legacy-web surface routing.** Designed (§4), not built. The seam
is real and single-location; the resolver for a second surface is not written.

**Business outcomes are not auto-recorded.** A successful discovery run never
encounters "no results", so outcomes cannot be observed from the happy path. The
fix — a *negative probe* that re-runs the recorded flow once with a deliberately
invalid input and records what the application reports — is designed but not
implemented. Consequence: a bad input currently produces a structured `failure`
rather than a clean business outcome. The engine supports outcomes fully; the
recorder does not yet populate them.

**No mid-replay resume.** If the process dies at step 5 there is no checkpoint
to resume from. Production would persist runtime outputs and step index per
step.

**Self-healing writes a sidecar patch, and nothing consumes it yet.** Healed
coordinates are written to `artifacts/heal/` with a review request rather than
mutating the versioned artifact — an artifact that rewrites itself mid-run is
not reproducible and races with concurrent replays. The review workflow that
would fold a patch back in is not built.

**Discovery reliability is one data point per flow.** It succeeded; I have not
measured a success rate, and it should not be described as reliable.

### What I would build next, in order

1. The negative probe, to complete the error-handling trichotomy with evidence.
2. A canary runner, since drift detection is currently a design claim.
3. The desktop resolver, to prove the surface seam rather than assert it.
4. An artifact review UI — the schema is deliberately reviewable and nothing yet
   takes advantage of that.

---

## Appendix: what running it actually taught

Roughly two dozen bugs were found by running the system against live
applications. The instructive part is that they cluster, and that the largest
cluster was invisible to a green test suite.

**Run-specific data in artifacts** (three instances, §3) — the whole reason
`reusability.py` exists.

**Timing.** A single-page application renders *after* the click, not after
`networkidle`. One search went from 342 named nodes to 57 in that gap, so a
snapshot taken at network-idle showed the previous page and the step read as
"nothing changed" — which sent the agent into clicking the same button
repeatedly.

**Judging a page instead of an element.** Sparseness was measured page-wide.
That fails twice over: a table-based page produces plenty of named nodes because
`cell` and `row` inherit names from their text, and a legacy page's *navigation*
is richly named while its form inputs carry nothing. Both report a healthy tree
for a page Layer 1 cannot drive. The fallback is now decided **per element**,
when the locator actually fails.

**Limits with no gradient.** A strict JSON schema caps at 16 union-typed
parameters; every `str | None` is a union and the decision models nest a locator
inside the decision *and* inside every extraction, so optionals multiply. Adding
one field took the count to 17 and every discovery call began failing at once,
with an error that reads like a model problem. There is now a test that fails
with the offending paths listed.

**Tests that check the wrong artefact.** All of the above passed 320 tests,
because those tests validated Python models rather than the JSON schema those
models emit, and schema correctness rather than reusability. The most productive
hour of the project was spent offline turning "I hope this generalises" into a
rule with tests — which found two flaws in itself within that hour that twelve
live runs had not.
