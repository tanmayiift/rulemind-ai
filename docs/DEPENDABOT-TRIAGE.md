# Dependabot triage — 2026-09-15

20 PRs had sat open because **no CI can run on them**: every workflow in this repo has zero
successes (a billing block, not a quota). So each was verified locally, by running the lane it
actually affects.

**Result: 5 merged, 3 rejected, 2 blocked on toolchain, 5 unverifiable, 5 need a rebase.**

## Two methodology points that changed the answers

**1. Test against current `main`, not the PR's base.** Checking out a Dependabot branch alone
ran **135** Flutter tests where `main` has **163** — those branches predate recent work, so it
was verifying the dependencies against last week's code. Merging `origin/main` in first is what
makes the result mean anything.

**2. Individual passes do not license a combined merge.** Every PR below that was rejected
passed its own lane first. The failures only appeared when several landed together, or when the
lane that could see the problem was finally run.

## Merged (5)

| PR | Change | Verified by |
|---|---|---|
| #149 | flutter_secure_storage 9.2.4 → 11.0.0 | flutter 163 |
| #150 | flutter_lints 5.0.0 → 6.0.0 | flutter 163 |
| #151 | pointycastle 3.9.1 → 4.0.0 | flutter 163 |
| #154 | espresso-core 3.6.1 → 3.7.0 | gradle |
| #163 | opentelemetry-instrumentation-fastapi ≥0.48b0 → ≥0.65b0 | python 1937 |

Combined regression before merging: **4,642 tests, 11 lanes, 0 failed.**

## Rejected (3) — each passed on its own first

### #170 — numpy 2.5.3 cannot install on this project's Python

`numpy==2.5.3` requires **Python ≥3.12**; the project pins 3.11 (Docker image, venv, docs). On
3.11 the newest installable numpy is **2.4.6**.

**This was initially reported as passing 1937 tests, and that was wrong.** The verification
harness ran `pip install ... >/dev/null 2>&1;` — a semicolon, not `&&` — so the install failure
was swallowed and the suite ran against the **old numpy still in the venv**. A verification step
that cannot fail is not a verification step; it was caught by `check_audit_ignores`, which runs
`pip-audit` and therefore actually resolves the file.

The group also bumps scipy → 1.18.1 and scikit-learn → 1.9.1, which need re-checking against
3.11 before any of this group lands.

### #146 — TypeScript 7 breaks `next dev`

Passes vitest (462) **and** `tsc --noEmit` clean. But Next.js 14 does not recognise TypeScript 7:
it decides the package is missing, launches its own `pnpm install`, and dies with
`TypeError: Cannot read properties of undefined (reading 'endsWith')`.

Proven side by side: `main` (TS 5.9.3) → `✓ Ready in 1183ms`; the branch never starts. A
type-checker upgrade that type-checks cleanly can still break the framework that consumes it.

### #145 — Playwright 1.63's Chromium blocks the localhost WebSocket

The npm minor/patch group bumps `@playwright/test` 1.53 → 1.63, which ships Chromium 153. The
e2e test *the collab WebSocket refuses an unauthenticated handshake* then fails on its **positive
control** — the *credentialed* socket will not open, code 1006.

Three hypotheses, two discarded:

- ❌ `about:blank` opaque origin — navigating to a real origin did not help.
- ❌ `opentelemetry-instrumentation-fastapi` breaking ASGI WebSockets — passes alone.
- ✅ **Chromium 153 blocks it client-side.** The server never logged the request at all.

The product is unaffected. **The test needs updating for the new browser** — drive that socket
from Node rather than a browser page, or pass the relevant Chromium flag. Merging #145 without
that turns the e2e lane red.

Worth recording: that positive control did exactly what its comment claims it is for — *"if the
port were wrong, the route renamed, or the backend down, the unauthenticated case below would
'pass' for the wrong reason."* Without it, this upgrade would have made a **security** test pass
because nothing could connect at all.

## Blocked on a toolchain bump (2)

| PR | Blocked by |
|---|---|
| #153 | kotlinx-coroutines 1.11.0 pulls kotlin-stdlib 2.2.20 (metadata v2.2.0) against the project's Kotlin 1.9.0 compiler. Needs a Kotlin bump first. |
| #155 | AGP 9.4.0 requires Gradle ≥ 9.6.0; the wrapper is on 8.7. Needs a wrapper upgrade first. |

Each deserves its own PR. A toolchain upgrade smuggled in under a dependency bump is how a
build breaks for reasons nobody can find.

## Unverifiable locally (5)

#156–#160 bump GitHub Actions (`setup-node`, `setup-python`, `setup-java`, `codeql-action`,
`pnpm/action-setup`). They can only be exercised by Actions, which cannot run. Leave open.

## Need a rebase (5)

#147, #148, #152, #164, #165 all verified individually, but conflict in the generated
`pnpm-lock.yaml` / `requirements.txt` because several PRs edit the same manifest. Dependabot
rebases these itself once one of them merges — reconstructing a pnpm lockfile by hand duplicates
that with a real risk of an inconsistent result.

## Separate finding, larger than any PR here

The dev server prints:

> `WARN deprecated next@14.2.34: This version has a security vulnerability. Please upgrade to a
> patched version. See https://nextjs.org/blog/security-update-2025-12-11`

**A known Next.js vulnerability in the pinned version, and Dependabot has not opened a PR for
it.** Tracked in `docs/PRODUCTION-READINESS.md`.

## Reproducing this

```bash
git fetch origin pull/<PR>/head:pr-<PR>
git checkout pr-<PR> && git merge origin/main     # against CURRENT main, not the stale base
pnpm install            # or: pip install -r apps/python-executor/requirements.txt -- CHECK THE EXIT CODE
<run the affected lane>
```
