FROM python:3.11-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# libgomp1 = OpenMP runtime, hard-required by xgboost / lightgbm (the ML model
# hosting stack). The slim base image doesn't ship it, so importing them fails
# with "libgomp.so.1: cannot open shared object file" without this.
RUN apt-get update \
  && apt-get install -y --no-install-recommends libgomp1 \
  && rm -rf /var/lib/apt/lists/*

COPY apps/python-executor/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY apps/python-executor/alembic.ini ./alembic.ini
COPY apps/python-executor/app ./app
COPY apps/python-executor/alembic ./alembic
COPY apps/python-executor/package.json ./package.json
COPY apps/python-executor/entrypoint.sh ./entrypoint.sh
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
# --workers 4 was 4 independent SQLAlchemy pools (and 4 resident copies of the ML
# stack) per pod: 4 x (DB_POOL_SIZE 20 + DB_MAX_OVERFLOW 20) = 160 connections from
# a single pod, against a Postgres default max_connections of 100. See the
# connection-budget arithmetic in helm/rulemind/values.yaml before raising this.
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2"]
