#!/bin/sh
# Apply schema migrations before serving. `create_all` (in Storage.__init__) builds
# tables on a FRESH database, but it never ALTERs an existing one — so a persistent
# volume that predates a new column would 500 on boot. Running the Alembic chain here
# makes upgrades safe: the additive migrations are idempotent, so this is a no-op on a
# fresh or already-current database and adds the missing column on an older one.
set -e

echo "[entrypoint] applying database migrations (alembic upgrade head)…"
if python3 -m alembic -c alembic.ini upgrade head; then
  echo "[entrypoint] migrations up to date."
else
  echo "[entrypoint] WARNING: alembic upgrade failed; continuing (create_all covers fresh databases)."
fi

echo "[entrypoint] starting: $*"
exec "$@"
