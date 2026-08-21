# Evidence

Logs and screenshots from real runs against live applications. Every file here
was produced by the system, not written by hand.

| File | What it shows |
|---|---|
| `discovery_run_saucedemo.json` | A real model-driven discovery run. 6 steps recorded, 7 model calls. This is the run that produced `artifacts/lookup_product_price_v1.0.0.json` |
| `replay_run_sauce_labs_backpack.json` | That artifact replayed with the product it was recorded against. `llm_calls_made: 0` |
| `replay_run_sauce_labs_bike_light.json` | **The same artifact replayed with a product it had never seen**, returning that product's own price. `llm_calls_made: 0` |
| `replay_error_run.json` | A structured failure, with `expected` and `observed` recorded, plus a screenshot and DOM snapshot taken at the moment it broke |
| `interventions.json` | A real human handoff on demo.guru99.com. The run paused at step 9, wrote this record, started the operator console, and blocked waiting for a person |
| `screenshots/` | The final screen for each run above, plus the page state at the moment of the handoff |

## The two replays are the point

`replay_run_sauce_labs_backpack.json` and `replay_run_sauce_labs_bike_light.json`
were produced by the **same artifact**, given different inputs:

```
Backpack    ->  $29.99
Bike Light  ->  $9.99
```

Both completed 6 of 6 steps with zero model calls. See section 1 of `REPORT.md`
for why the second one is the test that matters.

## Note on credentials

Every log shows `***REDACTED***` where a credential was used. That is the
redaction path running on real files rather than only in tests.
