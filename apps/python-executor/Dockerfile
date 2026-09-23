FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# libgomp1 = OpenMP runtime, hard-required by xgboost / lightgbm (the ML model
# hosting stack). The slim base image doesn't ship it, so importing them fails
# with "libgomp.so.1: cannot open shared object file" without this.
# libgomp1 only. pg_dump/pg_restore are deliberately NOT here -- see below.
RUN apt-get update \
  && apt-get install -y --no-install-recommends libgomp1 \
  && rm -rf /var/lib/apt/lists/*

# WHY NO postgresql-client, THOUGH THE RUNBOOK PRESCRIBES pg_dump.
#
# It was added, measured, and taken out again. Two reasons, both found by running it rather than
# reasoning about it:
#
# 1. THE VERSION MUST MATCH THE SERVER, and Debian 13 ships 17 while this stack runs Postgres 16.
#    The usual advice -- "the client may be newer than the server" -- fails here at RESTORE time:
#
#      pg_restore: error: could not execute query: ERROR: unrecognized configuration parameter
#                  "transaction_timeout"
#      Command was: SET transaction_timeout = 0;
#
#    pg_dump 17 writes a GUC introduced in 17, so a 17 client against a 16 server produces a dump
#    that cannot be restored into that same server. The backup succeeds and is useless, which is
#    precisely the failure docs/runbooks/backup-restore.md opens by warning about. Pinning to the
#    right major needs the PGDG apt repo, which needs curl/gnupg in a slim image -- ~130 MB on an
#    image this document already calls oversized, to carry a tool that must be re-pinned on every
#    Postgres upgrade or it silently starts producing unrestorable dumps.
#
# 2. So run it from a client of the SERVER'S OWN version instead, which you already have:
#      docker compose exec db pg_dump ...
#      kubectl run pgdump --rm -it --image=postgres:16-alpine -- pg_dump ...
#    docs/runbooks/backup-restore.md carries the full form.
#
# The operational SCRIPTS are here (below) because those genuinely belong to the app.

COPY apps/python-executor/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/python-executor/alembic.ini ./alembic.ini
COPY apps/python-executor/app ./app
COPY apps/python-executor/alembic ./alembic
COPY apps/python-executor/package.json ./package.json
COPY apps/python-executor/entrypoint.sh ./entrypoint.sh
# The operational scripts the runbooks tell you to run. They were not in the image, so
# docs/runbooks/backup-restore.md's first command -- `python scripts/backup_verify.py` -- failed
# with "No such file or directory" on the container you are logged into during the incident, and
# scripts/rotate_config_key.py was equally unreachable after a key leak. A runbook whose commands
# do not exist on the box is a runbook you rewrite at 3am. A few tens of KB of Python.
COPY apps/python-executor/scripts ./scripts
RUN mkdir -p /app/apps/python-executor \
  && ln -s /app/app /app/apps/python-executor/app \
  && ln -s /app/alembic /app/apps/python-executor/alembic \
  && chmod +x /app/entrypoint.sh

# Run as a non-root system account. This image executes untrusted customer Python
# in the variable sandbox, and it previously ran as uid 0 — and with no USER
# directive a cluster enforcing the `restricted` Pod Security Standard rejects the
# pod outright. uid 10001 matches packages/rulemind-decide-service/Dockerfile and
# the securityContext runAsUser in helm/rulemind/values.yaml; keep them in sync.
# /app/data is created (and owned) here because app/decision_wal.py writes there
# when DECISION_WAL=1; HOME is set so libraries that cache under $HOME (numba,
# matplotlib, huggingface) have somewhere writable instead of falling over.
RUN useradd -r -u 10001 -m -d /home/rulemind -s /usr/sbin/nologin rulemind \
  && mkdir -p /app/data \
  && chown -R rulemind:rulemind /app /home/rulemind
ENV HOME=/home/rulemind
USER 10001

EXPOSE 8080

# The entrypoint NO LONGER runs migrations. It used to run `alembic upgrade head`
# inside an `if ... else echo WARNING; fi`, which defeats `set -e`: a failed
# migration was logged and the API then booted against a stale schema. Every
# replica also raced every other replica running it. Migrations now happen exactly
# once per release, in the pre-install/pre-upgrade Helm hook Job
# (helm/rulemind/templates/job-migrate.yaml); docker-compose.yml has a one-shot
# `migrate` service that the api and worker wait on.
ENTRYPOINT ["/app/entrypoint.sh"]
# WORKER COUNT IS A CONNECTION-BUDGET DECISION, NOT A CORE-COUNT ONE, and hardcoding it here
# made it look like a property of the image.
#
# The cap of 2 was right for its stated arithmetic: 4 x (DB_POOL_SIZE 20 + DB_MAX_OVERFLOW 20)
# = 160 connections from one pod against a Postgres default max_connections of 100. But the
# pool size is configuration, and at compose's own default of 5+5 four workers need 40. The
# binding constraint was the pool, and the fix was applied to the wrong variable.
#
# Measured on this machine (4-core VM, both perf flags on, 3 rounds, median):
#
#     2 workers  109.9 rps  p50 71.0ms  p95 118.3ms
#     4 workers  140.8 rps  p50 53.8ms  p95  92.5ms     <- +28% throughput, -24% p50
#
# RULEMIND_WORKERS now sets it, defaulting to 2 so nothing changes by accident. Raise it only
# together with the pool: workers x (DB_POOL_SIZE + DB_MAX_OVERFLOW) must stay under your
# server's max_connections, with room for the worker process and anything else connecting.
# helm/rulemind/values.yaml carries the full arithmetic.
ENV RULEMIND_WORKERS=2
CMD ["sh", "-c", "exec python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers ${RULEMIND_WORKERS}"]
