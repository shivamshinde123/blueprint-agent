# Blueprint Agent

A computer-use automation system for UIs with no API.

A model drives a real browser **once** to work out how a task is done, then
writes those steps into a typed, versioned **artifact**. Every use after that
just follows the saved steps, with no model involved. Same input, same actions,
every time.

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the agent invokes it in production.

**Start here:** [`REPORT.md`](REPORT.md) is the design write-up, and explains
how the system works and why it is built this way. [`PLAN.md`](PLAN.md) is the
build spec. [`evidence/`](evidence/) holds the logs and screenshots from real
runs.

---

## Status

**Working end to end** against [saucedemo.com](https://www.saucedemo.com),
with the screenshot fallback and the human handoff both exercised on the legacy
bank at [demo.guru99.com](https://demo.guru99.com/V4/). 346 tests passing, lint
clean.

The claim the project exists to make, from `evidence/`:

```
recorded with  "Sauce Labs Backpack"    ->  $29.99   6/6 steps, 0 model calls
replayed with  "Sauce Labs Bike Light"  ->  $9.99    6/6 steps, 0 model calls
```

The second line used a product the saved steps had never seen. See section 1 of
[`REPORT.md`](REPORT.md) for why that is the test that matters.

| Component | State |
|---|---|
| Artifact schema + cross-reference validation | ✅ |
| Reusability enforced (no run data in locators or checkpoints) | ✅ |
| Safety: allowlist, risk gating, redaction | ✅ |
| Pinned browser sessions | ✅ |
| Discovery agent (observe → decide → act) | ✅ |
| Replay engine + three-category error handling | ✅ |
| Human escalation + operator console | ✅ |
| Multi-tenant overrides | ✅ |
| Local zero-ARIA legacy surface | ✅ |
| CLI: `discover` / `replay` / `validate` / `merge` | ✅ |
| Real discovery + strict replay, with committed evidence | ✅ |
| Screenshot fallback, fired on a real legacy site | ✅ |
| Human handoff, fired on a real legacy site | ✅ |
| `REPORT.md` design write-up | ✅ |
| Business outcomes recorded automatically | 🔲 see Cuts in `REPORT.md` |

One thing is deliberately unfinished. A discovery run only ever sees the happy
path, so it never encounters "no results" and cannot record what that looks
like. A bad input therefore returns a structured failure rather than a clean
"not found" answer. The replay engine supports those outcomes fully; the
recorder does not yet fill them in. Section 7 of [`REPORT.md`](REPORT.md)
explains the fix.

See [`PLAN.md` §13](PLAN.md) for the full work breakdown, and
[`PLAN.md` §11](PLAN.md) for the corrections made to the original design.

---

## Setup

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repo-url>
cd Blueprint_Agent

uv sync                          # create .venv and install dependencies
uv run playwright install chromium

cp .env.example .env             # then fill in the values
```

### Environment variables

| Variable | Required | Notes |
|---|---|---|
| `OPENROUTER_API_KEY` | for `discover` | <https://openrouter.ai/keys>. Not needed for `replay --mode strict` or `validate` |
| `SAUCEDEMO_USERNAME` / `SAUCEDEMO_PASSWORD` | for the demo flows | Public demo; the credentials are printed on the login page itself |
| `BLUEPRINT_MODEL` | no | Defaults to `anthropic/claude-sonnet-5` |
| `BLUEPRINT_PROVIDERS` | no | Pinned upstream routing, default `anthropic`. Empty string lets the gateway choose |

`.env` is gitignored. Only `.env.example` is committed.

### Model access

Models are reached through **OpenRouter**, an OpenAI-compatible gateway, so
trying a different model for the discovery loop is a one-line change:

```bash
BLUEPRINT_MODEL=google/gemini-3-pro uv run python main.py discover ...
```

Nothing outside [`src/llm/`](src/llm/) imports a provider SDK. Pointing
`BLUEPRINT_LLM_BASE_URL` at another compatible endpoint works without further
changes, whether that is a different gateway or a local server.

Routing to the upstream provider is **pinned** by default, with gateway
fallback disabled. A gateway silently substituting a different provider
mid-run is precisely the failure mode a determinism-focused system should
refuse, so it is opt-in rather than default.

---

## Demo path

### 1. Validate an artifact (works offline, no browser, no API key)

```bash
uv run python main.py validate tests/fixtures/golden_artifact.json
```

Every schema rule and cross-reference check runs here. Try breaking the file
and see what happens. Point a `step_N` key in the error map at a step that does
not exist, add an `output_schema` field that nothing extracts, or use a
`{{param}}` that is not declared. It will tell you exactly what is wrong, before
a browser is ever opened.

### 2. Merge a tenant override (offline)

```bash
uv run python main.py merge \
  tests/fixtures/golden_artifact.json \
  config/tenants/bank_a.json
```

One small file per tenant, listing only what differs: a different host, a
renamed button, a coordinate offset. It is merged over the shared base flow with
no re-recording.

### 3. Discover a capability *(needs `OPENROUTER_API_KEY`)*

```bash
uv run python main.py discover \
  --goal "Log in to the store, open the product with the given name, and get its price and description." \
  --url  "https://www.saucedemo.com" \
  --capability lookup_product_price \
  --credentials SAUCEDEMO \
  --params '{"product_name": "Sauce Labs Backpack"}' \
  --output artifacts/lookup_product_price_v1.0.0.json
```

`--credentials SAUCEDEMO` reads the username and password from `.env`, so they
never appear on the command line. On the command line they would end up in shell
history and the process list, where redaction cannot reach them.

### 4. Replay it deterministically

```bash
uv run python main.py replay \
  --artifact artifacts/lookup_product_price_v1.0.0.json \
  --params '{"product_name": "Sauce Labs Bike Light"}' \
  --credentials SAUCEDEMO \
  --mode strict
```

Note that the product is **not** the one it was recorded with, and it still
returns the right answer. That is the whole point of the artifact.

`--mode strict` makes **zero** model calls. In strict mode the engine is not
even handed a model client, so this is a property of the wiring rather than a
promise. It is the mode used for every evidence run.

Add `--escalate` to start the operator console, so a stuck run pauses for a
human instead of failing.

### 5. Try the local legacy surface

```bash
uv run python -m mock.server        # serves http://127.0.0.1:8081/mock/bank
```

A table-based page with no ARIA labels at all, so the screenshot fallback has
to do real work. It exists so the Layer 2 demonstration does not depend on a
public demo site being up.

---

## Evidence

Everything in [`evidence/`](evidence/) was written by the system during a real
run against a live site. Nothing in it was typed by hand.

| File | What it shows |
|---|---|
| `discovery_run_saucedemo.json` | The recording run: 6 steps, 7 model calls, the artifact it produced |
| `replay_run_sauce_labs_backpack.json` | Replay with the recorded input, `llm_calls_made: 0` |
| `replay_run_sauce_labs_bike_light.json` | Replay with an input the recording never saw, `llm_calls_made: 0` |
| `replay_error_run.json` | A replay against a deliberately broken artifact, classified as a hard failure |
| `interventions.json` | A real human handoff, raised on the Guru99 bank at step 9 |
| `screenshots/` | The final screen of each run above, plus the page HTML for the failure |

The two replay logs are the ones to compare. Same artifact, different product
name, correct price both times, no model calls either time.

Screenshots are committed here because both target sites are public demo sites
with no real data on them. On a real banking surface they would stay out of the
repository. A failure screenshot captures whatever happened to be on screen at
the time, and redaction cannot reach inside a PNG.

---

## Tests

```bash
uv run pytest
```

The suite is hermetic. No network, no credentials, no live demo sites, so it
passes on a fresh clone.

---

## Repository layout

```
main.py                CLI entry point
config/allowlist.json  permitted domains, routes, and actions
artifacts/             saved capability artifacts
evidence/              run logs, screenshots, intervention records
mock/                  local zero-ARIA legacy surface
src/
  artifact/            schema + validation
  agent/               discovery loop
  replay/              deterministic executor
  safety/              allowlist, risk gating, redaction
  escalation/          human handoff + operator console
  session/             pinned browser factory
  evidence/            structured run logging
tests/
```

---

## Safety

- **Allowlist** of domains, routes, and action types, enforced in code in both
  phases. Upload, download, and script execution are refused even if the model
  asks for them.
- **Risk gating.** `high` and `critical` steps always pause for human
  confirmation before executing.
- **Redaction.** Parameters marked `sensitive` never reach the artifact, the
  logs, or the evidence files. Playwright traces, videos, and HAR capture are
  disabled outright.

Known limits are stated in [`REPORT.md`](REPORT.md) § Safety rather than
implied away.
