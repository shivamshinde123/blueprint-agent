# Blueprint Agent

A computer-use automation system for UIs with no API.

An LLM drives a real browser once to work out how a task is done, and freezes
that successful run into a typed, versioned **artifact**. Production then
replays the artifact mechanically — no LLM in the decision loop, so the same
inputs produce the same actions every time.

> The model discovers. The artifact becomes a reusable capability. Deterministic
> replay is how the agent invokes it in production.

**Design docs:** [`PLAN.md`](PLAN.md) is the build spec. [`REPORT.md`](REPORT.md)
is the design write-up.

---

## Status

**Working end to end**, against [saucedemo.com](https://www.saucedemo.com).
346 tests passing, lint clean.

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
| Real discovery + strict replay, with evidence | ✅ |
| `REPORT.md` design write-up | 🔲 outstanding |

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
`BLUEPRINT_LLM_BASE_URL` at another compatible endpoint — a different gateway,
or a local server — works without further changes.

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

Every schema rule and cross-reference check runs here. Try breaking the file —
a stale `step_N` key in the error map, an `output_schema` field nothing
extracts, a `{{param}}` that isn't declared — and it will tell you exactly what
is wrong before a browser is ever opened.

### 2. Merge a tenant override (offline)

```bash
uv run python main.py merge \
  tests/fixtures/golden_artifact.json \
  config/tenants/bank_a.json
```

One small file per tenant — a different host, a renamed button, a coordinate
offset — merged over the shared base flow with no re-recording.

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
never appear on the command line — where they would land in shell history and
the process list, out of redaction's reach.

### 4. Replay it deterministically

```bash
uv run python main.py replay \
  --artifact artifacts/lookup_product_price_v1.0.0.json \
  --params '{"product_name": "Sauce Labs Bike Light"}' \
  --credentials SAUCEDEMO \
  --mode strict
```

Note the product differs from the one it was recorded with, and it still
returns the right answer — that is the point of the artifact, and there is a
committed evidence log for each:

```
recorded with  "Sauce Labs Backpack"   -> $29.99   6/6 steps, 0 model calls
replayed with  "Sauce Labs Bike Light" -> $9.99    6/6 steps, 0 model calls
```

`--mode strict` makes **zero** model calls — in strict mode the engine is not
even given a client — and that is the mode used for the evidence runs. Add
`--escalate` to start the operator console, so a stuck run pauses for a human
instead of failing.

### 5. Try the local legacy surface

```bash
uv run python -m mock.server        # serves http://127.0.0.1:8081/mock/bank
```

A table-based page with no ARIA labels at all, so the screenshot fallback has
to do real work. It exists so the Layer 2 demonstration does not depend on a
public demo site being up.

---

## Tests

```bash
uv run pytest
```

The suite is hermetic — no network, no credentials, no live demo sites — so it
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
- **Risk gating** — `high` and `critical` steps always pause for human
  confirmation before executing.
- **Redaction** — parameters marked `sensitive` never reach the artifact, the
  logs, or the evidence files. Playwright traces, videos, and HAR capture are
  disabled outright.

Known limits are stated in [`REPORT.md`](REPORT.md) § Safety rather than
implied away.
