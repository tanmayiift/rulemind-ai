#!/bin/sh
# Migrate, then serve. For SINGLE-INSTANCE platforms only — see the warning below.
#
# WHY A SCRIPT RATHER THAN AN INLINE COMMAND. This started life as a `dockerCommand` in
# render.yaml:
#
#     sh -c "python3 -m alembic -c alembic.ini upgrade head && exec python3 -m uvicorn ..."
#
# and the container exited 127 (command not found) without ever printing a line. Render splits
# that string into argv on whitespace rather than handing it to a shell, so the quotes survive
# as literal characters and `sh -c '"python3'` is not a command. Anything with a `&&` in it has
# the same problem. A file has no quoting to get wrong, and — the actual reason it is worth a
# file — it can be run locally against the real image before anyone deploys it:
#
#     docker run --rm -e DATABASE_URL=... rulemind-api ./render-start.sh
#
# WHY MIGRATIONS ARE HERE AT ALL. The Dockerfile's entrypoint deliberately does NOT run them,
# for two reasons that still hold:
#
#   1. It used to swallow failure. `if alembic upgrade head; then ... else echo WARNING; fi`
#      suppresses the non-zero exit that `set -e` exists to catch, so a failed migration printed
#      a warning and the API served against a stale schema, in production, silently.
#   2. Every replica raced. N pods rolling out simultaneously all ran the same chain against the
#      same database with no lock and no ordering.
#
# Kubernetes solves both with a pre-install/pre-upgrade Helm hook Job, and compose with a
# one-shot `migrate` service. Render's free tier offers neither — pre-deploy commands are a paid
# feature — so this script is the third option, and it is only correct because of the two lines
# below.
#
# `set -e` handles (1): a failed migration exits non-zero here and the container never starts
# uvicorn. Render then marks the deploy failed instead of serving a stale schema.
#
# (2) is handled by the deployment shape, not by this script: a free Render service is a single
# instance with no horizontal scaling, so there is exactly one runner. THAT IS THE WHOLE
# ASSUMPTION. If this service is ever scaled past one instance, or copied to a platform that
# runs replicas, migrations must move back to a pre-deploy command or a one-off job first —
# otherwise the race this file's comments describe is reintroduced with no lock to stop it.
set -e

echo "[render-start] alembic upgrade head"
python3 -m alembic -c alembic.ini upgrade head

echo "[render-start] starting uvicorn"
# --workers 1, not the Dockerfile's 2: each worker is a separate SQLAlchemy pool AND a resident
# copy of the ML stack (sklearn, lightgbm, numpy). Two do not fit in the free tier's 512MB, and
# a second buys nothing at 0.1 CPU.
#
# exec, so uvicorn replaces this shell and becomes PID 1. Without it the shell stays as PID 1,
# SIGTERM goes to the shell instead of uvicorn, and the platform's graceful shutdown becomes a
# 30-second wait followed by SIGKILL mid-request.
exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --workers 1
