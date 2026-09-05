# Changelog — Flutter SDK

Per-package: this repo versions each shipped artifact separately, so a change to
the Flutter SDK does not move the API's version. The root CHANGELOG.md is an index.

Entries are derived from **path-scoped** `git log` against this directory rather than
from the commit's conventional-commit scope — scopes drift (`fix(ops)`, `fix(scale)`,
`feat(api)` all land here), whereas the touched path cannot lie.

## Unreleased

### Added

- **Polling actions survive the handoff to the host.** A queued `action` operation now carries
  `mode: "poll"`, its exit condition, bounds and `onPollTimeout` when the author configured one.
  This engine performs no outbound I/O — actions are queued for the host app — so a host that saw
  only url/method/body would fire once and report whatever came back, which for a job-status
  endpoint is `{"status": "pending"}` treated as final. The server executor fails that case
  CLOSED, so dropping the fields would make the same policy decide differently on device.

## 4.2.0-beta.1 — 2026-09-03

First tagged release. `git tag` was empty before this, so there is no prior boundary
to slice against and this section covers all history to date.

### Added

- injectable http client and outbox, and a resetForTest that resets (#138)
- disclose partial edge decisions, surface sync state, document every SDK (#137)
- first-class date type + unified loose equality across all engines (#104)
- durable SQLite decision outbox + wired background sync (#48)
- on-device decision sync engine — durable outbox interface + syncer (Kotlin + Dart) (#47)
- on-device decision tables — compiler + Kotlin/Dart evaluators + conformance (#44)
- add MECE rule checking, SDK input validation, and dynamic offline sandbox

### Fixed

- make both Flutter apps responsive at every screen size and text scale (#132)
- make on-device decisioning work — bundle contract and engine parity (#117)
- reject non-finite numbers, parse TZ offsets, guard regex ReDoS (#108)
- bound the on-device ExecutionStore — persist only resumable executions (#50)
- large-policy cross-engine accuracy — missing-var parity + depth guard + conformance (#45)
- set minSdk 29 (Android 10+), fix JDK 25 toolchain fallback
- resolve Python MECE type detection, Flutter analyzer errors, and web TS error
- use API 30 emulators for stable CI and fix Flutter mixed-test error
- use single-line scripts for emulator runner and flutter drive for integration tests
