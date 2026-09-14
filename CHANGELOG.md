# Changelog

This repo versions **per package**, not as a whole. A change to the Flutter SDK does not move
the API's version, and there is no single repo-wide version to bump — the root `package.json`
is a private workspace, not an artifact.

This file is the index. Each package's own `CHANGELOG.md` holds its entries.

| Package | Path | Version |
| --- | --- | --- |
| RuleMind API | [`apps/python-executor/`](apps/python-executor/CHANGELOG.md) | 4.0.0 |
| Dashboard | [`apps/web/`](apps/web/CHANGELOG.md) | 4.0.0 |
| Rule engine (TS) | [`packages/rule-engine/`](packages/rule-engine/CHANGELOG.md) | 1.0.0 |
| Shared types | [`packages/shared/`](packages/shared/CHANGELOG.md) | 1.0.0 |
| JavaScript SDK | [`packages/sdk-js/`](packages/sdk-js/CHANGELOG.md) | 1.0.0 |
| SDK core | [`packages/sdk/`](packages/sdk/CHANGELOG.md) | 0.2.0 |
| UI components | [`packages/ui/`](packages/ui/CHANGELOG.md) | 0.2.0 |
| Embeddable widget | [`packages/widget/`](packages/widget/CHANGELOG.md) | 0.2.0 |
| Flutter SDK | [`packages/sdk-flutter/`](packages/sdk-flutter/CHANGELOG.md) | 4.2.0-beta.1 |
| Android SDK | [`packages/sdk-android/`](packages/sdk-android/CHANGELOG.md) | 4.1.0-beta.1 |
| Rust core engine | [`packages/rulemind-core-rs/`](packages/rulemind-core-rs/CHANGELOG.md) | 1.0.0 |
| Rust `/decide` service | [`packages/rulemind-decide-service/`](packages/rulemind-decide-service/CHANGELOG.md) | 0.2.0 |
| Python SDK | [`packages/sdk-python/`](packages/sdk-python/CHANGELOG.md) | 0.2.0 |

`demo-app/` and `packages/flutter-ui/` have no changelog: one is a sample, the other is an
internal layout package with no external consumers.

## How the versions were chosen

`git tag` was empty, so these are **assertions of current maturity, not increments** — there
was no release history to bump from.

- **4.0.0** for the API and dashboard: `app/main.py` already declared `version="4.0.0"`, which
  is what `openapi.json` publishes, while `package.json` said `3.0.0`. This corrects a drift
  rather than shipping anything new.
- **1.0.0** for `rule-engine`, `shared`, `sdk-js` and `rulemind-core-rs`: each is gated by a
  real conformance suite (operator conformance, cross-platform parity, large-policy
  conformance), so their public surface is already pinned by tests.
- **0.2.0** for `sdk`, `ui`, `widget`, `sdk-python` and `rulemind-decide-service`: real, but
  thin or covered only by a compile gate. Staying below 1.0 is the honest signal.
- The two mobile SDKs keep the versions they are already published at, except `sdk-flutter`,
  which moved to `4.2.0-beta.1` because it gained public API (an injectable `httpClient` and
  `outbox` on `initialize`).

## Before this

This file previously read `## 0.1.0` with two bullets describing a Fastify API and a
`packages/schemas` directory, neither of which exists. It had not been touched in 113 merged
PRs.
