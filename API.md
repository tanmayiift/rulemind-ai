# API Reference

**This file is the human introduction. The complete contract is
`GET /api/v1/openapi.json`, authenticated with your API key.**

That route exists because `/openapi.json`, `/docs` and `/redoc` are all disabled when
`RULEMIND_ENV=production` (`app/main.py:_docs_enabled` — they were served *unauthenticated*,
handing the whole endpoint map to anyone who asked) and the Helm chart sets exactly that. So a
production install published no machine-readable contract at all, and this file was the only
one shipped.

> **What this file used to say.** It documented 17 of 278 endpoints and 13 of the 17 did not
> exist: `GET /api/v1/api-keys` and `POST /api/v1/api-keys` (really `/api/v1/access/keys`),
> `GET /api/v1/approvals` and `POST /api/v1/approvals/:id/approve` and
> `POST /api/v1/approvals/:id/reject` (really `/api/v1/reviews` and
> `/api/v1/reviews/{task_id}/decide`), `POST /api/v1/rules/validate` and
> `POST /api/v1/rules/:id/evaluate` (really `POST /api/v1/rules/{rule_id}/test`),
> `GET /api/v1/dashboard/summary` (never existed), and the collections `/api/v1/segments`,
> `/api/v1/ab-tests`, `/api/v1/exports` and `/api/v1/outcome-imports` (none exist). It also
> pointed at `/docs/json` for the spec, which has never been the location. Listed here so
> someone who already integrated against the old document can find what they actually wanted.

Corrected 2026-09-24, and `scripts/check_api_docs.py` now fails the build if anything here
names an endpoint that does not exist — so this document can be incomplete, which is fine, but
not wrong.

```bash
curl -s https://your-host/api/v1/openapi.json -H "x-api-key: $RULEMIND_API_KEY" > openapi.json
```

**Known limitation of that spec:** every operation is annotated `-> Dict[str, Any]`, so it
describes **279 operations and zero typed 2xx response schemas**. Paths, methods and request
bodies are accurate; responses are not described, which is why typed client generation does not
work yet and all five SDKs are hand-written.

## Authentication

- `AUTH_MODE=apikey` (the default): send `x-api-key: rm_live_...` or
  `Authorization: Bearer rm_live_...`. Keys are minted by `auth.generate_api_key` and always
  carry the `rm_live_` prefix.
- `AUTH_MODE=jwt`: send a bearer JWT; verification uses the configured issuer/JWKS.
- `AUTH_MODE=none`: every request executes as a local admin profile. **Leaving `AUTH_MODE`
  unset is not the same as `none`** — `app/middleware.py` tests for the literal string, so
  unset leaves authentication ON. That is the safe direction, and it is stated because the
  README implied otherwise for a long time.

## Errors

Every error response carries a machine-readable `error` and a human or structured `detail`:

```json
{ "error": "not_found", "detail": "policy 'policy_x' not found" }
```

`error` is **always present and always a string** — that is the field to switch on. `detail`
is context whose shape depends on the error: a sentence for most, a structured object for the
ones where that is the useful thing, e.g. input validation:

```json
{
  "error": "invalid_input",
  "detail": { "error": "invalid_input", "issues": [
    { "path": "applicant.age", "message": "expected number, got string" }
  ] }
}
```

`504` responses from the evaluation budget additionally carry `budget_ms`, `elapsed_ms` and
`stage`. `429` responses carry `RateLimit-*` and `Retry-After` headers.

## Deciding

### `POST /api/v1/decide`

```json
{ "policyId": "policy_pl_underwriting",
  "payload": { "bureau_score": 720, "income": 50000 },
  "userId": "customer-123" }
```

Response:

```json
{ "policy_id": "policy_pl_underwriting",
  "decision_id": "174970a3-c47d-4d8e-9849-032c511bc4dd",
  "policy_version": 3,
  "outcome": "reject",
  "score": 400,
  "variables": { "bureau_score": 720 },
  "variable_errors": {},
  "rule_results": [],
  "scorecard_result": null,
  "trace": [],
  "latency_ms": 15.2 }
```

`decision_id` names the stored `Decision` row — use it for the reason codes and the replay
below. `policy_version` is the lineage: policies are mutated in place, so `policy_id` alone
cannot say which version decided. Both fields are returned on **either** decision path; a
`FAST_DECIDE` change must never alter this shape.

`payload` is either flat (`{"bureau_score": 720}`, matched against every source that declares
the field) or source-keyed (`{"bureau": {"bureau_score": 720}}`, authoritative and complete for
that source — omitted fields are absent, not defaulted). Send the source-keyed form for an
exact, reproducible decision.

