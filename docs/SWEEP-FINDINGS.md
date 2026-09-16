# Multi-agent sweep — findings ledger (2026-09-16)

Four parallel audits, one per lens: **C1 UI/UX**, **C2 backend API**, **C3 orchestration**,
**C4 evaluations**. Each was told to separate CONFIRMED (reproduced) from SUSPECTED (code reading
only), and to mutation-check every test it wrote.

**C3 and C4 finished. C1 and C2 were killed mid-run by an account session rate limit** — not by
any failure in the work. Their partial output landed and is validated; neither lens completed its
scope. What they did not reach is listed under [Not swept](#not-swept).

Everything below was verified against the running stack or a failing test before being recorded.
Where a claim in the original brief turned out to be wrong, the correction is kept rather than
quietly dropped.

---

## Fixed and mutation-checked

### Orchestration (C3)

| # | Defect | Where | Why it mattered |
|---|---|---|---|
| 1 | The reachability walk never scanned a transform's `mapping` | `variable_reachability.py` | A transform-only policy had an **empty** needed set. The variable was *skipped*, not failed, so `variable_errors` stayed empty and the fail-closed guard never fired — the decision was made on `None` with no signal. And it was not opt-in: the executor filters when `simulate or <flag>`, so **every backtest** silently did not predict production. `model`'s `inputMapping` had the identical gap. |
| 2 | `$.computed` keyed on truthiness | `executor.py` | A transform writing an empty `computed` did not clear it, but exposed a flat merge of every *other* transform's fields. A guarded transform therefore leaked an unrelated step's `limit` to a downstream rule. |
| 3 | An unterminated `[` returned the whole list | `jsonpath.py` | `"$.payload.scores[0"` bound the entire array. Every other malformed path already yielded `None`. |
| 4 | A `model` step that could not run failed **open** | `executor.py` | A stale model id raised, was swallowed into the trace, and the run continued: the output variable was never set, every comparison against a missing value is `False`, and the policy returned **APPROVE at HTTP 200** on a score no model produced. Now escalates to review. |

`transform` had **zero** execution tests before this — its only appearances in the suites were
assertions that it *disqualifies a policy from the fast path*. 12 of 13 step types had real
coverage; `transform` was the total gap and `model` was the one that looked covered and was not
(its sole test asserted `"model" in str(body)`, true merely because the step dict is echoed into
the trace).

### Evaluations (C4)

| # | Defect | Where | Why it mattered |
|---|---|---|---|
| 5 | The champion/challenger analysis never reached the page | `ab-tests/page.tsx` | Two independent mismatches hiding one block: the API emits `championChallenger`, the page read `champion_challenger`; and `recommend_action` returns a **string**, which the page typed as an object and read as `.action`. Lift and the promote/hold verdict were invisible, on a page whose subtitle promises exactly those. |
| 6 | Promote hardcoded `force: true` | `ab-tests/page.tsx` | The backend refuses to promote a variant recommended "hold"/"rollback" **and** refuses to promote the champion at all — but both checks sit inside `if not request.force:`, so one flag removed both. Every click rewrote live rules with the gate disabled. |
| 7 | A null cell returned **HTTP 500** | `routers/evaluations.py` | `float(None)` raises `TypeError`; the handler caught only `(ValueError, KeyError)`. A blank cell — what a spreadsheet writes for an unscored row — reported as a server fault. `"abc"` and a missing column were already 422. |
| 8 | WoE rendered clamped infinities as real numbers | `model_evaluation.py` | `max(dist, 1e-12)` turned an undefined band into a finite ±26, printed straight into the WoE column. Real weight-of-evidence lives in roughly [-3, 3], so an analyst could not tell it from a genuine value. |
| 9 | Confusion matrix, accuracy and calibration computed, persisted, never shown | `evaluations/page.tsx` | The nav entry for the page promises "accuracy, drift, and confusion matrix". Pure UI gap over data already on the wire. |
| 10 | PSI advertised in two places, zero callers | page subtitle + router docstring | It needs a reference distribution a single labelled set does not have. The claim was removed rather than faked; drift lives on `/predictors`, which has a baseline. |

### Frontend (C1, partial) and backend (C2, partial)

| # | Defect | Where |
|---|---|---|
| 11 | `/collab` could never open its WebSocket | `collab/page.tsx` — no credential was sent, and a browser cannot put `x-api-key` in a handshake header |
| 12 | The sign-in screen printed raw JSON at the user | `login/page.tsx` — `apiJson` rejects with the response *text*, so a 401 surfaced as `{"detail":"…"}` |
| 13 | Three ordinary test-console mistakes returned 500 | `routers/runtime.py` — a cleared numeric field, a duration typed as text, a pasted header line |
| 14 | Disarming two-person control left no audit trace | `routers/governance.py` — the promotions it stops gating are recorded; the decision to stop gating them was not |
| 15 | `/model-sets` and `/predictors` had complete backends and no UI at all | 7 + 8 endpoints reachable only by calling the API by hand |

---

## Closed since this ledger was written

| # | Finding | Fix |
|---|---|---|
| A | Promotion provenance silently discarded | Three columns + migration 0030, and `_as_datetime` on the way in because the router writes an ISO **string** into a DateTime column — which SQLite accepts and hands back, so the round-trip "works" until Postgres. |
| B | The promotion gate had no sample-size floor | An inversion now counts only when it exceeds 2 SE of the difference between the two bands; below 1000 rows the check reports **not applicable** rather than a verdict. Guarded against over-correction by a test that a genuinely bad (humped) ranker still fails. |
| C | Experiment analytics cached 300s with no invalidation | 30s. The other three `ttl_seconds=300` in that file are deliberately untouched — a near-identical literal appears four times, which is how an earlier attempt at this edit changed the wrong function. |
| D | The headline p-value was positional | Selected by **role**, and the response now names the pair (`control`, `treatment`, `comparedVariants`, `totalVariants`). |
| E | `/simulation` showed log-wide rejection drivers inside per-run results | The panel now says so, with a "whole decision log" badge and the run's own case count for contrast. |
| F | Contradictory field counts on `/simulation` | The label reads the policy's real schema instead of a hardcoded "20 vars". |

## Open — found, verified, not yet fixed

Each was reproduced.

| # | Finding | Severity | Note |
|---|---|---|---|
| G | **A transform mapping that is not a `$.` path is stored verbatim.** Literal passthrough is intentional, but the one-character typo `"$variables.x"` writes that string into the output and nothing validates mappings at author time. | Low-medium | Pinned as characterisation, not asserted as a bug. |

## Not swept

C1 and C2 did not finish. Still unexamined:

- The remainder of the 39-route UI pass. Covered so far: `/studio`, `/decision-tables`, `/login`
  + `/admin/login`, `/collab`, `/evaluations`, `/models`, `/ab-tests`, `/simulation`,
  `/model-sets`, `/predictors`. Not covered: `/reports`, `/schedules`, `/review-queue`,
  `/journeys`, `/debugger`, `/decision-explorer`, `/deploy`, `/lifecycle`, and the rest.
- Untested endpoints beyond `insights`, `runtime` and `governance` — notably `policies`'
  lifecycle pair.

## Two lessons about the tests themselves

Three tests written during this sweep **could not fail**, and each was caught only by mutation
checking:

1. `playwright.config.ts` sets a global `extraHTTPHeaders: { "x-api-key": … }`, and Playwright
   applies those to the **WebSocket upgrade** request — an ability no real browser has. A browser
   test of a handshake therefore authenticates in a way a user never could, and passes whether or
   not the client sends a credential. Any spec asserting on a handshake must
   `test.use({ extraHTTPHeaders: {} })`.
2. A route test asserted on a heading matching the page name — but the app shell paints that
   header from `PAGE_COPY` *before* the route renders, so the name is on screen even on a 404.
   It passed against a missing route.
3. A models test was written as `if (models.length === 0) { …; return; }`, and the e2e database
   seeds zero models — so the assertion branch never executed and blanking every model name on
   the page passed.

All three are the same shape: **the fixture hands the test the value it asserts.** Mutation
checking is what found them, which is the argument for doing it on every new guard rather than
on the ones that look risky.
