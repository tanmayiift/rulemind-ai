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
# The on-call destination is now a TOPIC, not a URL: Alertmanager posts to the in-network relay
# (which formats the envelope into a real notification) and the relay reads the topic at notify
# time. The secret moved one hop; the reasoning did not.
if [ ! -f "$SECRETS/topic" ]; then
  bad "$SECRETS/topic is MISSING — no on-call destination configured"
  note "cp $SECRETS/topic.example $SECRETS/topic   and put an unguessable topic name in it"
elif [ ! -s "$SECRETS/topic" ]; then
  bad "$SECRETS/topic is empty"
else
  # Never print it. The topic name IS the credential: anyone who knows it can read your alerts.
  ok "on-call destination is configured ($(wc -c < "$SECRETS/topic" | tr -d ' ') bytes)"
fi

# The relay is the hop that turns the envelope into something a human can act on. Alertmanager
# posting successfully to a relay that cannot reach its upstream is a delivery that looks fine
# and reaches nobody, so ask the relay itself.
RELAY=$(curl -sf --max-time 10 "${RULEMIND_RELAY_URL:-http://127.0.0.1:9097}/" 2>/dev/null)
if [ -z "$RELAY" ]; then
  warn "the alert relay is not answering — start it with: docker compose --profile observability up -d alert-relay"
  note "without it Alertmanager delivers its raw JSON envelope, which is a page nobody can read"
else
  ok "alert relay is up ($(printf '%s' "$RELAY" | tr -d '{}\"' | cut -c1-60))"
fi

if [ ! -f "$SECRETS/watchdog_url" ]; then
  bad "$SECRETS/watchdog_url is MISSING — no dead-man's-switch monitor configured"
  note "this one must live OUTSIDE this stack: a monitor on the same machine shares fate with"
  note "what it watches. See observability/secrets/README.md for the Grafana OnCall click-path."
elif ! grep -qE '^https?://' "$SECRETS/watchdog_url"; then
  bad "$SECRETS/watchdog_url exists but holds no http(s) URL"
else
  ok "dead-man's-switch monitor is configured ($(wc -c < "$SECRETS/watchdog_url" | tr -d ' ') bytes)"

  # DOES IT ANSWER? A URL in a file proves someone pasted something. Heartbeat endpoints are
  # tokenised, so a stale or mistyped token 404s -- and because a heartbeat's whole job is to be
  # SILENT when healthy, a broken one looks exactly like a working one right up until the day it
  # was supposed to save you. The only way to tell is to send one and read the status.
  #
  # Never printed: the URL IS the credential.
  WD_CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
    -X POST "$(cat "$SECRETS/watchdog_url")" 2>/dev/null)
  case "${WD_CODE:-000}" in
    2*)   ok "the monitor accepted a heartbeat (HTTP $WD_CODE)" ;;
    000)  bad "the monitor is unreachable — no response"
          note "check egress from this host, and that the URL host is right" ;;
    401|403|404)
          # Measured against Grafana OnCall: a wrong token answers 403, not 404. Both are the
          # same mistake and the same fix, so they share a branch rather than leaving 403 to
          # fall through to a message that says nothing.
          bad "the monitor answered HTTP $WD_CODE — the token is wrong, or the heartbeat was deleted"
          note "re-copy the whole URL from the integration's Heartbeat panel" ;;
    *)    bad "the monitor answered HTTP $WD_CODE — a heartbeat is not being recorded" ;;
  esac
  note "a green tick here means the endpoint RECEIVES. That it ALERTS on silence is a setting"
  note "on their side — stop alertmanager for one interval once, and confirm you get paged."
fi

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
  # A LIFETIME counter, so a single transient blip reds this check forever. Measured here: 440
  # DNS failures inside alert-relay during one outage window, all recovered, and this line then
  # reported "alerts are firing and NOT arriving" on a chain that was working. Ask Prometheus
  # for the RECENT rate instead, and fall back to the lifetime number only if it cannot answer.
  RECENT=$(curl -sf --max-time 10 --get "$PROM/api/v1/query" \
    --data-urlencode 'query=sum(increase(alertmanager_notifications_failed_total[15m]))' 2>/dev/null \
    | awk -F'"' '/"value"/ {for (i=1;i<=NF;i++) if ($i ~ /^[0-9.eE+-]+$/) v=$i} END {printf "%.0f", v+0}')
  if [ -z "$RECENT" ]; then
    warn "Alertmanager reports $FAILED lifetime failure(s); Prometheus could not be asked whether"
    note "any are recent. Treat delivery health as unknown, not healthy."
  elif [ "$RECENT" -gt 0 ]; then
    bad "Alertmanager failed $RECENT notification(s) in the last 15m — alerts are firing and NOT arriving"
    note "docker compose logs alertmanager | grep 'notify retry canceled'"
  else
    ok "no failed notifications in the last 15m ($FAILED lifetime, all older)"
  fi
else
  ok "no failed notifications"
fi

# ── 8 ─────────────────────────────────────────────────────────────────────────────────────
# Every alert rule must query a metric something can actually produce. A rule naming a metric
# Prometheus has no series for evaluates to empty forever and can never fire, while /alerts
# renders it green. RuleMindPagingIsFailing -- the rule whose job is to catch "alerts are firing
# and NOT arriving" -- was exactly that for as long as it existed, because prometheus.yml had no
# scrape job for Alertmanager. See scripts/check_alert_metrics.py for the full account.
ALERT_METRICS=$(RULEMIND_PROM_URL="$PROM" RM_REPO_ROOT="$PWD" \
  python3 scripts/check_alert_metrics.py 2>/dev/null)
AM_RC=$?
UNDEFINED=$(printf '%s\n' "$ALERT_METRICS" | sed -n 's/^UNDEFINED //p')
UNEMITTED=$(printf '%s\n' "$ALERT_METRICS" | sed -n 's/^UNEMITTED //p')
CHECKED=$(printf '%s\n' "$ALERT_METRICS" | sed -n 's/^CHECKED //p')
if [ "$AM_RC" -eq 2 ]; then
  warn "could not ask Prometheus whether the alert rules query live metrics"
elif [ -n "$UNDEFINED" ]; then
  bad "alert rule(s) query a metric with NO series and NO definition in the source — they can never fire:"
  printf '%s\n' "$UNDEFINED" | while IFS= read -r line; do [ -n "$line" ] && note "$line"; done
  note "usually a missing scrape_config in observability/prometheus.yml"
else
  ok "all ${CHECKED:-?} alert rules query a metric something can produce"
fi
if [ -n "$UNEMITTED" ]; then
  warn "alert rule(s) query a metric that is defined but has never been emitted here:"
  printf '%s\n' "$UNEMITTED" | while IFS= read -r line; do [ -n "$line" ] && note "$line"; done
  note "the rule is UNTESTED, not dead — nothing has exercised that code path in this stack"
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
