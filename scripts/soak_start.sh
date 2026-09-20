#!/usr/bin/env bash
# Start the month-long calibration run, and keep it running.
#
# WHAT THIS IS. docs/PRODUCTION-READINESS.md says the way to close "zero production hours" is a
# month of continuous operation under realistic traffic, then recalibrate from the result. This
# starts that on THIS machine, which is the free option.
#
# BE CLEAR ABOUT WHAT THIS MACHINE IS NOT. It is a laptop, not a server:
#
#   * it sleeps. `caffeinate` below prevents idle sleep while this runs, but a closed lid on
#     battery still sleeps, and every sleep is a gap in the series.
#   * it is ONE failure domain. Nothing here tests network partitions, a provider's maintenance
#     window, or a restart you did not initiate.
#   * its disk, CPU and memory are shared with whatever else you are doing, so the numbers carry
#     more variance than a dedicated box would.
#
# What it DOES give you is the thing a 30-second load test cannot: duration. Memory creep,
# connection-pool exhaustion, log growth, disk filling, certificate expiry, a container that dies
# at 3am and does not come back -- all of those are functions of uptime, and all of them show up
# here. That is most of the value, for free.
#
#   scripts/soak_start.sh              # start the stack + traffic
#   scripts/soak_start.sh --status     # what is running, and for how long
#   scripts/soak_start.sh --stop
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

LOG_DIR="${RULEMIND_SOAK_LOG_DIR:-$PWD/.runtime/soak}"
PID_FILE="$LOG_DIR/soak.pid"
CAF_PID_FILE="$LOG_DIR/caffeinate.pid"

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }

mkdir -p "$LOG_DIR"

status() {
  printf '\n\033[1mSoak run\033[0m\n'
  # LIVENESS IS NOT HEALTH, and reporting the first as the second is the failure this whole
  # strand of work keeps removing -- so it is galling that this script shipped with it.
  #
  # On 2026-09-20 Colima stopped. The generator stayed alive and kept firing into a dead API for
  # hours; `--status` said "ok traffic generator running" the entire time, because it checked
  # that a process existed. The log said `failed=456` of 906 and nothing read the log.
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
    local pid last failed
    pid="$(cat "$PID_FILE")"
    last="$(tail -1 "$LOG_DIR/soak.log" 2>/dev/null)"
    failed="$(printf '%s' "$last" | sed -n 's/.*failed=\([0-9]*\).*/\1/p')"
    if [ "${failed:-0}" -gt 0 ]; then
      bad "generator is alive (pid $pid) but its last window had ${failed} FAILED requests"
      note "a live process is not a working one — check the API and the docker daemon"
    else
      ok "traffic generator healthy (pid $pid, up $(ps -o etime= -p "$pid" | tr -d ' '))"
    fi
    [ -n "$last" ] && printf '        %s\n' "$last"
  else
    bad "traffic generator is not running"
  fi

  # The daemon underneath everything. `restart: unless-stopped` on a container does nothing when
  # the DAEMON is gone, which is exactly what happened.
  if docker ps >/dev/null 2>&1; then
    ok "docker daemon responding"
  else
    bad "DOCKER DAEMON IS DOWN — every container is gone and nothing will restart them"
    note "colima start && docker compose --profile observability up -d"
  fi

  if curl -sf --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1; then
    ok "API answering /health"
  else
    bad "API is NOT answering — the traffic being sent is going nowhere"
  fi
  if [ -f "$CAF_PID_FILE" ] && kill -0 "$(cat "$CAF_PID_FILE")" 2>/dev/null; then
    ok "caffeinate holding the machine awake"
  else
    note "caffeinate is NOT running — the machine may sleep and leave gaps in the series"
  fi
  if [ -f "$LOG_DIR/supervisor.pid" ] && kill -0 "$(cat "$LOG_DIR/supervisor.pid")" 2>/dev/null; then
    ok "supervisor loop running (pid $(cat "$LOG_DIR/supervisor.pid"))"
  else
    bad "supervisor is NOT running — nothing will restart the stack if the VM dies"
    note "bash scripts/soak_supervise.sh --loop &"
  fi
  local n; n=$(docker compose ps --format '{{.Service}}' 2>/dev/null | wc -l | tr -d ' ')
  ok "$n compose service(s) up"
  # The number that decides whether this month will be usable. calibrate_thresholds.py needs
  # sampled minutes, and a series with holes in it is what "not enough data" looks like later.
  local mins
  mins=$(curl -sf -G --max-time 10 "http://127.0.0.1:9090/api/v1/query" \
    --data-urlencode 'query=count_over_time(sum(rate(rulemind_decisions_total[5m]))[30d:1m])' \
    2>/dev/null | "${RULEMIND_PY:-python3}" -c \
    'import sys,json;d=json.load(sys.stdin);r=d["data"]["result"];print(int(float(r[0]["value"][1])) if r else 0)' 2>/dev/null)
  ok "sampled minutes of decision traffic in the last 30d: ${mins:-unknown}"
  note "calibrate_thresholds.py wants 200+; at ~1440/day that is a few hours, but the SHAPE"
  note "needs weeks — one day gives you one of each hour, which is not a distribution."
  printf '\n'
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --stop)
    [ -f "$LOG_DIR/supervisor.pid" ] && kill "$(cat "$LOG_DIR/supervisor.pid")" 2>/dev/null \
      && ok "stopped the supervisor"
    rm -f "$LOG_DIR/supervisor.pid"
    [ -f "$PID_FILE" ] && kill "$(cat "$PID_FILE")" 2>/dev/null && ok "stopped the generator"
    [ -f "$CAF_PID_FILE" ] && kill "$(cat "$CAF_PID_FILE")" 2>/dev/null && ok "released caffeinate"
    rm -f "$PID_FILE" "$CAF_PID_FILE"
    note "the compose stack is left running; 'docker compose --profile observability down' stops it"
    exit 0 ;;
