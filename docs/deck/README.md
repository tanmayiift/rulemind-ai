# The pitch deck, and where every number on it came from

`RuleMind-AI.pptx` — 11 slides, enterprise buyers (risk, credit and fraud leads at lenders,
fintechs and insurers).

```bash
npm install pptxgenjs react react-dom react-icons sharp   # not vendored; ~10 packages
node build.js                                             # rewrites RuleMind-AI.pptx
python geomqa.py RuleMind-AI.pptx                         # geometry check
```

**`node build.js` fails silently-ish without those packages** — it prints a MODULE_NOT_FOUND
stack and leaves the previous `.pptx` in place, so a geometry check run afterwards happily
passes **on the stale file**. If you change the generator, confirm the `.pptx` mtime moved
before trusting any QA that follows.

## Why the sources are written down

A technical buyer checks. Every figure below was resolved against the repository at the commit
that built the deck — not copied from an older document, which is how the counts in `CLAUDE.md`
came to be wrong by 500+ tests. If you edit a slide, re-derive its number here too.

| Claim on a slide | Verified as | Source |
|---|---|---|
| 4,608 tests, 11 lanes, all green | generated report | `TEST_RESULTS.md` (from `make test-all`) |
| 21 of 22 capability rows PRESENT, 0 absent | `PRESENT=21, CUT=1` | `docs/parity/parity.json` |
| Five language engines | Python, TypeScript, Rust, Kotlin, Dart | `packages/`, `apps/python-executor` |
| 600-condition rule, 135 cases, 1.6 MB | `conditionOrder`=600, `cases`=135, 1,681,435 bytes | `packages/shared/large-policy.spec.json` |
| 19.78 ms median decision | p50 **at concurrency 1** | `docs/PERFORMANCE.md` latency table |
| 117.5 rps | throughput **at concurrency 16** | same table |
| 232 Excel functions | `len(EXCEL_FUNCTIONS)` at runtime | `app/excel_functions.py` |
| 152 in the browser | `EXCEL_FUNCTION_COUNT` at runtime | `packages/rule-engine/src/excel-functions.ts` |
| 32 on device | quoted function names in the Kotlin table | `SafeFormula.kt` |
| 13 step types | distinct `step_type ==` branches | `app/executor.py` |
| 41 tables | unique `__tablename__` | `apps/python-executor/app/` |
| 281 API endpoints | `@app`/`@router` route decorators | `apps/python-executor/app/` |
| 232 MB dashboard image, from 2.54 GB | before/after build | `docs/PERFORMANCE.md` |
| 14-check and 16-check smoke tests | the scripts' own counts | `scripts/smoke_compose.sh`, `scripts/smoke_k8s.sh` |
| Five roles: owner, admin, policy_maker, reviewer, viewer | `ROLE_CAPABILITIES` | `app/rbac.py:39` |
| Backup restores, audit chain intact | drill run 2026-09-14: 41 tables / 165 rows | `apps/python-executor/scripts/backup_verify.py` |

**19.78 ms and 117.5 rps are different rows of the same table.** Never combine them into
"19.78 ms at 117.5 rps" — that reads as a throughput-at-latency claim the benchmark does not
make. Both are laptop measurements against the seeded demo tenant: a floor, not a ceiling, and
slide 7 says so.

## The do-not-claim list

Slide 10 exists to state these out loud, because a buyer's technical evaluation finds them
anyway and it is better that we said it first.

| Never say | Say instead | Why |
|---|---|---|
| "exactly-once delivery" | "never lose a decision event" | The parity row itself says at-least-once with idempotent consumption. |
| "immutable audit log" | "tamper-evident" | `audit_chain`'s own docstring: an operator with DB write access can recompute the chain. Detection, not prevention. |
| "232 Excel functions everywhere" | name the surface | 232 server-side, 152 TypeScript, 32 on-device. |
| "CI is green" | "a committed, regenerable test report" | Actions will not start jobs for this repo — a billing block. `TEST_RESULTS.md` is the honest and stronger answer: anyone can reproduce it in one command. |
| "open source" | "Apache 2.0 licensed" | True today. The repository is not public yet — see `docs/PRE-PUBLICATION-AUDIT.md`. |
| the 48.7% `COMPUTE_ONLY_USED_VARS` win | nothing, or "roadmap" | Measured, but the flag is off by default. |
| the README's "thousands/s" table | the measured numbers | That table is sizing guidance, not a measurement. |
| invented role names ("author", "approver", "service") | the real five: **owner, admin, policy_maker, reviewer, viewer** | Slide 8 had the COUNT right and three of the five NAMES invented. A buyer who opens `rbac.py` finds different words than the deck used — and "the deck made up our role names" is a worse first impression than any missing feature. Names come from `ROLE_CAPABILITIES`, not from memory. |
| "0 external dependencies at runtime" | "0 phone-home, metering or licence calls" | **It needs PostgreSQL and Redis.** An architect reads "external dependencies" as "services this requires" and finds two. What is actually true — and is the thing enterprise buyers care about — is that nothing calls a vendor: no telemetry, no per-decision metering, no licence server. Checked by grepping every hardcoded outbound host in the backend; the only hits are opt-in sample connectors (`postcodes.io`, `er-api.com`), a CSP header, and a SCIM schema URI. |

## QA

`validate.py` from the pptx skill passes, `markitdown` shows no placeholder text, and
`geomqa.py` reports no geometry issues across all 11 slides.

`geomqa.py` exists because there is no LibreOffice on this machine, so the usual
render-and-look pass was not available. It reads the packed XML and checks the four defect
classes that actually ship broken decks: shapes outside the canvas, shapes crowding the slide
edge, text estimated to overflow its own box, and text boxes overlapping each other. It found
six real defects on the first build — a closing line colliding with the footer on two slides, a
label sitting inside a body text box on slide 11, a heading running under a right-aligned command
on slide 9, and two feature headings wrapping past a one-line box on slide 4.

It is a substitute for a renderer, not an equal one: text fitting is estimated from average
glyph widths, so treat it as a floor. **Look at the slides in PowerPoint before presenting
them.**
