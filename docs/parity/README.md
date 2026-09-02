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

Of **22 features** named in the changelog: **3 present, 8 partial, 10 absent, 1 deliberately cut.**

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

**It does not inflate the present column.** The three `PRESENT` rows were checked
function-by-function, not by keyword. The Excel row lists all four named functions with line
numbers inside a ~230-function library.

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

`parity.json` is the guard. A check asserts that every `ABSENT` or `PARTIAL` feature has both
an owning phase and a named E2E flow, so a gap cannot quietly become unowned. Precedent for a
machine-readable expectations file already exists at `qa/e2e-full/journey_expectations.json`.
