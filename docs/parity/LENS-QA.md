# QA lens

What proves each feature works, where that proof lives, and the standing bar every new
entity must clear.

## The standing checklist

Every new entity added under this roadmap must satisfy all five before it is called done:

1. **Tenant isolation** — a negative test that tenant B cannot read tenant A's instance.
2. **RBAC** — each of the 5 roles gets the access it should and no more (`app/rbac.py`).
3. **Audit event emitted** — the action appears in the audit log with the authenticated actor.
4. **OpenAPI updated** — the committed spec regenerates with no diff.
5. **At least one negative test** — not only the happy path.

The absence of this checklist is why **12 endpoints currently have zero test references**,
including `/sdk/v1/signing-key`, which was added to fix a Critical finding and still has no
automated test of its own.

## Where proof lives

| layer | location | what it should prove |
|---|---|---|
| Python unit/integration | `apps/python-executor/tests/` (742) | endpoint contracts, engine semantics, security |
| TypeScript | `test/` (68) | the TS engine, cross-platform parity |
| Playwright | `e2e/` (52) | browser journeys — authoring → promote → decide |
| Full-stack QA | `qa/e2e-full/` | auth/tenancy, sandbox escape (21 cases), frontend sweeps |
| Mobile widget | `demo-app/test/`, `packages/sdk-flutter/example/test/` | layout across 6 surfaces × 3 text scales |
| Mobile device | `demo-app/lib/main_visual_probe.dart` | **real-font** rendering — see the limitation below |
| Cross-engine conformance | 5 suites named in `CLAUDE.md` | identical semantics in Python/TS/Rust/Kotlin/Dart |

## Two limitations to state rather than paper over

**Widget tests cannot catch real-font text clipping.** Flutter renders tests with a
placeholder font, so text width in a test does not match a device. Proven, not theorised: the
same screen at 393×851 @2.0x reported nothing out of bounds in a test while visibly
truncating a chip on an iOS simulator. **A device pass is mandatory** for any layout change,
not a nice-to-have.

**Most of the strongest suites do not run in CI.** Playwright, all of `qa/e2e-full/`
(including the 21-case sandbox-escape matrix — the highest-value security test in the repo),
k6 load, and `cargo test` are invoked by hand only. CI also runs SQLite exclusively; the
Postgres pass has always been manual. Wiring these in is Track B1 and gates the credibility
of every other claim here.

## Per-phase proof

**Phase 3 — quick wins.** *Author a connector → test the connection → use it in a workflow
whose action polls an external API → the policy times out cleanly under load → the decision
retains for N months.* New `e2e/connectors.spec.ts`, per-endpoint Python tests, and a new
`qa/e2e-full/suite_timeouts.py` beside the existing `suite_core.py` / `suite_sandbox.py`.

**Phase 4 — outbound + predictor.** *Register a webhook for `decision.rejected` with a filter
→ run a policy → a signed payload arrives at a stub receiver → predictor values land in
ClickHouse → the report shows drift.* Needs a receiver stub under `qa/e2e-full/`, an HMAC
signature test, a ClickHouse round-trip, and a redelivery test that kills the receiver
mid-flight to exercise the DLQ.

**Phase 5 — release safety.** *Promote to prod at 5% canary → watch the metric → auto-rollback
on SLO breach → open the failing execution and step through it.* **This is where the test debt
gets paid**, not as a separate chore: the flow necessarily exercises `/deploy/status`,
`/deploy/promote`, `/policies/{id}/whatif|shadow|rollback|releases` and
`/executions/{id}[/resume]` — all currently at zero coverage.

**Phase 6 — authoring.** *Import a 3-dimension pricing matrix from XLSX → reference it from a
rule → the MECE checker reports gaps and overlaps → edit the backing variable in Monaco with
live error squiggles → decide against it.* The importer is where the effort is; `app/mece.py`
gives the coverage validation for free.

**Phase 7 — journeys and the graph canvas.** A journey is the first entity to need an SDK
surface across **all five engines**, so its proof is a conformance case in each of the five
suites, not just a backend test. That 5× multiplier is the single biggest reason this phase
is sequenced last.
