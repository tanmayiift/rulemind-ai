# Parity analysis

An assessment of a larger decisioning platform's changelog against `rulemind-ai`, through
three lenses, ending in a sequenced build plan rather than a gap list.

## What this is

| file | what it holds |
|---|---|
| [`PARITY-MATRIX.md`](PARITY-MATRIX.md) | the source of truth — one row per feature, every claim citing a file and line |
| [`parity.json`](parity.json) | machine-readable twin; a CI check asserts every gap has an owning phase and a named E2E flow |
| [`LENS-PRODUCT.md`](LENS-PRODUCT.md) | who asks for each feature, what it unblocks, and where to say no |
| [`LENS-TECH.md`](LENS-TECH.md) | what each gap costs architecturally, and which are cheap because the substrate exists |
| [`LENS-QA.md`](LENS-QA.md) | the E2E flow and the proof for each feature, plus the standing checklist for new entities |
| [`ROADMAP.md`](ROADMAP.md) | phases, honest sizes, and what blocks what |

## The headline

Of **22 features** named in the changelog: **2 present, 9 partial, 10 absent, 1 deliberately cut.**
(Re-audited 2026-09-04. The present column went *down*: P21 was promoted when the evaluation
budget shipped, and P10 and P17 were demoted because neither had a test covering the claim the
row makes. See the re-audit section of `PARITY-MATRIX.md`.)

That distribution — a few generic infrastructure overlaps plus a large block of named product
nouns that appear nowhere in the codebase — is what you would expect when comparing a
**decisioning engine** (connectors → variables → rules → scorecards → policies) against a
broader **platform**. It is a scope difference, not a quality one.

## What this analysis refuses to do

**It does not treat the changelog as a specification.** One feature (`P14`, SQL editor) is
marked *cut*: this product has no SQL datasource, so an editor for it would be an editor for
nothing. Another (`P19`, superflows) is flagged as possibly a naming artifact of the other
platform rather than a capability gap, and is not scheduled until someone defines what it
would mean here.

**It does not inflate the present column.** The bar is: implementing code *and* a test that
covers the claim the row makes — not a test of something adjacent. The 2026-09-04 re-audit
applied it and demoted two rows that had been scored on code alone. `P17` (Excel functions) was
the cautionary one: all four named functions exist inside a ~230-function library, which is what
the original evidence said, but two of them (`CUMIPMT`, `DAYS360`) have no test anywhere and are
missing from the TypeScript twin — so the row was counting the library, not the functions.

**It does not hide a cheaper answer.** `P09` asks for Kafka and exactly-once delivery. The
roadmap ships a DLQ and an `Idempotency-Key` convention on the existing Redis bus in Phase 4,
which delivers most of the buyer-visible value at a fraction of the cost, and says plainly
that real exactly-once is a multi-month commitment to be taken against a paying requirement.

## The one number that decides cost

`CLAUDE.md` names five conformance suites that must move together — Python, TypeScript, Rust,
Kotlin and Dart. Any feature touching operator semantics costs roughly **5×** one that
touches only the backend. Every row carries a `5-engine` flag for exactly this reason, and it
is set on the way in rather than discovered during estimation.

## Keeping it honest

`parity.json` is the guard. `check_parity.py` asserts that every `ABSENT` or `PARTIAL` feature
has both an owning phase and a named E2E flow, and that every row cites evidence — so a gap
cannot quietly become unowned. Precedent for a machine-readable expectations file already exists
at `qa/e2e-full/journey_expectations.json`.

**As of 2026-09-04 that check actually runs.** Until then it was a script in the repo that no
workflow invoked, which is worth stating plainly: for the first week of its life the guard against
rot was itself the thing rotting.

It first ran as the `contracts` job in `sdk-verify.yml`. **That workflow was retired on
2026-09-16**; the check is now one of the contract gates in `scripts/run_all_tests.sh`, which is
both what a developer runs locally and what `self-hosted-tests.yml` runs in CI — so it can no
longer be green in one place and unrun in the other. The 2026-09-04 re-audit found
drifted line numbers on most rows and three wrong states, all of which accumulated in that window.

What the check still cannot do is verify a citation. It reads structure, not `app/executor.py`.
Line numbers drift on every refactor, so re-read the rows against the code periodically rather
than trusting a green job.
