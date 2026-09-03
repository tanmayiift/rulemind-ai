# Changelog — Rust core engine

Per-package: this repo versions each shipped artifact separately, so a change to
the Flutter SDK does not move the API's version. The root CHANGELOG.md is an index.

Entries are derived from **path-scoped** `git log` against this directory rather than
from the commit's conventional-commit scope — scopes drift (`fix(ops)`, `fix(scale)`,
`feat(api)` all land here), whereas the touched path cannot lie.

## Unreleased

### Fixed

- **rule-tree depth guard.** `evaluate_node` recursed with no bound; a deep author-supplied
  tree overflowed the native stack, which in Rust **aborts the process** rather than raising.
  Now bounded at `MAX_RULE_TREE_DEPTH = 200` — the same constant Python, Kotlin and Dart use —
  returning `Err(EvalError::DepthExceeded)`, surfaced as `ValueError` through PyO3, a thrown
  `Error` through WASM, and `422` from `rulemind-decide-service`.
- **regex input cap.** The `regex` operator had no input bound, so a multi-megabyte field burned
  CPU proportional to its size and could decide differently from the other engines. Capped at
  `REGEX_INPUT_MAX = 4096` **characters** (Python counts code points, so `.chars().count()`,
  not bytes), failing closed exactly as `app/logic.py::_safe_regex_search` does.
- **`fieldType` is matched case-insensitively.** Rust compared it with a bare
  `== Some("boolean")`, so a field declared `"Boolean"` or `"Date"` silently skipped the typed
  comparison and fell through to the generic path. Python, Kotlin and Dart all lowercase it
  first. Found by a 205k-case differential fuzz against `app/logic.py::compare`, where this was
  7,694 of the 8,034 disagreements. (The TypeScript engine, `packages/rule-engine`, has the same
  bare `===` comparison and is still affected.)
- **group evaluation no longer short-circuits.** `.any()` / `.all()` stopped early; Python,
  Kotlin and Dart evaluate every child. Short-circuiting under-counted passed conditions and
  could swallow a depth violation hiding behind an already-decided sibling.

### Added

- **per-condition trace.** `evaluate_tree_traced` / `CompiledBundle::decide_traced` return
  `conditions[]` + `groupResults[]` in the shape `app/logic.py::evaluate_rule_tree` emits.
  Rust now asserts the outcome, the passed-condition count AND the full per-condition bitstring
  for all 135 + 5 cases of `packages/shared/large-policy.spec.json` — 140 conformance cases the
  engine structurally could not participate in before.
- `CompiledBundle::from_value`, for callers that already hold a `Value` (and the only way to
  build a tree deeper than `serde_json`'s 128-deep parse limit).
- PyO3: `evaluate_tree_traced`, `Bundle.decide_traced`, and the `MAX_RULE_TREE_DEPTH` /
  `REGEX_INPUT_MAX` constants. WASM: `evaluateTreeTraced`.

## 1.0.0 — 2026-09-03

First tagged release. `git tag` was empty before this, so there is no prior boundary
to slice against and this section covers all history to date.

### Added

- first-class date type + unified loose equality across all engines (#104)
- standalone Rust decide service — dedicated /decide hot path (~575k dec/s/core) (#63)
- WASM build for the Rust eval-core + attribution licensing (#8)

### Fixed

- make on-device decisioning work — bundle contract and engine parity (#117)
- booleans and null are distinct types in loose equality across all 5 engines (#109)
- reject non-finite numbers, parse TZ offsets, guard regex ReDoS (#108)
