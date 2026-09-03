# Changelog — Rust core engine

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
- standalone Rust decide service — dedicated /decide hot path (~575k dec/s/core) (#63)
- WASM build for the Rust eval-core + attribution licensing (#8)

### Fixed

- make on-device decisioning work — bundle contract and engine parity (#117)
- booleans and null are distinct types in loose equality across all 5 engines (#109)
- reject non-finite numbers, parse TZ offsets, guard regex ReDoS (#108)
