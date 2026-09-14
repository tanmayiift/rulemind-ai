# Changelog — Standalone Rust /decide service

Per-package: this repo versions each shipped artifact separately, so a change to
the Flutter SDK does not move the API's version. The root CHANGELOG.md is an index.

Entries are derived from **path-scoped** `git log` against this directory rather than
from the commit's conventional-commit scope — scopes drift (`fix(ops)`, `fix(scale)`,
`feat(api)` all land here), whereas the touched path cannot lie.

## Unreleased

### Added

- **tests — the crate had none.** The service is now covered at its HTTP boundary by tests that
  drive the real `axum` `Router` in-process: the shared 90-case `operators.spec.json` contract
  replayed over `POST /decide`, the raw-payload variable path, the probes, and one test per
  engine fix below.
- `POST /decide` accepts `"trace": true`, returning the per-rule `conditions[]` /
  `groupResults[]` trace alongside the outcome.

### Fixed

- **a too-deep rule tree no longer kills the server.** Unbounded recursion in the engine
  overflowed the native stack and *aborted the process*, taking every in-flight request with
  it. It is now `422` + a JSON error, and the service keeps serving.

## 0.2.0 — 2026-09-03

First tagged release. `git tag` was empty before this, so there is no prior boundary
to slice against and this section covers all history to date.

### Added

- standalone Rust decide service — dedicated /decide hot path (~575k dec/s/core) (#63)
