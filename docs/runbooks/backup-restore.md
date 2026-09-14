# Backup and restore

> An unverified backup is not a backup. Every procedure here ends in a restore, because the
> failure that actually happens is not `pg_dump` erroring loudly — it is `pg_dump` succeeding
> for months while writing something that cannot be restored, discovered during the incident.

## What is backed up

The Postgres database is the only durable state that matters. Everything else is rebuildable:

| state | where | recovery |
|---|---|---|
| policies, rules, decisions, audit log, usage | Postgres | **this runbook** |
| decision archive | ClickHouse / S3, via `DECISION_ARCHIVE_SINK` | re-exported from Postgres if retained; otherwise the archive *is* the record |
| bundles | regenerated from policies | `POST /api/v1/bundles/rebuild` |
| Redis (rate limits, decision bus, leader lock) | ephemeral | nothing to do — repopulates |

`RULEMIND_CONFIG_KEY` is **not** in the backup and must be stored separately. Connector
credentials are Fernet-encrypted with it, so a database restored without that key gives you
every policy back and no working integrations. Losing it is unrecoverable.

## Taking a verified backup

```bash
DATABASE_URL=postgresql://... python apps/python-executor/scripts/backup_verify.py --out /var/backups/rulemind --keep
```

This dumps, restores into a scratch database, verifies, and drops the scratch. It exits
non-zero if the restore is not trustworthy — wire the exit status into your alerting, not the
log text.

Verification asserts three things:

1. every table in the source is present in the restore;
2. row counts match per table;
3. **the audit hash chain still verifies in the restored copy.** This is the check worth the
   most. A dump that drops or reorders audit rows restores to something that looks healthy by
   every row count and has lost its tamper-evidence — and a compliance guarantee that holds
   only on the primary is not a guarantee. See `app/audit_chain.py`.

Omit `--keep` to verify without retaining the file (what CI does).

## Schedule

Nightly, plus before every migration. `.github/workflows/backup-verify.yml` runs the whole
cycle against a throwaway Postgres on a schedule, so the *script* is exercised continuously
rather than first used during an incident.

Retention: 7 daily, 4 weekly, 12 monthly. Keep at least one copy off the account that hosts
the primary — a backup an attacker who owns your cloud account can delete is not a backup for
the case you most need it.

## Restoring for real

```bash
# 1. Stop writers. The API is stateless, so scale it to zero rather than draining.
kubectl scale deploy/rulemind-api --replicas=0

# 2. Restore into a NEW database, never over the live one -- if the dump turns out to be bad
#    you have destroyed the only remaining copy while finding out.
createdb rulemind_restored
pg_restore --no-owner --no-acl --dbname postgresql://.../rulemind_restored backup.dump

# 3. Verify BEFORE cutting over.
DATABASE_URL=postgresql://.../rulemind_restored \
  python apps/python-executor/scripts/backup_verify.py --out /tmp/verify

# 4. Cut over by pointing DATABASE_URL at the restored database, then scale back up.
kubectl set env deploy/rulemind-api DATABASE_URL=postgresql://.../rulemind_restored
kubectl scale deploy/rulemind-api --replicas=3

# 5. Confirm the audit chain from the outside.
curl -H "x-api-key: $KEY" https://api.example.com/api/v1/audit/verify
```

## After a restore

- **Decisions between the dump and the incident are gone.** Devices holding them in the SDK
  outbox will re-sync on their own and dedup on `(tenant_id, client_id)`, so let them; do not
  replay by hand.
- **`/api/v1/audit/verify` will report `pre_chain` rows** if the restore predates the chain
  migration. That is expected and honest — see `alembic/versions/20260903_0017`.
- **Re-anchor the audit head.** The chain detects partial tampering; detecting a full rewrite
  needs the head hash held somewhere the database operator cannot reach. Record the post-restore
  `head` from `/api/v1/audit/verify` wherever you keep the previous anchors, and confirm it
  matches the last anchor taken before the incident. If it does not, the restore is from a
  different history than you think.
