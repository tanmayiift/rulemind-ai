# Dependabot triage

## Round 2 — 2026-09-17

All **12** open PRs resolved: 2 merged, 7 applied to `main` and closed, 2 rejected, 1 left open
with a reproduction.

**The correction that changed the method.** The previous round of comments on these PRs said CI
could not run because of an *exhausted Actions quota*. That was wrong twice over: it was a
**billing block** (GitHub's annotation: *"recent account payments have failed or your spending
limit needs to be increased"*), so jobs were assigned no runner and executed `steps: 0` —
consumption was already ~0. And it no longer applies at all: the full regression now runs on a
**self-hosted runner**, so these are verifiable in CI rather than only on one laptop.

### Merged (2)

| PR | Change | Verified by |
|---|---|---|
| #164 | opentelemetry-instrumentation-sqlalchemy ≥0.48b0 → ≥0.65b0 | 2049 tests |
| #165 | signxml 4.5.1 → 5.1.0 (a major) | 2059 tests |

### Applied to `main` and closed (7)

| PR | Why it could not simply be merged |
|---|---|
| #147 react 19 | **Fails alone.** Dependabot splits react and react-dom, so this leaves react-dom at 18 and `next build` dies inside `react-dom-server-legacy`. |
| #152 react-dom 19 | The mirror image of #147. The two pass **only together**. |
| #174 vitest 5 | Passes alone — and closes a **critical** advisory the PR does not mention. |
| #153 coroutines 1.11.0 | Needed the Kotlin bump first (see below). |
| #155 AGP 9.4.0 | Needed the Gradle wrapper bump first. |
| #172 security-crypto 1.1.0 | Alpha → stable. Introduces 5 deprecation warnings: 1.1.0 deprecates `EncryptedSharedPreferences`. |
| #173 lucide-react 1.45.0 | Rewrites `pnpm-lock.yaml`, which the Next 15 upgrade had already rewritten. |

### Rejected (2)

**#146 — TypeScript 7.** Two blockers now, where there used to be one:

```
tsconfig.json(3,3): error TS5102: Option 'baseUrl' has been removed.
```

and `next dev` still dies with `TypeError: Cannot read properties of undefined (reading
'endsWith')` — on **Next 15**, so this is not the old "Next 14 doesn't know TS 7". `npx vitest
run` passes 462 tests on the branch, which is the point: a type-checker upgrade that type-checks
cleanly can still break the framework that consumes it.

**#158 — actions/setup-python 5 → 7.** Obsolete. The action appeared only in `load-test.yml`,
which was still on `ubuntu-latest` and therefore could never run. That workflow is now
self-hosted and provisions its own 3.11 venv; there is no step left to bump.

### Still open (1)

**#171 — the python-minor-patch group, 19 updates.** Two independent blockers:

1. `numpy==2.5.3` requires Python ≥3.12; this project pins 3.11, where the newest installable
   numpy is 2.4.6. (Same blocker that closed #170.)
2. With numpy capped, the group **hangs the suite**. Bisected:

   | Installed | `tests.test_collab_ws` |
   |---|---|
   | baseline (fastapi 0.115.6 / starlette 0.41.3) | OK in 0.195s |
   | + uvicorn 0.52.4 only | OK in 0.195s |
   | + fastapi 0.141.1 (starlette **1.6.0**) | **hangs indefinitely** |
   | revert fastapi | OK in 0.195s |

   Confirmed in both directions. FastAPI 0.141 crossing to starlette 1.x is a major dependency
   jump wearing a minor-version label; it deserves its own PR rather than riding in a 19-package
   group. Splitting would let the other 17 land now.

### What the PRs contained between them: three CRITICAL advisories

Not visible from any single PR, which is the argument for triaging them as a set:

```
GHSA-p293-qw3h-jr36  Next.js unauthenticated RCE      next@14.2.35
GHSA-2xp9-vwfh-vxw4  Next.js unauthenticated RCE      next@14.2.35
GHSA-5xrq-8626-4rwp  Vitest UI arbitrary file read    vitest@3.2.4
```

React 19 (#147+#152) is what Next 15 needs, and Next ≥15.5.24 is what closes the first two.
`pnpm audit`: **3 critical → 0**.

### Toolchain work this round required

"Blocked on a toolchain bump" stays true forever unless someone does the bump.
**Gradle 8.7 → 9.7.1 · Kotlin 1.9.24 → 2.2.20 · AGP 8.5.2 → 9.4.0.** Each error named something
other than its cause: AGP 9 registers its own `kotlin` extension (drop `kotlin("android")`),
which removes `android { kotlinOptions }` (use the top-level `kotlin { compilerOptions }`), which
exposes that Kotlin 2.0+ needs the Compose compiler plugin.

Verified from the JUnit XML with the results directory deleted first — Gradle reported
`BUILD SUCCESSFUL` and `UP-TO-DATE` on a run that executed **no tests at all**.

### Two test defects found while verifying, neither caused by any dependency

Both made a PR look red when it was not. Had either red been trusted, a good dependency would
have been closed as broken.

1. **A PII-leak assertion that false-failed ~1 run in 100.** It searched the raw webhook body for
   `b"410"` — the score in its fixture — and a tenant UUID containing `4100` tripped it. A false
   alarm on a security test is the worst kind: it is the one people learn to re-run.
2. **A temp-dir cleanup race.** `TemporaryDirectory.cleanup()` raised *Directory not empty* in
   teardown, after the test passed. 111 call sites used the fatal form while 10 already used
   `ignore_cleanup_errors=True`.

### Follow-ups this round created

- `EncryptedSharedPreferences` is deprecated by security-crypto 1.1.0; `ExecutionStore.kt` needs
  a replacement strategy. Staying on an alpha is not the alternative.
- 4 `high` advisories remain: xlsx/SheetJS prototype pollution and ReDoS (**no fixed version
  published**), plus vite and postcss issues reached through the toolchain.
- numpy stays capped at 2.4.6 until the project moves off Python 3.11.

## Reproducing any of this

```bash
git fetch origin pull/<PR>/head:pr-<PR>
git checkout pr-<PR> && git merge main        # against CURRENT main, not the stale base
pnpm install            # or: pip install -r apps/python-executor/requirements.txt
<run the affected lane>                        # AND CHECK THE EXIT CODE
```

The exit code matters: an earlier triage reported #170 as passing 1937 tests because its harness
used `;` rather than `&&` after `pip install`, so a failed install let the suite run against the
old package still in the venv.

---

## Round 1 — 2026-09-15 (historical)

20 PRs triaged locally when no CI could run: 5 merged, 3 rejected, 2 blocked on toolchain,
5 unverifiable, 5 needing a rebase. The toolchain blockers (#153, #155) and the TypeScript
rejection (#146) carried into Round 2 above. Its "exhausted quota" diagnosis is corrected there.
