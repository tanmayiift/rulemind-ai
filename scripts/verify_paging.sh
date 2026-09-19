#!/usr/bin/env bash
# Prove the alerting chain end to end, one link at a time, and say which link is broken.
#
# WHY. "We have alerts" and "we get alerted" are different claims, and only the second one
# matters at 3am. Every component here validates on its own while being disconnected: the rules
# file passes promtool, Alertmanager starts cleanly with no destination configured, Prometheus
# happily runs with no Alertmanager discovered. A stack in that state looks configured from every
# angle and pages nobody. That is not hypothetical -- it is the state this repo was in.
#
# Each check below is a LINK, in order, because a broken link makes every later one meaningless:
#
#   1  rules file is valid                     promtool
#   2  Prometheus has LOADED them               /api/v1/rules      (a valid file it never read
#                                                                   is the commonest failure)
#   3  Prometheus has DISCOVERED Alertmanager   /api/v1/alertmanagers
#   4  the heartbeat is actually firing         /api/v1/alerts     (Watchdog; if this is absent
#                                                                   the dead man's switch is dead)
#   5  Alertmanager RECEIVED it                 /api/v2/alerts
#   6  a destination is CONFIGURED              url_file exists and is non-empty
#   7  delivery is not failing                  alertmanager_notifications_failed_total
#
#   bash scripts/verify_paging.sh
#   bash scripts/verify_paging.sh --send        # also inject a real test alert and watch it land
#
# Needs the observability profile up:  make observability
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

PROM="${RULEMIND_PROM_URL:-http://127.0.0.1:9090}"
ALERTMANAGER="${RULEMIND_ALERTMANAGER_URL:-http://127.0.0.1:9093}"
SECRETS="observability/secrets"
SEND=0
[ "${1:-}" = "--send" ] && SEND=1

