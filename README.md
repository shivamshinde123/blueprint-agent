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

**The framework is complete.** 305 tests passing, lint clean.

| Component | State |
|---|---|
| Artifact schema + cross-reference validation | ✅ |
| Safety: allowlist, risk gating, redaction | ✅ |
| Pinned browser sessions | ✅ |
| Discovery agent (observe → decide → act) | ✅ |
| Replay engine + three-category error handling | ✅ |
| Human escalation + operator console | ✅ |
| Multi-tenant overrides | ✅ |
| Local zero-ARIA legacy surface | ✅ |
| CLI: `discover` / `replay` / `validate` / `merge` | ✅ |
| Evidence runs against the real target apps | ⏳ pending target flows |

See [`PLAN.md` §13](PLAN.md) for the full work breakdown, and
[`PLAN.md` §11](PLAN.md) for the 20 corrections made to the original design.

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
| `ORANGEHRM_USERNAME` / `ORANGEHRM_PASSWORD` | for the modern-web demo | Public demo: `Admin` / `admin123` |
| `GURU99_USERNAME` / `GURU99_PASSWORD` | for the legacy demo | Request at <https://www.demo.guru99.com/> → *Demo Login*; these expire |
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
  --goal "Search for employee Peter Anderson and get their job title and sub unit" \
  --url  "https://opensource-demo.orangehrmlive.com" \
  --capability lookup_employee_profile \
  --credentials ORANGEHRM \
  --output artifacts/lookup_employee_profile_v1.0.0.json
```

`--credentials ORANGEHRM` reads the username and password from `.env`, so they
never appear on the command line — where they would land in shell history and
the process list, out of redaction's reach.

### 4. Replay it deterministically

```bash
uv run python main.py replay \
  --artifact artifacts/lookup_employee_profile_v1.0.0.json \
  --params '{"employee_name": "Peter Anderson"}' \
  --credentials ORANGEHRM \
  --mode strict
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
