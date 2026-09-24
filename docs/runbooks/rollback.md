# Rollback runbook

**This page did not exist until 2026-09-23.** A grep for `rollback` across `docs/runbooks/`,
`README.md` and `CHANGELOG.md` returned one unrelated hit. The chart shipped a migration hook, a
PodDisruptionBudget and an HPA, and no answer to *"the deploy is bad, now what"* — which is the
question you have at 3am, not the one you have while reading the README.

Read the **Before you start** section even if you are in a hurry. Two of the three facts in it
will change what you do.

---

## Before you start

**1. A rollback does not roll back the database.** `helm rollback` reverts Deployments. The
migration Job already ran, and it is a *forward* hook — `pre-install,pre-upgrade`, never
`pre-rollback`. So you are putting the previous application binary in front of the **new**
schema, and nothing anywhere checks that the two are compatible: there is no schema-version gate
in `app/main.py`, no read of `alembic_version`, no refusal to serve.

That is usually fine, because Alembic revisions here are additive (new columns, new tables). It
is **not** fine for a revision that drops or renames something the old code reads. Check the
revision that ran before you decide:

```bash
kubectl -n <ns> logs job/rulemind-migrate-1 | tail -40     # while the Job still exists — see (3)
ls apps/python-executor/alembic/versions/ | tail -5
```

**2. Do not reach for `alembic downgrade`.** The initial revision's downgrade is
`Base.metadata.drop_all(bind)` — it drops the entire schema. Downgrades here are not a rehearsed
path; restoring from backup (`docs/runbooks/backup-restore.md`) is.

**3. A failed migration deletes its own logs in 10 minutes.** `values.yaml` sets
`ttlSecondsAfterFinished: 600` on the Job, so the pod that tells you *why* the deploy failed is
garbage-collected while you are still reading the alert. **Capture the logs first:**

```bash
kubectl -n <ns> logs job/rulemind-migrate-1 > /tmp/rulemind-migrate-$(date +%s).log
```

If it is already gone, `helm get hooks <release> -n <ns>` still shows what was applied, and the
`alembic_version` row tells you where the schema stopped.

---

## The deploy failed and never came up

The migrate Job is a hook, so a failure fails the release and the old pods are still serving.
**You are not down.** Do not rush.

```bash
helm status <release> -n <ns>
kubectl -n <ns> logs job/rulemind-migrate-1 > /tmp/migrate.log   # DO THIS FIRST (see 3 above)
kubectl -n <ns> get pods -l app=rulemind-api                      # old pods, still Running
```

Fix forward if the cause is obvious and small (a missing secret, a bad value). Otherwise
`helm rollback <release> -n <ns>` returns the release record to the previous revision; since the
new pods never started, there is nothing to undo but the record.

---

## The deploy came up and is wrong

Bad decisions, a latency cliff, errors in the log. The binary is the suspect.

```bash
helm history <release> -n <ns>                     # find the last good REVISION
helm rollback <release> <REVISION> -n <ns> --wait
kubectl -n <ns> rollout status deploy/rulemind-api
```

Then confirm you actually changed something:

```bash
kubectl -n <ns> get deploy rulemind-api -o jsonpath='{.spec.template.spec.containers[0].image}{"\n"}'
```

**If that prints the same image you were running, the rollback did nothing.** That is why
`image.*.tag` defaults to the chart's `appVersion` and not `latest`: with both revisions on
`:latest`, Kubernetes pulls the same digest and `helm rollback` reports success while changing
nothing at all. If you are on `:latest`, roll back by pinning an explicit tag instead:

```bash
helm upgrade <release> helm/rulemind -n <ns> --reuse-values \
  --set image.api.tag=<previous-version> --set image.web.tag=<previous-version> --wait
```

---

## Rolling back a policy, not the platform

Usually the right answer. A bad decision outcome is almost always a policy change, not a
release, and the product has a first-class path for it that does not touch the cluster:

```
GET  /api/v1/policies/{id}/releases      # the promotion timeline, each with a snapshot
POST /api/v1/policies/{id}/rollback      # {"promotion_id": N, "reason": "..."}
```

It restores the policy and its referenced rules, scorecards and tables from that release's
snapshot, and records the restore as a **forward** promotion — history stays append-only, so the
audit trail shows what happened rather than pretending it did not. Requires AUTHOR; a viewer is
refused.

Prefer this to a platform rollback whenever the symptom is *decisions are wrong* rather than
*the service is unhealthy*. It is faster, it is scoped to one policy, and it leaves the audit
chain intact.

---

## Rotating a secret that leaked during the incident

`helm upgrade --set env.configKey=...` **did nothing to running pods** before 2026-09-23: the
Secret is a hook consumed via `envFrom`, so changing it left the pod template byte-identical and
Helm performed no rollout. The `checksum/secret` annotation now forces one. Verify by pod age,
not by the absence of an error:

```bash
kubectl -n <ns> get pods -l app=rulemind-api -o wide    # AGE must reset
```

With `existingSecret` the chart cannot see the content, so nothing rolls automatically:

```bash
kubectl -n <ns> rollout restart deploy/rulemind-api deploy/rulemind-worker
```

Re-encrypting stored config after a master-key change is a separate, scripted step:
`apps/python-executor/scripts/rotate_config_key.py` (tested in `tests/test_crypto_rotation.py`).
It is referenced from `.env.example` and from code comments, and until now from no runbook.

---

## What this page still cannot tell you

There is **no schema/application compatibility gate**. Nothing refuses to serve when the binary
and `alembic_version` disagree, so "is this rollback safe" is a judgement you make by reading the
revision, not a question the system answers. That is a real gap and it is named here rather than
papered over.

No rollback drill has been rehearsed against a real cluster. The backup/restore path **has**
been (`docs/runbooks/backup-restore.md`, verified on both lanes); this one has not.
