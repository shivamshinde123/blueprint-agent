# REPORT — Blueprint Agent

Design write-up for the computer-use automation take-home.
The seven sections below are the ones the brief asks for.

> **Status: draft shell.** Sections are filled in as each phase lands, so the
> claims here can cite real evidence files rather than intentions. Source
> material for each section is noted in italics — see `PLAN.md` for the detail.

---

## 1. Architecture

*Draws on PLAN.md §3 and §11 C11.*

- [ ] Two-phase split: LLM-in-the-loop discovery vs. LLM-free replay, and why
      the boundary sits exactly there
- [ ] Why two target surfaces (modern + legacy), and what each one proves
- [ ] The local zero-ARIA mock: a reproducibility decision, not a shortcut
- [ ] Key trade-offs and what they cost

TODO

---

## 2. Artifact Schema

*Draws on PLAN.md §4 and §11 C1, C2, C4, C8, C16.*

- [ ] The nine top-level blocks and why the flow is separated from the locators
- [ ] Two-layer locators: accessibility tree first, screenshot fallback second
- [ ] Why a fallback is recorded for *every* step, and why it costs nothing
- [ ] Coordinates are viewport-relative with a recorded scroll offset — a
      full-page screenshot's y-values are not clickable
- [ ] Versioning, provenance, and what makes the artifact reviewable by a human
      as well as executable by a machine
- [ ] Load-time cross-reference validation, and the silent failures it prevents

TODO

---

## 3. Determinism and Error Handling

*Draws on PLAN.md §6 and §11 C3, C5, C10.*

- [ ] Determinism is structural, not sampler-based — current models reject
      `temperature`, and the artifact is what makes replay repeatable
- [ ] Layer 2 is *locator resolution*, never decision-making; the step sequence
      comes from the artifact in both modes
- [ ] `strict` vs `assisted`, and why the evidence run uses `strict`
- [ ] The three error categories: expected business outcome / recoverable /
      hard failure — and why outcomes are checked before failures
- [ ] Why every step carries a post-condition, and how trivially-true
      checkpoints are avoided

TODO

---

## 4. Heterogeneity and Multi-Tenant

*Draws on PLAN.md §10.*

- [ ] The seam: WHAT (recorded flow) vs HOW (surface-specific locators)
- [ ] Extending to legacy web and Windows desktop — swap the resolver, keep
      everything else
- [ ] Base artifact + per-tenant override files, merged at load time
- [ ] Detecting per-tenant drift: replay failure signals and canary runs

TODO

---

## 5. Escalation and Handoff

*Draws on PLAN.md §8.*

- [ ] Three triggers: stuck in discovery, replay exhausted, risky action
- [ ] The same live session is paused and handed over — never closed or
      refreshed
- [ ] The control-transfer model, and why exactly one party holds control
- [ ] Two resume semantics: the human performs the step vs. the human only
      authorises it
- [ ] What is recorded about everything the human did

TODO

---

## 6. Safety

*Draws on PLAN.md §7 and §11 C12.*

- [ ] Allowlist: domains, routes, action types — enforced in code, both phases
- [ ] Four risk levels and their enforcement
- [ ] Redaction of sensitive values before anything is written
- [ ] Fail-fast pre-run validation
- [ ] **Limits.** Failure screenshots and accessibility snapshots can still
      carry on-screen PII. Stated plainly rather than implied away.

TODO

---

## 7. Cuts

*Draws on PLAN.md §11 C11, C18 and §8.7.*

- [ ] Real-time co-browsing is out of scope; the operator uses the same browser
      window the automation opened
- [ ] No mid-replay resume after a process crash
- [ ] Desktop support is designed, not built
- [ ] What would be built next, in priority order

TODO
