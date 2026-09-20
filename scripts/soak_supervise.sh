#!/usr/bin/env bash
# Keep the month-long run alive, and page if it cannot.
#
# WHY. `restart: unless-stopped` on a container does nothing when the DAEMON is gone. On
# 2026-09-20 Colima stopped, all twelve containers went with it, and the traffic generator kept
# firing into a dead API for four and a half hours. Nothing restarted anything and nothing said
# so -- the status check reported "running" because a process existed.
#
# Docker restart policies solve the wrong layer. This solves the layer underneath: the VM, the
# stack, and the generator, in that order, because each depends on the one before it.
#
#   scripts/soak_supervise.sh          # one check-and-repair pass
#   scripts/soak_supervise.sh --check  # report only, change nothing
#   scripts/soak_supervise.sh --loop   # run forever, checking every 5 minutes
#
# WHY --loop AND NOT LAUNCHD. A launchd agent was the obvious answer and it does not work here:
# macOS TCC blocks agents from reading ~/Desktop without Full Disk Access, and this repo lives
# there. The agent loaded, reported exit 0 to `launchctl list`, and failed every five minutes
# with:
#
#     /bin/bash: .../scripts/soak_supervise.sh: Operation not permitted
#
# Silent from the outside -- a supervisor that looks installed and supervises nothing, which is
# worse than none. `--loop` inherits the permissions of the shell that started it, so it simply
# works. The trade is honest and stated in the README: it does not survive a REBOOT. It does
# survive the thing that actually happened, which is the Docker VM dying under a running session.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

LOG_DIR="${RULEMIND_SOAK_LOG_DIR:-$PWD/.runtime/soak}"
LOG="$LOG_DIR/supervisor.log"
PID_FILE="$LOG_DIR/soak.pid"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# --loop re-executes this script every interval. A subshell per pass rather than a `while` around
# the body, so one bad pass cannot leave state set for the next.
if [ "${1:-}" = "--loop" ]; then
  mkdir -p "$LOG_DIR"
  echo $$ > "$LOG_DIR/supervisor.pid"
  trap 'rm -f "$LOG_DIR/supervisor.pid"; exit 0' INT TERM
  while true; do
    bash "$0" >/dev/null 2>&1
    sleep "${RULEMIND_SUPERVISE_INTERVAL_S:-300}"
  done
fi

mkdir -p "$LOG_DIR"
say() { printf '%s  %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$1" | tee -a "$LOG"; }

REPAIRED=""

# ── 1. the VM ────────────────────────────────────────────────────────────────────────────────
if ! docker ps >/dev/null 2>&1; then
  say "docker daemon is DOWN"
  if [ "$CHECK_ONLY" -eq 0 ]; then
    if command -v colima >/dev/null 2>&1 && colima start >/dev/null 2>&1; then
      say "  restarted colima"
      REPAIRED="$REPAIRED colima"
      sleep 10
    else
      say "  FAILED to restart colima"
    fi
  fi
fi

# ── 2. the stack ─────────────────────────────────────────────────────────────────────────────
# Counted, not assumed: `docker compose up -d` on a partially-running stack is a no-op for what
# is already there, so this is safe to call whenever the count is short.
if docker ps >/dev/null 2>&1; then
  UP=$(docker compose ps --format '{{.Service}}' 2>/dev/null | wc -l | tr -d ' ')
  WANT="${RULEMIND_SOAK_EXPECTED_SERVICES:-12}"
  if [ "${UP:-0}" -lt "$WANT" ]; then
    say "only $UP/$WANT services up"
    if [ "$CHECK_ONLY" -eq 0 ]; then
      docker compose --profile observability up -d >/dev/null 2>&1 \
        && { say "  brought the stack up"; REPAIRED="$REPAIRED stack"; }
    fi
  fi
fi

# ── 3. the API ───────────────────────────────────────────────────────────────────────────────
# The generator's target. Checked separately from the container count because a container can be
# "up" and not serving -- which is the whole reason /ready exists.
API_OK=0
for _ in 1 2 3; do
  curl -sf --max-time 5 http://127.0.0.1:8080/health >/dev/null 2>&1 && { API_OK=1; break; }
  sleep 5
done
[ "$API_OK" -eq 1 ] || say "API is not answering /health"

# ── 4. the generator ─────────────────────────────────────────────────────────────────────────
GEN_ALIVE=0
[ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null && GEN_ALIVE=1

if [ "$GEN_ALIVE" -eq 0 ]; then
  say "traffic generator is not running"
  if [ "$CHECK_ONLY" -eq 0 ] && [ "$API_OK" -eq 1 ]; then
    VENV="${RULEMIND_VENV:-$PWD/.venv-rulemind/bin/python}"
    [ -x "$VENV" ] || VENV="python3"
    nohup "$VENV" scripts/soak_traffic.py \
      --min-rps "${RULEMIND_SOAK_MIN_RPS:-0.4}" \
      --max-rps "${RULEMIND_SOAK_MAX_RPS:-4.0}" >> "$LOG_DIR/soak.log" 2>&1 &
    echo $! > "$PID_FILE"
    say "  restarted the generator (pid $(cat "$PID_FILE"))"
    REPAIRED="$REPAIRED generator"
  fi
fi

# ── 5. keep the machine awake ────────────────────────────────────────────────────────────────
CAF_PID_FILE="$LOG_DIR/caffeinate.pid"
if ! { [ -f "$CAF_PID_FILE" ] && kill -0 "$(cat "$CAF_PID_FILE")" 2>/dev/null; }; then
  if [ "$CHECK_ONLY" -eq 0 ] && command -v caffeinate >/dev/null 2>&1; then
    nohup caffeinate -s -i >/dev/null 2>&1 &
    echo $! > "$CAF_PID_FILE"
    say "restarted caffeinate"
    REPAIRED="$REPAIRED caffeinate"
  fi
fi

# ── 6. say so ────────────────────────────────────────────────────────────────────────────────
# A supervisor that repairs silently is a supervisor that hides a machine falling over every
# twenty minutes. Repairs are pushed to the on-call topic so a pattern is visible, at priority 3
# -- worth reading, not worth waking up for.
if [ -n "$REPAIRED" ] && [ "$CHECK_ONLY" -eq 0 ]; then
  TOPIC_FILE="$PWD/observability/secrets/topic"
  if [ -s "$TOPIC_FILE" ]; then
    curl -s -o /dev/null --max-time 15 -X POST "${RULEMIND_NTFY_BASE:-https://ntfy.sh}/" \
      -H 'content-type: application/json' \
      -d "{\"topic\":\"$(cat "$TOPIC_FILE")\",\"title\":\"[SOAK] repaired:$REPAIRED\",\"message\":\"The supervisor restarted:$REPAIRED. The month's series has a gap where this was down.\",\"priority\":3,\"tags\":[\"wrench\"]}" \
      2>/dev/null || true
  fi
fi

[ -z "$REPAIRED" ] && say "healthy" || say "repaired:$REPAIRED"
exit 0

