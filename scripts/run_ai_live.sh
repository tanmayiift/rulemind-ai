#!/usr/bin/env bash
# Run the live-AI suite (#123) against a real provider.
#
# THE KEY NEVER TOUCHES GIT AND IS NEVER ECHOED. It is read from `.env.ai` (gitignored via
# `.env.*`) into this process's environment and nothing else. This script prints the provider and
# the key's LENGTH, never the key -- a fingerprint is enough to tell "pasted correctly" from
# "pasted a trailing newline", which is the only thing anyone actually needs to see.
#
#   cp .env.ai.example .env.ai      # then paste the key into .env.ai
#   bash scripts/run_ai_live.sh
#
# An environment variable wins over the file, so CI or a one-off can do:
#   RULEMIND_AI_LIVE_KEY=sk-... RULEMIND_AI_LIVE_PROVIDER=openai bash scripts/run_ai_live.sh
#
# THESE CALLS COST MONEY -- six generation endpoints against a real model. That is why this is a
# script you run, not a lane in the gate.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; }
note() { printf '        %s\n' "$1"; }

VENV="${RULEMIND_VENV:-$PWD/.venv-rulemind/bin/python}"
[ -x "$VENV" ] || { bad "no venv at $VENV"; note "python3 -m venv .venv-rulemind && .venv-rulemind/bin/pip install -r apps/python-executor/requirements.txt"; exit 2; }

# `set -a` so the file's assignments export without this script naming each one -- and so adding
# a variable to .env.ai.example does not require editing this script too.
if [ -f .env.ai ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env.ai
  set +a
  ok "read .env.ai"
elif [ -z "${RULEMIND_AI_LIVE_KEY:-}" ]; then
  bad ".env.ai not found and RULEMIND_AI_LIVE_KEY is not set"
  note "cp .env.ai.example .env.ai   then paste the key into it"
  exit 2
fi

PROVIDER="${RULEMIND_AI_LIVE_PROVIDER:-openai}"
KEY="${RULEMIND_AI_LIVE_KEY:-}"

if [ -z "$KEY" ]; then
  bad "RULEMIND_AI_LIVE_KEY is empty"
  note "paste the key after the '=' in .env.ai, with no quotes"
  exit 2
fi

# A trailing newline or space inside a pasted key produces a 401 that reads like a bad key, and
# people then go and regenerate a perfectly good one. Catch it here instead.
CLEAN="$(printf '%s' "$KEY" | tr -d '[:space:]')"
if [ "$CLEAN" != "$KEY" ]; then
  note "the key had surrounding whitespace; using the trimmed value"
  KEY="$CLEAN"
fi

# Length and last four only. Enough to confirm a paste, useless to anyone reading a log.
printf '\n\033[1mLive AI suite\033[0m\n'
ok "provider: $PROVIDER"
ok "key: ${#KEY} chars, ending ...${KEY: -4}"
[ -n "${RULEMIND_AI_LIVE_MODEL:-}" ] && ok "model: $RULEMIND_AI_LIVE_MODEL"
printf '\n  \033[33mThese calls are billed to that key.\033[0m\n\n'

cd apps/python-executor || exit 2
RULEMIND_AI_LIVE=1 \
RULEMIND_AI_LIVE_PROVIDER="$PROVIDER" \
RULEMIND_AI_LIVE_KEY="$KEY" \
RULEMIND_AI_LIVE_MODEL="${RULEMIND_AI_LIVE_MODEL:-}" \
RULEMIND_CONFIG_KEY="${RULEMIND_CONFIG_KEY:-rulemind-test-key}" \
  "$VENV" -m unittest tests.test_ai_live -v
STATUS=$?

printf '\n'
if [ $STATUS -eq 0 ]; then
  ok "live suite passed — #123's endpoints answered a real provider"
else
  bad "live suite failed (exit $STATUS)"
  note "a 401 here means the key; a 502 means the provider rejected the CALL, which is"
  note "the interesting failure and the one #123 exists to find."
fi
exit $STATUS
