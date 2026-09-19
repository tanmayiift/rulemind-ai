#!/usr/bin/env bash
# Delete workflow runs that never executed a step, and runs of workflows that no longer exist.
#
# WHY. The Actions tab shows 127 failed runs and almost none of them is a test failure:
#
#   119  assigned no runner at all, `steps: 0` -- GitHub's annotation on each reads "The job was
#        not started because recent account payments have failed or your spending limit needs to
#        be increased." A billing block, not a test result.
#     78  of those belong to `ci`, `sdk-verify` and `test-report`, workflows RETIRED on
#        2026-09-16. GitHub keeps a deleted workflow's runs in the tab forever, so they can never
#        turn green and never age out.
#      8  actually executed something. Those are REAL history and this script does not touch them.
#
# WHAT IT DELETES, and nothing else. A run qualifies only if the sum of steps across all its jobs
# is ZERO -- asked of the API per run, not inferred from the conclusion or the workflow name. A
# run that executed even one step is kept regardless of how it ended, because a genuine failure is
# the most useful record in the tab.
#
#   scripts/github_runs_cleanup.sh --dry-run   # list what would go; change nothing
#   scripts/github_runs_cleanup.sh             # do it
#
# IRREVERSIBLE. A deleted run and its logs are gone for good.
set -uo pipefail

REPO="${RULEMIND_GH_REPO:-tanmayiift/rulemind-ai}"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }

command -v gh >/dev/null 2>&1 || { bad "gh is required"; exit 2; }
gh auth status >/dev/null 2>&1 || { bad "gh is not authenticated"; exit 2; }

printf '\n\033[1mScanning runs on %s\033[0m\n' "$REPO"

# --paginate over the runs API rather than `gh run list --limit`, which caps at 1000 and orders
# by creation. Every run is examined; the filter is the step count, not the page.
RUNS=$(gh api "/repos/$REPO/actions/runs?per_page=100" --paginate \
  --jq '.workflow_runs[] | "\(.id)\t\(.name)\t\(.conclusion // "none")\t\(.created_at[0:10])"' 2>/dev/null)

TOTAL=0; ZERO=0; KEPT=0; FAILED=0
ZERO_IDS=""

while IFS=$'\t' read -r id name concl date; do
  [ -n "$id" ] || continue
  TOTAL=$((TOTAL + 1))
  # THE ONLY TEST THAT MATTERS: did this run execute anything? A job list with no steps means no
  # runner ever picked it up. Asked per run, because the conclusion cannot distinguish "the tests
  # failed" from "the job never started" -- both say `failure`.
  steps=$(gh api "/repos/$REPO/actions/runs/$id/jobs" --jq '[.jobs[].steps | length] | add // 0' 2>/dev/null)
  if [ "${steps:-1}" -eq 0 ]; then
    ZERO=$((ZERO + 1))
    ZERO_IDS="$ZERO_IDS $id"
    printf '  %-12s %-22s %-9s %s  steps=0\n' "$id" "${name:0:22}" "$concl" "$date"
  else
    KEPT=$((KEPT + 1))
  fi
done <<EOF
$RUNS
EOF

printf '\n  %d run(s) scanned — %d executed nothing, %d kept\n' "$TOTAL" "$ZERO" "$KEPT"

if [ "$ZERO" -eq 0 ]; then
  ok "nothing to delete"
  exit 0
fi

if [ "$DRY" -eq 1 ]; then
  printf '\n\033[33mdry run — nothing was changed.\033[0m Re-run without --dry-run to delete.\n\n'
  exit 0
fi

printf '\n\033[31mDeleting %d run(s). This is irreversible.\033[0m\n\n' "$ZERO"
DONE=0
for id in $ZERO_IDS; do
  if gh api -X DELETE "/repos/$REPO/actions/runs/$id" >/dev/null 2>&1; then
    DONE=$((DONE + 1))
  else
    bad "could not delete run $id"
    FAILED=$((FAILED + 1))
  fi
done

ok "deleted $DONE run(s)"
[ "$FAILED" -gt 0 ] && { bad "$FAILED could not be removed"; exit 1; }

left=$(gh api "/repos/$REPO/actions/runs?per_page=1" --jq '.total_count' 2>/dev/null)
ok "runs remaining: ${left:-?}"
printf '\n\033[32mClean.\033[0m\n\n'