- `POST /api/v1/decide/batch` — many payloads in one call.
- `POST /api/v1/decide/batch/jsonl` — streaming JSONL, for backtests.

### Explaining a decision

- `GET /api/v1/decisions/{decision_id}/reason-codes` — the deterministic adverse-action codes,
  ranked by relative miss magnitude, derived from the conditions that actually failed.
- `POST /api/v1/decisions/{decision_id}/replay` — re-run a stored decision, optionally against
  a different `bundleVersion`, and report whether the outcome changed.
- `GET /api/v1/decisions/stream` — server-sent events for the live decision feed.

## Audit

- `GET /api/v1/audit` — the authoring change trail: who changed which rule/variable/scorecard/
  policy, when. Paginated (`limit`, `offset`), filterable by `event_type`, `entity_type`,
  `entity_id`.
- `GET /api/v1/audit/verify` — verify the tamper-evident hash chain over that trail. Returns
  `ok`, `verified`, `pre_chain`, `breaks` and the chain `head`. A chain proves *partial*
  tampering only; export `head` to external storage to make it an attestation.
- `GET /api/v1/audit/decisions` — decision history. `limit`/`offset`, plus `client_id`,
  `user_id`, `policy_id`, `outcome`, and `created_from`/`created_to` (ISO-8601, half-open
  `[from, to)` so adjacent ranges tile without double-counting).
- `GET /api/v1/audit/decisions/{decision_id}`, `GET /api/v1/audit/decisions/count`
- `GET /api/v1/audit/promotions` — the maker/checker promotion ledger.
- `GET /api/v1/audit/errors` — operational error events.

## Authoring

These collections follow the same CRUD contract —
`GET /api/v1/<resource>`, `POST /api/v1/<resource>`, `GET /api/v1/<resource>/{id}`,
`PUT /api/v1/<resource>/{id}`, `DELETE /api/v1/<resource>/{id}`:

- `/api/v1/policies`
- `/api/v1/rules`
- `/api/v1/variables`
- `/api/v1/scorecards`
- `/api/v1/decision-tables`
- `/api/v1/connectors`

Beyond CRUD:

- `POST /api/v1/rules/{rule_id}/test` — evaluate one rule against a sample payload and get the
  condition-by-condition result.
> Previously documented here as `POST /api/v1/rules/validate` and
> `POST /api/v1/rules/:id/evaluate`. Neither has ever existed.
- `POST /api/v1/rules/{rule_id}/promote`, `POST /api/v1/policies/{policy_id}/lifecycle` —
  `dev → uat → prod`.
- `GET /api/v1/policies/{policy_id}/diff`, `/input-schema`, `/releases`, `/shadow`
- `POST /api/v1/policies/{policy_id}/analyze-mece` — overlap and gap analysis.
- `POST /api/v1/policies/{policy_id}/backtest`, `POST /api/v1/policies/{policy_id}/execute`
- `GET /api/v1/variables/graph`, `GET /api/v1/variables/{variable_id}/history`

## Human review

- `GET /api/v1/reviews` — the queue. `GET /api/v1/reviews/stats`,
  `GET /api/v1/reviews/{task_id}`.
- `POST /api/v1/reviews/{task_id}/decide` — submit the human decision and resume the paused
  execution. `POST /api/v1/reviews/{task_id}/escalate`.

> Previously documented here as `GET /api/v1/approvals` and
> `POST /api/v1/approvals/:id/approve` / `:id/reject`. No such routes exist.

## Access and settings

- `GET /api/v1/access/keys`, `POST /api/v1/access/keys`,
  `DELETE /api/v1/access/keys/{kid}`, `PATCH /api/v1/access/keys/{kid}/role` — API keys.
> Previously documented here as `GET /api/v1/api-keys` and `POST /api/v1/api-keys`, which do
> not exist.
- `GET /api/v1/access/me`, `/members`, `/roles`, `/sso`
- `GET`/`PUT /api/v1/settings` — per-tenant `engine_config`, including `timeout_ms`,
  `memory_mb`, `evaluation_budget_ms` and `compute_only_used_vars`. See `.env.example` for
  what each one does.
- `GET /api/v1/settings/data-protection`, `/governance`, `/slo`

## Operational

- `GET /health` — liveness. Never consults the database; a liveness probe that did would
  restart every pod on a Postgres blip.
- `GET /health/ready`, `GET /ready` — readiness, including the database and Redis.
- `GET /metrics` — Prometheus. Aggregated across uvicorn workers only when
  `PROMETHEUS_MULTIPROC_DIR` is set (the chart and compose both set it). Unauthenticated, and
  it carries per-tenant labels — put a NetworkPolicy in front of it.
