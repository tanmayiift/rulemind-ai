# Changelog — Android SDK

Per-package: this repo versions each shipped artifact separately, so a change to
the Flutter SDK does not move the API's version. The root CHANGELOG.md is an index.

Entries are derived from **path-scoped** `git log` against this directory rather than
from the commit's conventional-commit scope — scopes drift (`fix(ops)`, `fix(scale)`,
`feat(api)` all land here), whereas the touched path cannot lie.

## 4.1.0-beta.1 — 2026-09-03

First tagged release. `git tag` was empty before this, so there is no prior boundary
to slice against and this section covers all history to date.

### Added

- first-class date type + unified loose equality across all engines (#104)
- durable on-device decision outbox (SQLite) + batch sync (#49)
- on-device decision sync engine — durable outbox interface + syncer (Kotlin + Dart) (#47)
- on-device decision tables — compiler + Kotlin/Dart evaluators + conformance (#44)
- add MECE rule checking, SDK input validation, and dynamic offline sandbox

### Fixed

- make on-device decisioning work — bundle contract and engine parity (#117)
- reject non-finite numbers, parse TZ offsets, guard regex ReDoS (#108)
- bound the on-device ExecutionStore — persist only resumable executions (#50)
- large-policy cross-engine accuracy — missing-var parity + depth guard + conformance (#45)
- set minSdk 29 (Android 10+), fix JDK 25 toolchain fallback
- fix live admin test timeout with keyboard dismiss and better health check
