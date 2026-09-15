# Production runbook

One section per alert in `observability/alerts.yml`. Each alert's `runbook` annotation links
here by anchor, so the page you need is one click from the notification rather than a search.

**Read this first if you are new to the system:** `/health` and `/ready` answer *different
questions on purpose*. `/health` is liveness — the process is alive and can serve. `/ready` is
readiness — the database, Redis and the sandbox pool are all usable. A container can be healthy
and not ready, and that distinction is what stops Kubernetes restart-looping a pod whose only
problem is that Postgres is briefly gone.

---

## The API is down

`RuleMindApiDown` · critical · `up{job="rulemind-api"} == 0`

Prometheus cannot scrape. That is either the process or the network, and `/ready` tells you
which within seconds.

```bash
kubectl -n rulemind get pods                 # or: docker compose ps
curl -s localhost:8080/health                # liveness — is the process serving at all?
curl -s localhost:8080/ready | jq            # readiness — which dependency is unhappy?
kubectl -n rulemind logs deploy/rulemind-api --tail=100
```

`/ready` returns a per-dependency verdict. Read it before restarting anything: a restart fixes
a wedged process and does nothing at all for a database that is down, while costing you the
in-process caches and any warm sandbox pool.

**If `/ready` reports the sandbox degraded**, read the `mode` field carefully. `mode:
inline_fallback` means the process pool is unavailable and `SANDBOX_FALLBACK_MODE=inline` is
running **untrusted customer Python inside the API process**, with no RLIMIT and no process
boundary. That is a materially different security posture, not a performance note. Treat it as
an incident in its own right, not as a degraded-but-fine state.

---

## Decisions stopped

`RuleMindDecisionsStopped` · critical · no decisions in 10 minutes, where there were some an
hour ago

**A decisioning engine that has stopped deciding is an outage even when every health check is
green.** Work outward:

1. **Is it us or them?** `sum(rate(rulemind_decisions_total[10m]))` is zero — check whether the
   ingress/load balancer is still sending traffic at all before touching the service.
2. **Are requests arriving and failing?** Look for 4xx/5xx in the API logs. A wave of 401s means
   a rotated key; a wave of 422s means a caller changed payload shape.
