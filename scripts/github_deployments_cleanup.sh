#!/usr/bin/env bash
# Delete stale GitHub Deployments and Environments left behind by retired hosting.
#
# WHY. The repo's Deployments tab is red, and none of it reflects anything that still exists:
#
#   5x  Production                  vercel[bot]   ALL FAILED, all 2026-09-04, all now 404
#   6x  main - rulemind-api|db|dashboard          Render, 5 success + 1 failure, 12 days stale
#
# Vercel was building the PYTHON EXECUTOR as a Vercel project, which was never going to work; the
# integration looks like it was never meant to exist. The GitHub App behind it was uninstalled on
# 2026-09-16, so nothing recreates these.
#
# A deployment cannot be deleted while it is `active`, so each is marked `inactive` first. That
# is a GitHub API rule, not a nicety -- skipping it returns 422.
#
#   scripts/github_deployments_cleanup.sh --dry-run   # list what would go; change nothing
#   scripts/github_deployments_cleanup.sh             # do it
#
# IRREVERSIBLE. Deployment history and environment records are gone for good. They are dead
# records for hosting that is being retired, which is the only reason this is safe to run.
set -uo pipefail

REPO="${RULEMIND_GH_REPO:-tanmayiift/rulemind-ai}"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }

command -v gh >/dev/null 2>&1 || { bad "gh is required"; exit 2; }
gh auth status >/dev/null 2>&1 || { bad "gh is not authenticated"; exit 2; }

printf '\n\033[1mDeployments on %s\033[0m\n' "$REPO"
IDS=$(gh api "/repos/$REPO/deployments" --paginate --jq '.[].id' 2>/dev/null)
if [ -z "$IDS" ]; then
  ok "no deployments — nothing to do"
else
  COUNT=0
  for id in $IDS; do
    env=$(gh api "/repos/$REPO/deployments/$id" --jq '.environment' 2>/dev/null)
    state=$(gh api "/repos/$REPO/deployments/$id/statuses" --jq '.[0].state // "none"' 2>/dev/null)
    creator=$(gh api "/repos/$REPO/deployments/$id" --jq '.creator.login' 2>/dev/null)
    printf '  %-12s %-28s %-9s %s\n' "$id" "$env" "$state" "$creator"
    COUNT=$((COUNT + 1))
  done
  printf '\n  %d deployment(s)\n' "$COUNT"
fi

printf '\n\033[1mEnvironments\033[0m\n'
ENVS=$(gh api "/repos/$REPO/environments" --jq '.environments[]?.name' 2>/dev/null)
if [ -z "$ENVS" ]; then
  ok "no environments"
else
  while IFS= read -r e; do [ -n "$e" ] && printf '  %s\n' "$e"; done <<EOF
$ENVS
EOF
fi

if [ "$DRY" -eq 1 ]; then
  printf '\n\033[33mdry run — nothing was changed.\033[0m Re-run without --dry-run to delete.\n\n'
  exit 0
fi

printf '\n\033[31mDeleting. This is irreversible.\033[0m\n\n'

FAILED=0
for id in $IDS; do
  # A deployment must be INACTIVE before it can be deleted; otherwise DELETE returns 422.
  gh api -X POST "/repos/$REPO/deployments/$id/statuses" \
    -f state=inactive >/dev/null 2>&1 || true
  if gh api -X DELETE "/repos/$REPO/deployments/$id" >/dev/null 2>&1; then
    ok "deleted deployment $id"
  else
    bad "could not delete deployment $id"
    FAILED=$((FAILED + 1))
  fi
done

while IFS= read -r e; do
  [ -n "$e" ] || continue
  # The name goes in a URL path and these contain spaces ("main - rulemind-api"), so it must be
  # percent-encoded or the request 404s on a name that plainly exists.
  enc=$(printf '%s' "$e" | sed 's/ /%20/g')
  if gh api -X DELETE "/repos/$REPO/environments/$enc" >/dev/null 2>&1; then
    ok "deleted environment '$e'"
  else
    bad "could not delete environment '$e'"
    FAILED=$((FAILED + 1))
  fi
done <<EOF
$ENVS
EOF

printf '\n'
left=$(gh api "/repos/$REPO/deployments" --jq 'length' 2>/dev/null)
envs_left=$(gh api "/repos/$REPO/environments" --jq '.total_count' 2>/dev/null)
ok "deployments remaining: ${left:-?}   environments remaining: ${envs_left:-?}"
[ "$FAILED" -gt 0 ] && { bad "$FAILED item(s) could not be removed"; exit 1; }
printf '\n\033[32mClean.\033[0m\n\n'