PASS=0; FAIL=0; WARN=0
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33mwarn\033[0m  %s\n' "$1"; WARN=$((WARN+1)); }
note() { printf '        %s\n' "$1"; }

jq_() { "${RULEMIND_PY:-python3}" -c "import json,sys; d=json.load(sys.stdin); print($1)" 2>/dev/null; }

printf '\n\033[1mAlerting chain\033[0m\n'

# ── 1 ────────────────────────────────────────────────────────────────────────────────────────
if command -v promtool >/dev/null 2>&1; then
  promtool check rules observability/alerts.yml >/dev/null 2>&1 \
    && ok "rules file is valid" || { bad "promtool rejected observability/alerts.yml"; }
elif command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$PWD/observability/alerts.yml:/tmp/a.yml:ro" \
    --entrypoint promtool prom/prometheus:v2.54.1 check rules /tmp/a.yml >/dev/null 2>&1 \
    && ok "rules file is valid (promtool via docker)" || bad "promtool rejected the rules file"
else
  warn "no promtool and no docker; rules not validated"
fi

# ── 2 ────────────────────────────────────────────────────────────────────────────────────────
# A VALID FILE PROMETHEUS NEVER READ is the single commonest way this chain is broken, because
# every local check passes. Ask Prometheus what it actually has.
RULES=$(curl -sf --max-time 10 "$PROM/api/v1/rules" 2>/dev/null)
if [ -z "$RULES" ]; then
  bad "Prometheus is not answering at $PROM"
  note "make observability   (or set RULEMIND_PROM_URL)"
  note "every check below depends on this; stopping here would be guessing"
else
  N=$(printf '%s' "$RULES" | jq_ 'sum(len(g["rules"]) for g in d["data"]["groups"])')
  if [ "${N:-0}" -gt 0 ]; then
    ok "Prometheus has loaded $N rule(s)"
  else
    bad "Prometheus has loaded ZERO rules"
    note "the file is valid but not mounted or not referenced from rule_files"
  fi
fi

# ── 3 ────────────────────────────────────────────────────────────────────────────────────────
AMS=$(curl -sf --max-time 10 "$PROM/api/v1/alertmanagers" 2>/dev/null)
NACT=$(printf '%s' "$AMS" | jq_ 'len(d["data"]["activeAlertmanagers"])')
if [ "${NACT:-0}" -gt 0 ]; then
  ok "Prometheus has discovered $NACT active Alertmanager(s)"
else
  bad "Prometheus has discovered NO Alertmanager"
  note "rules will evaluate and fire into nothing — check alerting.alertmanagers in prometheus.yml"
fi

# ── 4 ────────────────────────────────────────────────────────────────────────────────────────
# The heartbeat. If Watchdog is not firing, the dead man's switch is itself dead, and its whole
# value was that its silence would be noticed.
ALERTS=$(curl -sf --max-time 10 "$PROM/api/v1/alerts" 2>/dev/null)
WD=$(printf '%s' "$ALERTS" | jq_ 'len([a for a in d["data"]["alerts"] if a["labels"].get("alertname")=="Watchdog"])')
if [ "${WD:-0}" -gt 0 ]; then
  ok "Watchdog is firing — the heartbeat exists"
else
  bad "Watchdog is NOT firing"
  note "it is expr: vector(1), so it fires unconditionally when the rule is loaded."
  note "Not firing means Prometheus does not have this rules file."
fi

# ── 5 ────────────────────────────────────────────────────────────────────────────────────────
AMALERTS=$(curl -sf --max-time 10 "$ALERTMANAGER/api/v2/alerts" 2>/dev/null)
if [ -z "$AMALERTS" ]; then
  bad "Alertmanager is not answering at $ALERTMANAGER"
else
  NARR=$(printf '%s' "$AMALERTS" | jq_ 'len(d)')
  if [ "${NARR:-0}" -gt 0 ]; then
    ok "Alertmanager is holding $NARR alert(s) — they are ARRIVING, not just firing"
  else
    warn "Alertmanager has no alerts"
    note "expected at least Watchdog; if Prometheus says it is firing, the hop between them is broken"
  fi
fi

# ── 6 ────────────────────────────────────────────────────────────────────────────────────────
# THE LINK THAT IS USUALLY MISSING. url_file is read at NOTIFY time, so an absent file does not
# stop Alertmanager starting -- the stack looks healthy and drops every page.
for pair in "paging_url:on-call destination" "watchdog_url:dead-man's-switch monitor"; do
  f="${pair%%:*}"; what="${pair#*:}"
  if [ ! -f "$SECRETS/$f" ]; then
    bad "$SECRETS/$f is MISSING — no $what configured"
    note "cp $SECRETS/${f}.example $SECRETS/$f   and put the URL in it"
  elif [ ! -s "$SECRETS/$f" ] || ! grep -qE '^https?://' "$SECRETS/$f"; then
    bad "$SECRETS/$f exists but holds no http(s) URL"
  else
    # Never print the file. A webhook URL IS the credential.
    ok "$what is configured ($(wc -c < "$SECRETS/$f" | tr -d ' ') bytes)"
  fi
done

# ── 7 ────────────────────────────────────────────────────────────────────────────────────────
# `awk ... END {print s+0}` prints 0 even for EMPTY input, so an unreachable Alertmanager was
# reporting "no failed notifications" -- a green tick for a component that is not running. Ask
# whether the metrics were readable at all before interpreting the number.
METRICS=$(curl -sf --max-time 10 "$ALERTMANAGER/metrics" 2>/dev/null)
if [ -z "$METRICS" ]; then
  FAILED=""
else
  FAILED=$(printf '%s' "$METRICS" | awk '/^alertmanager_notifications_failed_total/ {s+=$2} END {printf "%.0f", s+0}')
fi
if [ -z "$FAILED" ]; then
  warn "could not read Alertmanager's metrics — delivery health is unknown, not healthy"
elif [ "$FAILED" -gt 0 ]; then
  bad "Alertmanager has $FAILED failed notification(s) — alerts are firing and NOT arriving"
  note "docker compose logs alertmanager | grep 'notify retry canceled'"
else
  ok "no failed notifications"
fi

# ── optional: inject a real alert ─────────────────────────────────────────────────────────────
if [ "$SEND" -eq 1 ]; then
  printf '\n\033[1mInjecting a test alert\033[0m\n'
  STAMP=$(date -u +%Y-%m-%dT%H:%M:%S.000Z)
  BEFORE=$(curl -sf --max-time 10 "$ALERTMANAGER/metrics" 2>/dev/null \
    | awk '/^alertmanager_notifications_total/ {s+=$2} END {printf "%.0f", s+0}')
  # Posted straight to Alertmanager rather than via a fake metric, so this tests the DELIVERY
  # half specifically: routing, the receiver, and the URL behind url_file.
  curl -sf --max-time 10 -X POST "$ALERTMANAGER/api/v2/alerts" \
    -H 'content-type: application/json' \
    -d "[{\"labels\":{\"alertname\":\"RuleMindPagingTest\",\"severity\":\"critical\"},
          \"annotations\":{\"summary\":\"Test alert from scripts/verify_paging.sh\",
                           \"description\":\"If you are reading this in your on-call channel, the chain works. It resolves itself.\",
                           \"runbook\":\"docs/runbooks/production.md\"},
          \"startsAt\":\"$STAMP\"}]" >/dev/null 2>&1 \
    && ok "test alert accepted by Alertmanager" || bad "Alertmanager rejected the test alert"

  note "waiting 15s for the group_wait and the delivery attempt…"
  sleep 15
  AFTER=$(curl -sf --max-time 10 "$ALERTMANAGER/metrics" 2>/dev/null \
    | awk '/^alertmanager_notifications_total/ {s+=$2} END {printf "%.0f", s+0}')
  NOWFAILED=$(curl -sf --max-time 10 "$ALERTMANAGER/metrics" 2>/dev/null \
    | awk '/^alertmanager_notifications_failed_total/ {s+=$2} END {printf "%.0f", s+0}')
  if [ "${NOWFAILED:-0}" -gt "${FAILED:-0}" ]; then
    bad "the delivery FAILED — the destination rejected it or is unreachable"
    note "docker compose logs --tail 20 alertmanager"
  elif [ "${AFTER:-0}" -gt "${BEFORE:-0}" ]; then
    ok "delivered — check your on-call destination for 'RuleMindPagingTest'"
    note "THIS is the claim that matters. Everything above it is a precondition."
  else
    warn "no delivery attempt was recorded in 15s; re-run --send, or check group_wait"
  fi
fi

printf '\n  %d ok, %d failed, %d warning(s)\n\n' "$PASS" "$FAIL" "$WARN"
[ "$FAIL" -gt 0 ] && exit 1
exit 0