esac

if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; then
  bad "already running (pid $(cat "$PID_FILE")) — use --status or --stop"
  exit 1
fi

printf '\n\033[1mStarting the soak run\033[0m\n'

# The observability profile, because the point of the month is the metric history. Without
# Prometheus there is nothing to calibrate from at the end.
docker compose --profile observability up -d >/dev/null 2>&1 \
  && ok "compose stack up (observability profile)" \
  || { bad "compose failed to start"; exit 1; }

for i in $(seq 1 60); do
  curl -sf --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1 && break
  sleep 2
done
curl -sf --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1 \
  && ok "API answering" || { bad "API never became healthy"; exit 1; }

# -s prevents SYSTEM sleep, -i idle sleep. Not a substitute for leaving it plugged in: on battery
# with the lid shut, macOS sleeps regardless.
if command -v caffeinate >/dev/null 2>&1; then
  nohup caffeinate -s -i > /dev/null 2>&1 &
  echo $! > "$CAF_PID_FILE"
  ok "caffeinate holding the machine awake (pid $(cat "$CAF_PID_FILE"))"
fi

# The supervisor, in --loop mode. Without it a dead Docker VM stays dead: `restart:
# unless-stopped` is a container policy and does nothing when the DAEMON is gone. That is not
# hypothetical -- on 2026-09-20 Colima stopped and the generator fired into a dead API for four
# and a half hours while --status reported "running".
if ! { [ -f "$LOG_DIR/supervisor.pid" ] && kill -0 "$(cat "$LOG_DIR/supervisor.pid")" 2>/dev/null; }; then
  nohup bash scripts/soak_supervise.sh --loop >/dev/null 2>&1 &
  sleep 2
  [ -f "$LOG_DIR/supervisor.pid" ] \
    && ok "supervisor loop running (pid $(cat "$LOG_DIR/supervisor.pid")) — checks every 5m" \
    || bad "supervisor did not start"
else
  ok "supervisor already running (pid $(cat "$LOG_DIR/supervisor.pid"))"
fi

VENV="${RULEMIND_VENV:-$PWD/.venv-rulemind/bin/python}"
[ -x "$VENV" ] || VENV="python3"
nohup "$VENV" scripts/soak_traffic.py \
  --min-rps "${RULEMIND_SOAK_MIN_RPS:-0.4}" \
  --max-rps "${RULEMIND_SOAK_MAX_RPS:-4.0}" \
  >> "$LOG_DIR/soak.log" 2>&1 &
echo $! > "$PID_FILE"
sleep 3
kill -0 "$(cat "$PID_FILE")" 2>/dev/null \
  && ok "traffic generator running (pid $(cat "$PID_FILE"))" \
  || { bad "generator exited immediately — see $LOG_DIR/soak.log"; tail -5 "$LOG_DIR/soak.log"; exit 1; }

printf '\n'
note "log:     $LOG_DIR/soak.log"
note "status:  scripts/soak_start.sh --status"
note "in ~30d: scripts/calibrate_thresholds.py --window 30d"
printf '\n'
