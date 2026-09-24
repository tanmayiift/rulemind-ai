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
    # STOP THE SUPERVISOR FIRST, AND WAIT FOR IT TO ACTUALLY BE GONE.
    #
    # `kill` returning 0 means the signal was delivered, not that the process died, and this
    # printed "stopped the supervisor" on that basis. The loop traps TERM -- but bash defers a
    # trap while a foreground child runs, and the loop spends almost all its time inside
    # `sleep 300`. So the supervisor stayed alive for up to five more minutes after --stop
    # reported success, and its next pass RESTARTED the generator this command had just killed.
    # Measured: --stop reported three green ticks and left pid 82565 running.
    #
    # It also has to happen before the generators are killed, for the same reason: kill them
    # first and a live supervisor simply puts them back.
    if [ -f "$LOG_DIR/supervisor.pid" ]; then
      SUP_PID="$(cat "$LOG_DIR/supervisor.pid" 2>/dev/null)"
    else
      SUP_PID="$(pgrep -f 'soak_supervise.sh --loop' 2>/dev/null | head -1)"
    fi
    if [ -n "${SUP_PID:-}" ] && kill -0 "$SUP_PID" 2>/dev/null; then
      kill "$SUP_PID" 2>/dev/null
      for _ in $(seq 20); do kill -0 "$SUP_PID" 2>/dev/null || break; sleep 0.5; done
      if kill -0 "$SUP_PID" 2>/dev/null; then
        # Deferred trap, still sleeping. SIGKILL is correct here: there is no state to flush,
        # and leaving it alive is what breaks the command.
        kill -9 "$SUP_PID" 2>/dev/null
        sleep 1
      fi
      kill -0 "$SUP_PID" 2>/dev/null \
        && bad "the supervisor (pid $SUP_PID) would not die — kill it by hand before restarting" \
        || ok "stopped the supervisor (pid $SUP_PID), confirmed gone"
    fi
    # Anything else still looping, by process rather than by pid file.
    for pid in $(pgrep -f 'soak_supervise.sh --loop' 2>/dev/null); do
      kill -9 "$pid" 2>/dev/null && ok "stopped a second supervisor loop ($pid)"
    done
    rm -f "$LOG_DIR/supervisor.pid"
    # Every generator, not just the one in the pid file.
    for pid in $(pgrep -f "soak_traffic.py" 2>/dev/null); do
      kill "$pid" 2>/dev/null && ok "stopped generator $pid"
    done
    sleep 1
    LEFT="$(pgrep -f 'soak_traffic.py' 2>/dev/null | tr '\n' ' ')"
    [ -n "$LEFT" ] && bad "generator(s) still running after --stop: $LEFT"
    [ -f "$CAF_PID_FILE" ] && kill "$(cat "$CAF_PID_FILE")" 2>/dev/null && ok "released caffeinate"
    rm -f "$PID_FILE" "$CAF_PID_FILE"
    note "the compose stack is left running; 'docker compose --profile observability down' stops it"
    exit 0 ;;
esac

# By PROCESS, not just the pid file: the supervisor may have started one whose pid this file
# does not know about, and starting a second doubles the traffic rate.
if pgrep -f "soak_traffic.py" >/dev/null 2>&1; then
  bad "a generator is already running (pid $(pgrep -f 'soak_traffic.py' | tr '\n' ' ')) — use --status or --stop"
  exit 1
fi

printf '\n\033[1mStarting the soak run\033[0m\n'

# The observability profile, because the point of the month is the metric history. Without
# Prometheus there is nothing to calibrate from at the end.
# --build so the month runs the code in the working tree, not whatever image happened to be
# tagged when the last run started. Without it a soak can spend weeks measuring a build nobody
# has on disk any more.
docker compose --profile observability up -d --build >/dev/null 2>&1 \
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

# ONE OWNER FOR THE GENERATOR, AND IT IS THE SUPERVISOR.
#
# This block used to start a generator directly, and that raced with the supervisor launched
# immediately above: the guard near the top of this script checks `pgrep -f soak_traffic.py`
# BEFORE the supervisor exists, the supervisor's first pass then sees no generator and starts
# one, and this block started a second. Measured after a clean restart: two generators, both
# orphaned to PPID 1, identical arguments.
#
# Two generators is not a tidiness problem. The whole point of the run is a shaped arrival rate
# -- a diurnal curve between --min-rps and --max-rps -- and two of them silently double it, so
# every quantile the month produces describes traffic nobody configured. It is the same class as
# the /metrics worker bug: the measurement is wrong and nothing looks wrong.
#
# The guard could have been moved later, but the real fix is that two things were allowed to
# create the same singleton. The supervisor already owns restart-if-missing; let it own start-up
# too, and wait for it here.
VENV="${RULEMIND_VENV:-$PWD/.venv-rulemind/bin/python}"
[ -x "$VENV" ] || VENV="python3"
for _ in $(seq 24); do
  pgrep -f "soak_traffic.py" >/dev/null 2>&1 && break
  sleep 5
done
GEN_PIDS="$(pgrep -f 'soak_traffic.py' 2>/dev/null | tr '\n' ' ')"
GEN_COUNT="$(printf '%s' "$GEN_PIDS" | wc -w | tr -d ' ')"
case "$GEN_COUNT" in
  0) bad "the supervisor did not start a generator within 2m — see $LOG_DIR/soak.log"
     tail -5 "$LOG_DIR/soak.log"; exit 1 ;;
  1) printf '%s' "$GEN_PIDS" | tr -d ' ' > "$PID_FILE"
     ok "traffic generator running (pid $(cat "$PID_FILE")), owned by the supervisor" ;;
  *) bad "MORE THAN ONE generator is running (pids $GEN_PIDS) — the arrival rate is multiplied"
     note "stop them all and restart: scripts/soak_start.sh --stop && scripts/soak_start.sh"
     exit 1 ;;
esac

printf '\n'
note "log:     $LOG_DIR/soak.log"
note "status:  scripts/soak_start.sh --status"
note "in ~30d: scripts/calibrate_thresholds.py --window 30d"
printf '\n'
