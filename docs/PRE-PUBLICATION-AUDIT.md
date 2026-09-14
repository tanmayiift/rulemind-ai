# Pre-publication audit

Run before making this repository public. Last run **2026-09-14** against `main`.

The reason to publish is concrete: **public repositories get unlimited free GitHub-hosted Actions
minutes**, and this repo's CI has been blocked by a billing failure, not by a consumed quota.
Publishing costs nothing and matches what the project already says it is — it ships an Apache 2.0
LICENSE and a README that calls itself open-source.

**That switch is the owner's to flip.** This document only establishes whether it would be
*safe*.

## Result: no secret has ever been committed

| Check | Method | Result |
|---|---|---|
| `.env`, `apps/web/.env.local`, `.env.deployed` ever added | `git log --all --diff-filter=A -- <path>` | **0 add-commits each** — never tracked, in any branch, at any point |
| Provider keys anywhere in history | full `git log --all -p` scanned for `sk-ant-api03-`, `sk-`, `ghp_`, `gho_`, `github_pat_`, `AKIA`, `rnd_`, `AIza`, `xox[baprs]-` | **zero matches** |
| Tracked env files | `git ls-files \| grep -i '\.env'` | only `.env.example` |

History was scanned, not just the working tree. A secret removed in a later commit still ships
with the repository, so "it is not in the tree now" is not the question.

## The committed dev defaults are real values, and that is fine

`.env.example` and `app/security_config.py` contain working default secrets —
`rulemind-dev-master-key`, `rulemind-admin-dev-secret`, `rulemind-admin`,
`rm_live_devlocaltenantkey000000000000`. They are documented as real defaults rather than
placeholders, and they exist so `pnpm dev` works out of the box.

They are safe to publish because **the application refuses to boot on any of them in
production**. `PRODUCTION_SECRET_CHECKS` (`security_config.py:23`) fails closed over all four
whenever `RULEMIND_ENV` is `production`/`prod`/`staging`, and `helm/rulemind` generates fresh
values per install for exactly this reason. Anyone who finds these values in the public repo can
only use them against a deployment that was already running with no secrets configured — which
that check prevents.

## Author emails are in the history and cannot be removed without a rewrite

Four addresses appear as commit authors, including two personal and one employer address. This is
normal for any git history and is what `@users.noreply.github.com` exists to avoid going forward.
Removing the others means rewriting every commit (`git filter-repo --mailmap`), which changes
every SHA. **Flagged as a decision, not a blocker** — most public repositories carry the same.

## Before flipping the switch

1. Re-run the history scan above, since it is only valid as of the commit it was run against.
2. Decide on the author-email question.
3. Check the repo description and topics read the way you want them to (the description said
   "Orchestrator" until 2026-09-14).
4. Expect Dependabot PRs to start running CI. Wire any self-hosted runner to `workflow_dispatch`
   only, never `pull_request` — running bot-authored dependency code on your own machine is a
   different risk from running it on GitHub's.

## Re-running the scan

```bash
git log --all -p --no-color \
  | grep -aoE '(sk-ant-api03-[A-Za-z0-9_-]{20,}|sk-[A-Za-z0-9]{32,}|ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|AKIA[0-9A-Z]{16}|rnd_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{30,}|xox[baprs]-[A-Za-z0-9-]{20,})' \
  | sort -u
```

Empty output is the pass condition. Anything printed must be revoked at the provider **first** —
rotating the key matters more than scrubbing the history, because the history may already be
cloned.
