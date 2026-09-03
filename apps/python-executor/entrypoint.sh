#!/bin/sh
# Thin exec wrapper. This script used to run `alembic upgrade head` before serving.
# That was removed, for two reasons:
#
#  1. It swallowed failure. The `if python3 -m alembic ... upgrade head; then ...
#     else echo WARNING; fi` form suppresses the non-zero exit that `set -e` exists
#     to catch, so a migration that failed printed a warning and the API started
#     anyway — against a stale schema, in production, silently.
#  2. Every replica raced. There was no lock and no ordering: N API pods rolling out
#     simultaneously all ran the same Alembic chain against the same database.
#
# Migrations now run exactly once per release, before any application pod starts:
#   - Kubernetes: helm/rulemind/templates/job-migrate.yaml, a pre-install/pre-upgrade
#     Helm hook whose failure fails the release.
#   - Compose:    the one-shot `migrate` service in docker-compose.yml, which api and
#     worker wait on with `condition: service_completed_successfully`.
#
# Keeping the wrapper (rather than dropping ENTRYPOINT) means `command:`/`args:`
# overrides in compose and in the chart keep behaving the way they already do.
set -e

echo "[entrypoint] starting: $*"
exec "$@"
