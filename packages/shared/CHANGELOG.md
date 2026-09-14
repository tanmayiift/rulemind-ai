# Changelog — Shared types

Per-package: this repo versions each shipped artifact separately, so a change to
the Flutter SDK does not move the API's version. The root CHANGELOG.md is an index.

Entries are derived from **path-scoped** `git log` against this directory rather than
from the commit's conventional-commit scope — scopes drift (`fix(ops)`, `fix(scale)`,
`feat(api)` all land here), whereas the touched path cannot lie.

## 1.0.0 — 2026-09-03

First tagged release. `git tag` was empty before this, so there is no prior boundary
to slice against and this section covers all history to date.

### Added

- first-class date type + unified loose equality across all engines (#104)
- on-device decision tables — compiler + Kotlin/Dart evaluators + conformance (#44)
- add MECE rule checking, SDK input validation, and dynamic offline sandbox
- 5 major features — Excel functions, CSV export, import validation, model hosting, WoE scoring

### Fixed

- booleans and null are distinct types in loose equality across all 5 engines (#109)
- reject non-finite numbers, parse TZ offsets, guard regex ReDoS (#108)
- make ordered operators numeric-only in the TS engine to match server + SDKs (#101)
- large-policy cross-engine accuracy — missing-var parity + depth guard + conformance (#45)