3. **Are they arriving and hanging?** Check the evaluation budget. If `/decide` is returning
   504 `evaluation_budget_exceeded`, go to [Latency](#latency) — the decisions are being
   attempted and abandoned, which is a different failure from not arriving.
4. **Quota.** `_enforce_quota()` can reject before any policy runs. Check the workspace's usage
   limits.

---

## Latency

`RuleMindDecisionLatencyHigh` (p95 > 500ms) · `RuleMindDecisionLatencyCritical` (p95 > 2s)

**Split by `source` first — it is the fastest way to a correct diagnosis:**

| What you see | What it means |
|---|---|
| `api_fast` vanishes, `api` rises | Traffic fell off the fast path. Something made policies ineligible — most often a *running experiment* (the fast path applies no experiment overrides) or a connector with decision-time fetch enabled. Not an engine slowdown. |
| both rise together | A real slowdown. Continue below. |
| only `api` rises | Executor-only work: connectors, actions, review gates, sub-workflows. |

For a real slowdown, in order of likelihood:

1. **The sandbox pool is saturated.** Every variable runs in a worker with its own timeout, so a
   saturated pool serialises them. `SANDBOX_POOL_SIZE` is the knob. `/ready` reports pool mode.
2. **A connector is slow.** Decision-time fetches are bounded per connector, but a bureau at
   3s × several sources is 10s of wall clock. Check the circuit breaker state.
3. **The database.** The executor is documented at "8+N queries" per decision. A missing index
   or a bloated `decisions` table shows up here first.
4. **Variables doing too much.** `COMPUTE_ONLY_USED_VARS=1` computes only what the policy can
   actually reach — measured at **−92.8%** on the serving path and **−76.0%** on the executor
   (`docs/PERFORMANCE.md`). It is correct and proven, and it is **off by default** because it
   shrinks the recorded `computed_variables` from the whole catalog to what the policy read.
   That is a change to your audit surface — decide deliberately, not during an incident.

The budget itself is `engine_config.evaluation_budget_ms`. When it fires the caller gets a 504
with `error: evaluation_budget_exceeded`, which is a *bound working as designed*, not a crash.

---

## The outcome mix moved

`RuleMindOutcomeMixShifted` · warning · approval rate moved >20 points vs the same window
yesterday

**This is the alert that matters most for this product, and the one a generic stack does not
have.** Every request can return 200 in 15ms while the engine approves everything it should
reject. Latency and error-rate alerts are all silent for that.

It is a tripwire, not a precision instrument. Real causes, most common first:

1. **A policy was promoted.** Check `/api/v1/audit/promotions` — it records the entity, both
   statuses, the authenticated actor and a snapshot for diffing. This is the first thing to look
   at, and usually the answer.
2. **A data source changed under you.** A bureau returning nulls makes variables `None`, and
   `None` compares False against every operator — so a typo'd or missing field does not fail a
   rule, it **re-phrases** it. Which way it breaks depends on how the author worded the gate.
   Check `variable_errors` on recent decisions and the `error_events` stream.
3. **A connector is failing open.** Check circuit-breaker state per connector.
4. **Genuine population shift.** Check PSI drift (`app/drift.py`) before assuming a bug — a
   marketing campaign that changes who applies moves this legitimately.

Cross-check with `/api/v1/slo/status`, which computes the same idea per workspace against that
tenant's own configured min/max approval bounds.

**Do not "fix" this by widening the alert.** If the shift is legitimate, move the workspace's
SLO bounds deliberately and write down why.

---

## Bundle sync

`RuleMindBundleSyncFailing` · warning

Devices that cannot sync **keep deciding offline against the bundle they already hold**. So this
never appears as an error rate — it appears as devices quietly running an old policy, which is
the worst shape of failure for an audit.

```bash
# Decisions from on-device carry the bundle they ran against; API decisions do not.
# That field is how you establish afterwards which decisions ran stale.
```

Check the `/sdk/v1/bundle` endpoint and the sync error events. After the sync is restored,
identify affected decisions by `bundle_hash` and decide whether they need re-running.

---

## Backup and restore

See [backup-restore.md](backup-restore.md). The procedure is **automated and rehearsed** —
`apps/python-executor/scripts/backup_verify.py` dumps, restores into a throwaway database, and
verifies the restore:

- every table present in the source is present in the restore;
- row counts match per table;
- **the audit hash chain still verifies in the restored copy.**

That last check is the one worth the most: a dump that silently drops or reorders audit rows
produces a restore that looks healthy by row count and whose tamper-evidence is broken — and a
compliance story that only holds on the primary is not a compliance story.

**`pg_dump`/`pg_restore` are NOT in the API image.** Checked, not assumed — the deployed
application container cannot run this procedure. Run it from a container that has the Postgres
client tools (the `db` container does: `pg_dump (PostgreSQL) 16.15`), from a dedicated backup
job, or from an admin host with `postgresql-client` installed. A backup procedure you first
discover you cannot run during an incident is the failure this whole section exists to prevent,
so it is written down here rather than left to be found.

Drills run 2026-09-15, both lanes:

| Lane | Result |
|---|---|
| SQLite | 41 tables / 165 rows, chain verified, clean |
| **PostgreSQL 16** | 103 MB dump · **42 tables → 42** · decisions **11,801 → 11,801** · audit_events 1 → 1 · **audit chain verifies in the restored copy** |

The Postgres drill is the one that matters — it is the path production actually takes, and it
exercises `pg_dump -Fc` / `pg_restore` rather than a file copy.

---

## What this runbook does not cover, honestly

- **Alertmanager is deployed and wired, but has NO REAL DESTINATION.** It runs in the
  `observability` profile and Prometheus delivers to it — verified, not assumed:
  `curl -s localhost:9090/api/v1/alertmanagers` lists one **active** endpoint. The default
  receiver is an explicit black hole, because a committed Slack webhook is a leaked credential.
  **Uncomment one receiver in `observability/alertmanager.yml`, add the real credential, and
  change `receiver: "null"` in both places** — the top-level route *and* the critical sub-route.
  Until then alerts group, inhibit and resolve correctly, and go nowhere.
- **No on-call rotation.** The rules say what is worth waking someone for; who gets woken, and
  in what order, is not defined anywhere.
- **No incident severity ladder or comms template.**
- **No load-shedding playbook** beyond the per-workspace quota (1000 rpm, shedding with a 429 and
  a retry hint in ~44 ms — verified under load).
- **Thresholds are calibrated against a laptop, not your traffic.** Derivation is at the top of
  `observability/alerts.yml`, including the finding that ramp latency and steady-state latency at
  the same concurrency differ by more than 2x. Re-derive after the first month.

### Verifying the alert pipeline actually works

Component checks pass individually while the chain is broken — that happened here:
`alertmanager.yml` was valid per `amtool`, the rules were valid per `promtool`, and nothing was
connected, so alerts reached nobody. Check the **chain**, not the parts:

```bash
promtool check rules observability/alerts.yml         # rules parse
curl -s localhost:9090/api/v1/alertmanagers           # Prometheus -> Alertmanager: ACTIVE?
curl -s localhost:9090/api/v1/rules | grep -o '"state":"[a-z]*"' | sort -u   # inactive/pending/firing
curl -s localhost:9093/api/v2/alerts                  # did anything actually ARRIVE?
```

The last one is the only check that proves delivery. Allow a few seconds after a Prometheus
restart — the alertmanagers endpoint reads empty at first, which looks exactly like a broken
config.
