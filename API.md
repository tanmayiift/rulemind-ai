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

**What the spec types.** The responses an integration depends on — decide and batch decide,
decision records, reason codes, replay, executions, OAuth2 tokens, the `/sdk/v1` endpoints, data
subjects and residency, liveness and readiness — are typed schemas, checked against real
responses on every decision path by `tests/test_api_contract.py`. Every operation documents the
error envelope (`4XX`/`5XX`) and the credentials it needs, and a test sends each one without
credentials to prove the document and the middleware agree. The authoring surface still
documents its responses as open objects. Response objects may gain fields without a version
bump: ignore fields you do not know.

The JS SDK's response types are generated from this spec (`packages/sdk-js/src/generated/`), so
a client generated with `openapi-typescript` or `openapi-generator` gets the same types:

```bash
npx openapi-typescript openapi.json -o rulemind-api.ts
```

> **Until 2026-09-24 the spec described 279 operations and zero typed responses**, documented
> every 422 as FastAPI's `{detail: [...]}` (the app has sent `{error, detail}` since the error
> envelope landed), and declared no security schemes at all.

## Authentication

- `AUTH_MODE=apikey` (the default): send `x-api-key: rm_live_...`. Keys are minted by
  `auth.generate_api_key` and always carry the `rm_live_` prefix. **A key sent as
  `Authorization: Bearer rm_live_...` is rejected with 401** — a bearer value is read as a
  session or OAuth2 access token, never as a key. (This page said otherwise until 2026-09-24.)
- `Authorization: Bearer <access token>` from the OAuth2 token endpoint below, for clients
  built on a standard OAuth2 library.
- `AUTH_MODE=jwt`: send a bearer JWT; verification uses the configured issuer/JWKS.
- `AUTH_MODE=none`: every request executes as a local admin profile. **Leaving `AUTH_MODE`
  unset is not the same as `none`** — `app/middleware.py` tests for the literal string, so
  unset leaves authentication ON. That is the safe direction, and it is stated because the
  README implied otherwise for a long time.

### OAuth2 client credentials

`POST /api/v1/oauth/token` (RFC 6749 §4.4) exchanges an API key for a short-lived bearer token,
so the key itself is sent once rather than on every request.

| field | value |
|---|---|
| `grant_type` | `client_credentials` — anything else is `400 unsupported_grant_type` |
| `client_id` | the key's id (`kid`, listed by `GET /api/v1/access/keys`) |
| `client_secret` | the key |

Client authentication by HTTP Basic (`client_secret_basic`, preferred) or in the body
(`client_secret_post`); the body may be form-encoded or JSON.

```bash
curl -s -u "$KID:$RULEMIND_API_KEY" -d grant_type=client_credentials \
  https://your-host/api/v1/oauth/token
# {"access_token": "eyJ...", "token_type": "Bearer", "expires_in": 3600}
```

- The token has **exactly the key's tenant and role**, and is re-resolved to the key on every
  request: revoke the key and its tokens stop working; change the key's role and its tokens
  carry the new role. Both take effect immediately in the process that made the change and
  within `API_KEY_CACHE_TTL` (default 300 s) in every other worker and replica — the cache is
  per process, so this is the same window in which revoking the key itself takes effect.
- Lifetime is `OAUTH2_ACCESS_TOKEN_TTL_SECONDS` (default 3600, clamped to 60–86400). There is no
  refresh token: a client-credentials client just requests a new one.
- A secret presented under a `client_id` that is not its own is `401 invalid_client`, with
  `WWW-Authenticate`. Error bodies carry both the RFC fields (`error`, `error_description`) and
  this API's `detail`, so either kind of client can parse them.
- The endpoint is rate-limited per IP on the same budget as login, since it accepts a guess.
- Tokens are signed with a key derived from `RULEMIND_ADMIN_JWT_SECRET` under a fixed label, so
  a platform-admin JWT is not accepted as an access token, nor the reverse. Rotating that
  secret invalidates every outstanding access token.

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

A request the API cannot parse at all — a missing required field, a wrong type — is a `422`
with `"error": "validation_error"` and `detail` as a list of `{loc, msg, type}`.

`504` responses from the evaluation budget additionally carry `budget_ms`, `elapsed_ms` and
`stage`. `429` responses carry `RateLimit-*` and `Retry-After` headers.

## Paging collections

Every endpoint that returns a list can be paged, the same way:

- `?limit=N` — page size, 1–1000. `?offset=M` — items to skip.
- `X-Total-Count` — the size of the whole collection, on every paged response.
- `Link` — RFC 8288 `rel="next"` / `rel="prev"` URLs; follow them rather than doing arithmetic.

**Omit both parameters and you get the whole collection as a plain array, exactly as before** —
paging is opt-in, so existing integrations are unaffected. An out-of-range or non-integer value is
a `422` in the error envelope above, never a silent clamp.

