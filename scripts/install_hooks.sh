#!/usr/bin/env bash
# Point git at the tracked hooks in .githooks/.
#
# Hooks live in .git/hooks, which is NOT version controlled -- so a hook written once protects
# exactly one clone on one machine, and silently protects nothing everywhere else. `core.hooksPath`
# is the fix: it moves the hook directory into the repo, where it is reviewed and cloned like any
# other file.
#
#   bash scripts/install_hooks.sh
#
# Still per-clone: git will not set this for you, by design (a repo that could install its own
# hooks could run arbitrary code on clone). So this has to be run once per checkout, which is why
# it prints what it did rather than staying quiet.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }

git rev-parse --git-dir >/dev/null 2>&1 || { printf '  not a git repository\n'; exit 2; }

git config core.hooksPath .githooks
ok "core.hooksPath -> .githooks"

for h in .githooks/*; do
  [ -f "$h" ] || continue
  chmod +x "$h"
  ok "$(basename "$h") is executable"
done

printf '\n'
note "Installed: a pre-push hook that refuses a direct push to main."
note "It is CLIENT-SIDE. GitHub's branch protection needs Pro or a public repo (both routes"
note "return 403 on this repo), so this stops accidents and not intent. Bypass with --no-verify."
printf '\n'
