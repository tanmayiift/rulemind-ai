# Changelog — RuleMind dashboard

Per-package: this repo versions each shipped artifact separately, so a change to
the Flutter SDK does not move the API's version. The root CHANGELOG.md is an index.

Entries are derived from **path-scoped** `git log` against this directory rather than
from the commit's conventional-commit scope — scopes drift (`fix(ops)`, `fix(scale)`,
`feat(api)` all land here), whereas the touched path cannot lie.

## Unreleased

### Added

- **A real code editor for Python variables.** The variable editor was a plain `<textarea>` for
  the code the sandbox actually runs. It is now CodeMirror 6 with Python highlighting themed from
  the app's own tokens (so it follows the light/dark toggle rather than becoming a bright
  rectangle), bracket closing, history and indent-with-tab.
- **RuleMind-aware completion**, built from live page state rather than a static list: the
  selected source's `sample_payload` keys after `payload.`, the workspace's other variables after
  `variables.`, and the `@variable(source=…)` decorator with its three parameters. Change the
  source dropdown and the completions change with it.
- **Inline diagnostics.** Syntax and sandbox-policy errors are marked in the gutter before the
  author clicks Test, served by `POST /api/v1/variables/lint` — which reuses the sandbox's own
  `validate_source`, so a marker is the rule rather than a second implementation of it. Debounced
  at 600ms, and a failed request renders nothing rather than an error banner.

### Changed

- **CodeMirror, not Monaco**, and the parity row counts them as equivalent. The dashboard deploys
  as a static site and must be self-contained: `@monaco-editor/react` pulls Monaco from a CDN at
  run time by default, and self-hosting it under `output: "export"` means hand-rolled worker
  plumbing. Monaco is also ~5MB against ~250KB, for one editor on one page.
- The dashboard is deployed as a **Render static site**, not Vercel. It is a pure client SPA — no
  API routes, no dynamic segments, no middleware — so it exports to files, and a static site is
  free and does not spin down.

### Fixed

- **Selecting a saved variable deselected it.** CodeMirror's `docChanged` is also true for the
  programmatic replace that pushes a new `value` in from props, so loading a saved variable read
  as an edit, `beginDraft()` ran, and `variable-promote` never rendered. Fixed with an annotation
  on the sync transaction. Typecheck, both builds and 19 unit tests all passed with this bug
  present — only the end-to-end spec caught it.

## 4.0.0 — 2026-09-03

First tagged release. `git tag` was empty before this, so there is no prior boundary
to slice against and this section covers all history to date.

### Added

- add Gemini, and stop blank env values defeating their defaults (#126)
- predictive-model evaluation + label-based backtesting (Gini/AUC/KS/calibration/uplift) (#112)
- live collaborative policy editor — WebSocket sync, presence, time-travel (#111)
- AI Copilot floating action button (all AI actions, one place) (#103)
- policy backtesting — preview a change's impact on real traffic before promotion (#72)
- per-workspace SLOs + outcome-drift alerting (#71)
- per-workspace data-protection controls in UI — retention window + PII redaction + at-rest encryption posture (#70)
- decision replay against current or historical bundle version (#69)
- policy diff before promotion — see + record what decision logic changed (#68)
- move human session to httpOnly cookie + double-submit CSRF (#65)
- real A/B experiment management page (fix ab-tests route) (#55)
- JSONL import/export for batch backtests (backend + UI) (#54)
- live decision feed via Server-Sent Events (backend + UI) (#53)
- one-command tracing, unreachable-row note, throughput docs (#43)
- per-workspace AI cost/usage tracking + monthly budget cap (#42)
- decision tables on the fast path + concurrent-map loops (#40)
- enterprise SSO — OIDC + SAML 2.0 with JIT provisioning (#39)
- human RBAC — workspace member accounts, password + email-OTP login, session auth (#38)
- Build Studio — one canvas-first builder replacing the split editors (#34)
- clean fresh install + guided activation journey (#33)
- live model dropdown (auto-updates on new launches) + resizable input preview (#32)
- reports builder — dynamic columns, filters, timezone, scheduled email delivery (#28)
- RBAC — role-scoped API keys + Access & Roles UI (#26)
- config-driven AI gating — AI features stay hidden until a key is configured (#25)
- workflow loops + loop-debug (iterate collections/maps, per-iteration trace) (#23)
- decision tables + optimiser (grid authoring, hit policies, conflict/gap/unreachable/invalid detection) (#22)
- AI slice 2 — rejection-driver analytics + NL→policy + explain-decision (#20)
- AI Copilot foundation — BYO OpenAI/Anthropic key + NL→rule + scope guardrail (#19)
- schedules & ML-model hosting UI + working ML runtime (#17)
- editable branches in the workflow builder (#16)
- UI overhaul (Stripe-refined) + real Excel & Postman workflows (#15)
- simulation perf panel + individual case review (#13)
- Postman-style API console for workflow action steps (#12)
- visual drag-and-drop workflow builder (#11)
- config-driven, admin-only white-label branding (#10)
- backend-ready screens — Decision Explorer, Simulation, Lifecycle (#7)
- add MECE rule checking, SDK input validation, and dynamic offline sandbox

### Fixed

- rules builder mode reset, promote with no body, and real auth in e2e (#166, #139)
- make every executable step type reachable and configurable in the builder (#135)
- name every control, and stop clipping the decision table on mobile (#130)
- correct CSV parsing, pin the shell, and label the nav (#118)
- gate data fetches on store rehydration — kills the 401 burst + raw first-run error (#100)
- scale + durability — no 1000-row cap, async email, durable outbox, retry (#31)
- concurrent, no-persist backtest with honest server-side throughput (#29)
- flat input fields must drive source variables (was: all cases approved) (#27)
- resolve Python MECE type detection, Flutter analyzer errors, and web TS error

### Changed

- split rulemind-page.tsx monolith into kit + per-page component files (#68 frontend) (#87)