This bounds what is *sent*. For the authoring collections (rules, variables, policies — hundreds
per tenant) the collection is still read whole server-side and sliced; the ones that genuinely
grow without bound — `/api/v1/audit`, `/api/v1/audit/decisions`, `/api/v1/audit/errors`,
`/api/v1/executions` — page in the database.

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
  "execution_id": null,
  "status": "completed",
  "policy_version": 3,
  "outcome": "reject",
  "score": 400,
  "variables": { "bureau_score": 720 },
  "variable_errors": {},
  "rule_results": [
    { "rule_id": "rule_bureau_floor", "ruleId": "rule_bureau_floor", "passed": false,
      "outcome": "reject", "logic": "AND", "groupResults": [],
      "conditions": [ { "variable_id": "bureau_score", "variable_name": "Bureau score",
                        "source_id": "bureau", "operator": ">=", "threshold": 750,
                        "value": 720, "passed": false, "group": "AND" } ] } ],
  "scorecard_result": null,
  "trace": [],
  "latency_ms": 15.2 }
```

The full schema is `DecideResponse` in the spec.

`decision_id` names the stored `Decision` row — use it for the reason codes and the replay
below. `policy_version` is the lineage: policies are mutated in place, so `policy_id` alone
cannot say which version decided. Both fields are returned on **either** decision path; a
`FAST_DECIDE` change must never alter this shape.

**A decision that pauses at a review gate** returns `"status": "paused"`, `decision_id: null` —
no decision exists until a reviewer completes it — and an `execution_id` to follow:
`GET /api/v1/executions/{execution_id}`. `execution_id` is null whenever the decision completed
in the call.

> **`rule_results` changed shape on 2026-09-24.** On the executor path (the default) it used to
> be a filter of `trace` — items shaped `{step, result: {passed, conditions, ...}, duration_ms,
> error}` — while the `FAST_DECIDE` path, the stored decision and the reason codes all used the
> shape above. It is the shape above on every path now. If you read `rule_results[i].result.passed`
> or `.step.id`, read `rule_results[i].passed` and `rule_results[i].rule_id` instead; the step
> wrappers are still in `trace`.

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

## Hosted models and evaluation

- `POST /api/v1/models` with `{name, model_base64, input_schema?}` hosts a model. The format is
  detected from the file's bytes, never from a declared type: **ONNX**, **PMML** (`Scorecard`,
  `RegressionModel`, `TreeModel`; anything else is `422` naming the element), **XGBoost JSON**,
  **LightGBM** model text, or a **pickle**. The response carries `model_format`. With
  `MODEL_ALLOW_PICKLE=0`, pickles are refused at upload and at scoring.
- `POST /api/v1/models/{id}/predict` with `{input_data, features?, allow_missing?}`. Named-feature
  models (XGBoost/LightGBM boosters that carry names, PMML) are scored **by name**, whatever order
  the caller lists features in. A missing or non-numeric feature is a `422`.
- `POST /api/v1/evaluations/from-model/{id}` scores every row in one batch — one model load per
  2,000 rows in the isolated worker, not one per row — then computes Gini/AUC/KS and the gate.
  The dashboard accepts the dataset as a `.csv`, `.xlsx`, `.json` or `.jsonl` upload.

## Release, impact and history

The Policies screen's **Release & impact** panel is built on these; each is also usable directly.

- `POST /api/v1/policies/{id}/whatif` with `{kpis, bundle_version?, sample?, full?}` replays this
  policy's recorded decisions through a compiled bundle and returns each KPI as recorded vs as it
  would be now. KPI types: `outcome_rate`, `outcome_count`, `count`, `avg`, `sum`, `min`, `max`,
  `count_where`. `GET /api/v1/bundles/versions` lists the retained bundle versions.
- `POST|GET|DELETE /api/v1/policies/{id}/shadow` dark-launches a candidate policy on this one's live
  traffic and reports how often it would have decided differently. The live decision never changes.
- `GET /api/v1/policies/{id}/releases` and `POST /api/v1/policies/{id}/rollback` with
  `{promotion_id, reason}` restore a release's snapshot, recorded as a new rollback entry.
  **The ledger records the authenticated caller.** An `actor` in the body is honoured only when
  there is no authenticated identity (`AUTH_MODE=none`); it used to be recorded as sent.
- `POST /api/v1/decisions/{id}/replay?bundleVersion=N` re-decides one recorded decision under a
  retained bundle version (latest when omitted): `{original_outcome, replayed_outcome, changed}`.
- `GET /api/v1/history/{entity_type}/{entity_id}` — versions of a `policy`, `rule`, `scorecard`,
  `decision_table`, `variable` or `connector`, newest first. Connector snapshots never carry
  secrets. History outlives the entity, so a deleted policy's versions stay readable.

Promotion from the dashboard asks for a reason, which is what the promotion records.

## Human review

- `GET /api/v1/reviews` — the queue. `GET /api/v1/reviews/stats`,
  `GET /api/v1/reviews/{task_id}`.
- `POST /api/v1/reviews/{task_id}/decide` — submit the human decision and resume the paused
  execution. `POST /api/v1/reviews/{task_id}/escalate`.

> Previously documented here as `GET /api/v1/approvals` and
> `POST /api/v1/approvals/:id/approve` / `:id/reject`. No such routes exist.

## Signing up

`POST /api/v1/onboarding/signup` (public, rate-limited per IP; off with
`RULEMIND_SELF_SERVE_SIGNUP=0`) with `{company, contact_email, name?, password?, use_case?}` creates
a workspace, loads the sample inventory and returns its **development API key once**. With a
`password` (12+ characters) it also creates the workspace's first member, its **owner**, and signs
them in (session cookie plus `session` in the body). The rest of setup is
`GET /api/v1/onboarding/status`, `POST /api/v1/onboarding/verify` (needs one decision),
`POST /api/v1/onboarding/ai`, and `POST /api/v1/onboarding/request-prod`, which issues the
production key once, after verification. The dashboard walks through all of it at `/signup` and
`/onboarding`.

Every response carries CORS headers, including the 401/403/429 the auth layer returns before any
route runs, so a browser can tell "not signed in" and "rate limited" from "the API is down".

## Data residency and data subjects

- `GET /api/v1/data-residency` — the install's declared region (`RULEMIND_DATA_REGION`), the
  database engine and host (never its credentials), encryption at rest, and every configured egress
  destination: connector hosts and token endpoints, webhook receivers, AI providers, archive sink,
  Kafka, tracing.
- `GET /api/v1/data-subjects/{subject_id}` — every decision about one subject, in full. Admin only.
- `POST /api/v1/data-subjects/{subject_id}/erase` with `{reason}` — redacts that subject's personal
  data in one transaction, keeping each decision's outcome, policy version and adverse-action reason
  codes. The applicant's own values are removed, including from the reason text. The receipt lists
  what was redacted and what it could not reach. Admin only; audited under a pseudonym.
  After erasure `GET /api/v1/decisions/{id}/reason-codes` returns the kept codes.

See [`docs/DATA-RESIDENCY.md`](docs/DATA-RESIDENCY.md).

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

## Connector templates

Prebuilt decision-time connectors for named providers — Equifax (bureau), FinBox BankConnect (bank
statements) and Setu PAN verification (KYC). The full contract for each — inputs, request, mapped
fields, error taxonomy, and what was and was not verified — is generated into
[`docs/connectors/TEMPLATES.md`](docs/connectors/TEMPLATES.md).

- `GET /api/v1/connector-templates` (`?category=bureau|bank_statement|kyc`) and
  `GET /api/v1/connector-templates/{template_id}`.
- `POST /api/v1/connector-templates/{template_id}/install` with
  `{connector_id?, environment?, base_url?, settings, secrets, install_variables?}` creates the
  connector and its starter variables (in `dev`). Every problem is reported at once as
  `422 invalid_template_install` with a `problems` list; an id that already exists is
  `409 already_exists` rather than a silently suffixed copy. Needs the author capability.

A fetch failure from a template carries a stable code in the decision's source-fetch trace
(`setu_pan_not_found`, `finbox_statement_processing`, `equifax_no_hit`, …) and, by default, sends
the decision to review.

## Outbound webhooks

- `GET`/`POST /api/v1/webhook-subscriptions`, `GET`/`PATCH`/`DELETE /api/v1/webhook-subscriptions/{subscription_id}`,
  `POST /api/v1/webhook-subscriptions/{subscription_id}/test`, `GET /api/v1/webhook-subscriptions/event-types`
- `GET /api/v1/webhook-subscriptions/{subscription_id}/deliveries` — every delivery, newest first.
  `?status=dead` is the dead-letter queue.
- `POST /api/v1/webhook-deliveries/{delivery_id}/redeliver` — replay one.

Delivery is **durable and at-least-once**: an event is recorded before it is sent, retried with
backoff for about nine hours, then dead-lettered where it can be listed and replayed. Each request
carries its delivery id — dedupe on it, because a process can die after your endpoint accepted a
delivery and before it was recorded, and the event will then be sent again.

## Operational

- `GET /health` — liveness. Never consults the database; a liveness probe that did would
  restart every pod on a Postgres blip.
- `GET /health/ready`, `GET /ready` — readiness, including the database and Redis.
- `GET /metrics` — Prometheus. Aggregated across uvicorn workers only when
  `PROMETHEUS_MULTIPROC_DIR` is set (the chart and compose both set it). Unauthenticated, and
  it carries per-tenant labels — put a NetworkPolicy in front of it.
