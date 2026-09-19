#!/usr/bin/env bash
# Delete every RuleMind resource hosted on Render, so it stops billing.
#
# WHY THIS EXISTS. The hosted deployment was retired on 2026-09-14 and `render.yaml` was deleted
# from the repo — but deleting the CONFIG does not delete the SERVICES. They kept running, and a
# later check found the API still answering 200 and the dashboard still serving. A repo that says
# "we removed the hosted deployment" while four resources quietly bill is the worst of both.
#
# WHY IT NEEDS A TOKEN FROM YOU. There is no Render CLI, no ~/.render credential and no
# RENDER_API_KEY in this environment, and Render has no unauthenticated delete — by design. So
# this cannot run unattended. Create a key at:
#
#   Render dashboard -> Account Settings -> API Keys -> Create API Key
#
# then:
#
#   cp .env.render.example .env.render        # paste the key into .env.render (gitignored)
#   bash scripts/render_teardown.sh           # lists what it found, asks, then deletes
#   bash scripts/render_teardown.sh --yes     # skip the prompt
#
# or, without a file:
#
#   export RENDER_API_KEY=rnd_xxxxxxxxxxxx    # in your shell only
#
# The token is read from the environment, never written to a file, never passed as an argument
# (argv is visible in `ps`), and never echoed — including in the curl trace on failure.
#
# THIS IS IRREVERSIBLE. A deleted Render Postgres is gone with its data. Take a dump first if you
# want one: the dashboard's Postgres page has a "Download backup" action.
set -uo pipefail

API="https://api.render.com/v1"
ASSUME_YES=0
[ "${1:-}" = "--yes" ] && ASSUME_YES=1

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }

# `.env.render` is gitignored (`.env.*`), same shape as `.env.ai`. Reading it here means the key
# is pasted into ONE file whose only job is to hold it, rather than typed into a shell whose
# history keeps it.
if [ -z "${RENDER_API_KEY:-}" ] && [ -f "$(dirname "$0")/../.env.render" ]; then
  set -a
  # shellcheck disable=SC1091
  . "$(dirname "$0")/../.env.render"
  set +a
fi

if [ -z "${RENDER_API_KEY:-}" ]; then
  bad "RENDER_API_KEY is not set and .env.render was not found."
  note "Create a key at: Render dashboard -> Account Settings -> API Keys -> Create API Key"
  note "Then:  cp .env.render.example .env.render   and paste it in (the file is gitignored),"
  note "  or:  export RENDER_API_KEY=rnd_...        (this shell only; never commit it)"
  exit 2
fi

# Whitespace from a paste produces a 401 that reads like a revoked key. Trim it here instead.
RENDER_API_KEY="$(printf '%s' "$RENDER_API_KEY" | tr -d '[:space:]')"
export RENDER_API_KEY
command -v curl >/dev/null 2>&1 || { bad "curl is required"; exit 2; }
command -v python3 >/dev/null 2>&1 || { bad "python3 is required (for JSON parsing)"; exit 2; }

# -s so the token never appears in a progress line; the header is passed via @- on stdin rather
# than on the command line, so it is not visible to `ps`.
render_get() {
  printf 'Authorization: Bearer %s\n' "$RENDER_API_KEY" \
    | curl -sS --max-time 60 -H @- -H 'Accept: application/json' "$API/$1"
}
render_delete() {
  printf 'Authorization: Bearer %s\n' "$RENDER_API_KEY" \
    | curl -sS --max-time 60 -o /dev/null -w '%{http_code}' -X DELETE -H @- "$API/$1"
}

# Render paginates and wraps each row as {"<kind>": {...}, "cursor": "..."} on some endpoints and
# returns a bare list on others. Normalise both to "id<TAB>name<TAB>type".
parse_rows() {
  python3 -c '
import json,sys
kind = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    sys.exit(0)
rows = data if isinstance(data, list) else data.get(kind + "s") or []
for row in rows:
    item = row.get(kind, row) if isinstance(row, dict) else {}
    if not isinstance(item, dict) or not item.get("id"):
        continue
    print("\t".join([str(item.get("id")), str(item.get("name") or "?"),
                     str(item.get("type") or kind)]))
' "$1"
}

printf '\n\033[1mRender resources on this account\033[0m\n'

FOUND=""
probe() {            # probe <endpoint> <json-kind> <label>
  local body rows
  body="$(render_get "$2")"
  case "$body" in
    *Unauthorized*|*unauthorized*) bad "the API key was rejected (401)"; exit 1 ;;
  esac
  rows="$(printf '%s' "$body" | parse_rows "$3")"
  if [ -z "$rows" ]; then
    note "no $1 found"
    return
  fi
  while IFS="$(printf '\t')" read -r id name type; do
    [ -n "$id" ] || continue
    printf '  %-28s %-22s %s\n' "$name" "$type" "$id"
    FOUND="$FOUND$2|$id|$name"$'\n'
  done <<EOF
$rows
EOF
}

probe "services"   "services?limit=100"  "service"
probe "databases"  "postgres?limit=100"  "postgres"
probe "key-value stores" "key-value?limit=100" "keyValue"

FOUND="$(printf '%s' "$FOUND" | sed '/^$/d')"
if [ -z "$FOUND" ]; then
  printf '\n\033[32mNothing to delete.\033[0m The account has no services, databases or key-value stores.\n\n'
  exit 0
fi

COUNT="$(printf '%s\n' "$FOUND" | wc -l | tr -d ' ')"
printf '\n\033[31mThis deletes all %s resource(s) above, permanently.\033[0m\n' "$COUNT"
printf 'A deleted Render Postgres is gone WITH ITS DATA. Take a backup first if you want one.\n\n'

if [ "$ASSUME_YES" -ne 1 ]; then
  printf 'Type DELETE to proceed: '
  read -r answer
  if [ "$answer" != "DELETE" ]; then
    printf '\nAborted. Nothing was deleted.\n\n'
    exit 0
  fi
fi

printf '\n'
FAILED=0
while IFS='|' read -r endpoint id name; do
  [ -n "$id" ] || continue
  # "services?limit=100" -> "services"; the DELETE path is the collection plus the id.
  base="${endpoint%%\?*}"
  code="$(render_delete "$base/$id")"
  case "$code" in
    200|202|204) ok "deleted $name ($id)" ;;
    404)         ok "$name ($id) was already gone" ;;
    *)           bad "could not delete $name ($id) — HTTP $code"; FAILED=$((FAILED+1)) ;;
  esac
done <<EOF
$FOUND
EOF

printf '\n'
if [ "$FAILED" -gt 0 ]; then
  bad "$FAILED resource(s) could not be deleted — finish those in the dashboard"
  note "https://dashboard.render.com/"
  exit 1
fi

# The point of the whole exercise, asserted rather than assumed. A cold-start timeout is NOT
# proof a service is gone: the first probe of a sleeping free-tier service takes 16-30s and looks
# exactly like death. That misread is why these were believed dead for a week while they billed.
printf '\033[1mVerifying (a cold start takes ~30s — a timeout is not proof of deletion)\033[0m\n'
code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 60 https://rulemind-api.onrender.com/health 2>/dev/null)"
if [ "$code" = "000" ] || [ "$code" = "404" ]; then
  ok "rulemind-api.onrender.com no longer answers (HTTP ${code})"
else
  bad "rulemind-api.onrender.com still answers HTTP $code — check the dashboard"
fi
printf '\n\033[32mDone.\033[0m Confirm the billing page shows no active resources:\n'
printf '  https://dashboard.render.com/billing\n\n'
