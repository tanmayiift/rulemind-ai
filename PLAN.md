# RuleMind — Round 6 plan: object-centric platform, lighter and redesigned

## Context

The repo is now public. The user wants RuleMind to become a general, self-hosted platform:
model any domain as objects and links (Vehicle → Route → Stop → Shipment, or Applicant →
Application → Loan), then build a complete operational app on top of it. That app needs
decisions, lifecycles, automated next steps, communications through any provider, agents, an
MCP server, maps, telematics, data sync, and internal, portal and mobile apps. The UI must be
modern and easy. The whole thing must be **lightweight, cheap to host and scalable**, with
dead code removed. Logistics and lending are the proving templates. Nothing may be specific to
either.

User decisions (2026-10-14/15):
- **Scope:** the full platform, all at once.
- **Agentic:** all four (builder copilot, AI steps in processes, autonomous agents, MCP server).
- **Integrations:**
  - maps and routing;
  - data sources and sync;
  - telematics and IoT;
  - communications through *any* provider the client links, fired by events and object decisions.
- **Users:** internal, external portal, and mobile.
- **Processes:** Temporal.
- **CI:** GitHub-hosted, on every PR.
- **UI:** rethink the whole UI and its sections, and write `DESIGN.md`.
- **Dead code:** remove it from the repo and from GitHub.
- **Architecture and engineering calls** are delegated to me, judged on "best engineering and
  product completeness". My calls are listed under "Decisions taken".

## Execution rules (user's working style)

**How I work:**
- Work end to end. Do not stop for confirmation between steps.
- Pause only for a genuine blocker: a missing credential, an ambiguous requirement, or a
  failing test I cannot fix.
- After each step: tick its checkbox in `PLAN.md` (rewritten as the Round 6 checklist in D8),
  commit, and continue immediately.

**How changes land:**
- Each step is its own Conventional-Commit PR with a scope, stacked as `r6/NN-slug` on top of
  #261 (`round5/65`).
- **Never merge.** Never touch the open `round5/*` or Dependabot heads.
- Commit trailer: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`. PR bodies end with
  the Claude Code line.

**Hard limits:**
- No secrets in git.
- Never echo `.env`.
- Repo and account **settings are owner actions**, listed under "Owner actions". I do not change
  them.

**Per-PR quality bar:**
- Typed OpenAPI contract regenerated (`pnpm --filter @rulemind/sdk-js gen:types`) in the same PR.
- New env vars go in both `.env.example` and `helm/rulemind/values.yaml`
  (`check_config_surface.py`).
- New routes are classified in `tests/test_tenant_isolation_matrix.py`.
- Operator-semantics changes land in every engine, with parity tests.
- Bug-fix tests are mutation-checked. After restoring a mutation, `touch` the file so stale `.pyc`
  doesn't hide the restore.
- UI PRs are verified in the browser preview (open the page, don't just read it).

## Decisions taken (engineering calls delegated to me)

1. **Object-centric, event-driven architecture**, detailed below.
2. **Delete `packages/rulemind-core-rs` and `packages/rulemind-decide-service`.**
   - Python no longer imports them (`fast_decide.py:38`) and nothing deploys them.
   - Removing them drops the Rust toolchain and 35 tests.
   - **The parity contract becomes four engines: Python, TypeScript, Kotlin, Dart.**
3. **Keep the TypeScript `rule-engine` and give it a real consumer.** Generated web and portal
   forms evaluate show/hide/validation rules client-side, with the same semantics as the server.
   Kotlin and Dart already do this on mobile.
4. **Delete the collab stack.** It is an in-memory demo and is not tied to any policy. The new
   canvases instead get optimistic locking (`If-Match` → 409) plus a presence indicator.
5. **Delete the Kafka decision outbox, its consumer and the WAL.**
   - The outbox grows without bound today, because no consumer drains it.
   - Webhook subscriptions become the way events leave the system.
   - A Kafka relay is out of scope this round.
6. **Postgres is the event bus.**
   - A transactional outbox plus a `SKIP LOCKED` dispatcher.
   - **Redis becomes optional** (`scale` profile). It is required only by telematics
     (streams and device state).
   - Live fan-out runs in-process when there is one replica, and over Redis pub/sub when scaled.
   - Rate limits use Redis when it is present, and an in-process limiter otherwise.
7. **One backend image, roles chosen by a flag.**
   - `worker --roles=scheduler,dispatcher,effects,comms,temporal,telematics`. The lite install
     runs every role in one process; Helm splits the roles into separate Deployments and scales
     them with KEDA.
   - **Minimal install: 5 containers** (`db`, `temporal`, `api`, `worker`, `web`) plus two one-shot
     jobs (`migrate`, `temporal-schema`).
   - Optional profiles: `scale` (redis, split roles), `maps` (osrm, vroom, nominatim),
     `telematics` (mosquitto, redis), `observability`, `tls` (caddy).
8. **Temporal runs as one container on the existing Postgres server.**
   - Its own `temporal` and `temporal_visibility` databases. Advanced visibility on Postgres
     (server ≥1.20), so no Elasticsearch.
   - Official server image plus `temporal-sql-tool`. Not `auto-setup`, which is deprecated after
     1.29.
   - One shared namespace per deployment, with tenant IDs in workflow IDs and search attributes.
   - An AES-GCM payload codec, because workflow histories will hold PII.
   - `RULEMIND_TEMPORAL_MODE=off|external|dev` degrades cleanly: with `off`, processes and agents
     are hidden.
9. **Per-object SLA timers live in Postgres (`state_timers`), not in Temporal.** A workflow per
   object does not scale to 10M objects. Temporal is used only for processes, agents, sync runs,
   durable pauses and tenant schedules.
10. **Files:** a local volume by default (`RULEMIND_FILES_DIR`). Any S3-compatible store is
    optional. Not tied to MinIO, which changed its community distribution in 2025.
11. **Maps:**
    - The `maps` profile is optional.
    - Without it, ETAs fall back to haversine distance.
    - The basemap is MapLibre reading a static PMTiles file, so no tile server is needed.
    - Geo search uses H3 cells plus shapely, so PostGIS is not required.
12. **The design system lives inside `apps/web/src/ui/`**, not in a separate package. The portal
    and generated apps are route groups of the same web app.
13. **No self-hosted CI remains.**
    - Every workflow moves to GitHub-hosted runners: PR gate, CodeQL, audits, and multi-arch
      release images.
    - `scripts/check_workflow_triggers.py` fails the build on any `self-hosted` job that is not
      dispatch-only, on any `pull_request_target`, and on any action not pinned by SHA.

## Target architecture

### Components

```
web   Next 15: (console) | (apps) runtime | (portal)    — one app, route groups
api   FastAPI: existing routers + definitions, ontology, objects, actions, triggers,
      processes, tasks, comms, agents, mcp, geo, telemetry, sync, apps, portal, packages
worker  roles: scheduler (APScheduler system sweeps, lease leader) · dispatcher · effects ·
        comms · temporal (queues rm-wf/rm-act/rm-io/rm-llm) · telematics
infra   postgres16 (rulemind, temporal, temporal_visibility) · temporal · [profiles]
```

**Non-negotiable rules for new code:**
1. **Typed actions are the only write path.** Rules, humans, processes, agents, sync jobs and
   telematics all write through actions.
2. **Postgres is the source of truth** for records, state, events, tasks and the mirror of
   process instances. Temporal only orchestrates.
3. **No new decision operators.**
   - Geo and time logic is turned into plain scalars during hydration: each datetime gets an
     `<prop>__epoch` number, and each request carries `_meta.as_of_epoch`. A lint check bans
     `now()`.
4. **No new code in `storage.py`.**
   - New code goes into packages under `app/{definitions,ontology,events,actions,processes,
     temporal,comms,llm,agents,mcp,geo,telematics,sync,files,appbuilder,portal,packages}/`.
   - Each one takes a `Storage` and uses `SessionLocal`, so the RLS `after_begin` hook
     (`storage.py:1018`) applies automatically.
   - New models are registered in `Base.metadata` through `app/models.py`. That registration is
     what puts them under `rls.tenant_tables()` and `create_all`.

### Data model (new tables; all carry `tenant_id`)

**Definitions** (versioned and promotable):
- `definitions (kind, api_name, draft/dev/uat/prod_version)` and
  `definition_versions (spec JSON, spec_hash, sealed)`. A version is immutable once sealed.
- `definition_dependencies` records what each version uses, for closure promotion.
- Kinds: `object_type, link_type, state_machine, action_type, trigger, process, agent,
  message_template, comm_policy, app, sync_source, telemetry_rule, package`.

**Ontology:**
- `object_records`: `env, type_id, type_version, external_key, title, state, state_entered_at,
  version` (used for compare-and-swap), `properties` (JSONB on Postgres), `lat, lng, h3_r9`.
  - Indexes: by type and update time; by type and state; a unique external key; by H3 cell; a
    trigram index on title; property indexes created on demand.
- `object_links`: the `single_from` / `single_to` unique indexes enforce cardinality without races.
- Also: `object_record_grants` (portal row scope), `object_property_indexes`, `state_timers`.

**Events:**
- `object_events` is range-partitioned by month: `seq, partition_no, type, object_*, actor_*,
  causation_id, correlation_id, depth, changed, data` (PII-redacted), `dispatched_at`.
- `event_handlings`: idempotency and dead-letter queue.
- `worker_leases`: a general lease table.
- `process_subscriptions`.

**Actions, tasks and processes:**
- `action_executions`: unique on `(tenant, idempotency_key)`.
- `effect_outbox`.
- `human_tasks`: kinds `approval | form | review | assignment`.
- `process_instances`: a mirror of Temporal state, keyed by workflow id.
- Also: `callback_tokens`, `agent_runs`, `agent_messages`.

**Communications:**
- `channels`, with encrypted config.
- `contact_points`, with encrypted address plus a hash.
- `consents`, by purpose.
- `suppressions`, `messages`, `message_events`.

**Telematics, geo and files:**
- `devices`.
- `telemetry_points`: daily partitions, a BRIN index, and a per-record time index.
- Also: `telemetry_rollups_1m`, `geocode_cache`, `files`.

**Sync and portal:**
- `sync_runs`, `sync_errors`, `portal_users`, `portal_sessions`, `portal_magic_links`.

**Changes to existing tables:**
- `decisions`: add `subject_type_id`, `subject_id`, `subject_version`, `hydration_hash`.
- `variables`: add `kind ('code'|'field')` and `field_path`.
- `connectors`: add `object_type_id` and `managed`.
- `api_keys`: add `scopes`.

**Row-level security:**
- New tables are covered automatically because they carry `tenant_id`.
- `rls.install` and `rls.enforcement` filter `relkind='r'` (`rls.py:96,154`). They must also
  cover `'p'` and the child partitions **before** any partitioned table is added.
- Portal access adds `rulemind.principal_id` in `scope_transaction`, plus a RESTRICTIVE policy
  over `object_record_grants`.
- Environment scoping is a predicate in the single query builder, tested in both lanes.
- Background workers run each unit of work inside `context.tenant_scope`. Temporal activities do
  this through an interceptor, and **fail closed** if there is no tenant.

**Promotion (dev → uat → prod):**
- `POST /definitions/{kind}/{id}/promote` seals the version, moves the environment pointer, and
  writes `storage.add_promotion`.
- It reuses dual control (`storage.dual_control_enabled` / `last_promotion_actor`), extracted
  from `main.promote_entity:1123` into `app/promotion.py`.
- **Closure check:** everything a prod definition references must itself be in prod.
- **Compatibility check against prod data:**
  - additive changes pass;
  - breaking changes need a migration spec, which a Temporal `MigrationWorkflow` runs.
- Running process instances keep the definition version pinned in their input.
- The API key's `environment` is enforced on the new routes. Today it is stored but never checked.

**Deciding on an object:**
- **Bindings on the object type** (`include` link paths and SQL rollups) are hydrated by
  `app/ontology/hydrate.py` into the source-keyed payload that
  `PolicyExecutor._payloads_by_source` (`executor.py:503`) already reads.
  - One batched `ANY(:ids)` query per level.
  - Limits: depth 2 (hard cap 3), 100 linked records per link (hard cap 1000), 500 records,
    256 KB. Rollups are computed in SQL and are never truncated.
  - If the payload had to be truncated, a strict binding fails closed to review.
- **Field variables (`kind='field'`).**
  - These compile to IR for every engine and are computed in-process in
    `variable_runner.compute_variables`, with no sandbox.
  - This is how object decisions reach ≤10 ms. Today's decision is about 19.8 ms p50, 37% of it
    in the sandbox (`docs/PERFORMANCE.md:181,217`).
- **Caching:** no neighbourhood cache across requests in v1. Invalidating it correctly when linked
  records change is a correctness trap.

**Events and dispatch:**
- **The write is atomic.** A record or link write, its event, and the `action_executions` row
  commit in one transaction.
- **Claiming and ordering.**
  - 64 leased partitions.
  - Workers claim with `FOR UPDATE SKIP LOCKED ORDER BY seq` and use no cursor.
  - Wake-up is LISTEN/NOTIFY, with polling as a fallback.
  - Order per object is guaranteed by the record's version compare-and-swap.
- **Handlers.**
  - What runs: triggers, process signals, portal grants, outbound webhooks (dynamic
    `object.<type>.<event>` types in `webhook_dispatch.validate_event_types:98`), live fan-out.
  - All handlers are idempotent through `event_handlings`.
  - Event chains stop at depth 8, with a rate cap per object and trigger.

**Actions:**
- Each action runs these steps in order:
  1. validate the input;
  2. authorize it (new `OPERATE` capability and `operator` role in `rbac.py`, plus per-action
     roles);
  3. check idempotency;
  4. apply the DB effects in one transaction;
  5. write to `effect_outbox`, which the effects role drains with the
     `webhook_dispatch.drain/deliver_claimed/BACKOFF_SECONDS` design.
- **Audit:** every hour, a Merkle root of `action_executions` is anchored into the audit chain
  (`audit_chain.compute_hash`). The per-tenant sequential chain cannot absorb 1k writes/s.
- **High-impact actions** create a `human_task` and wait for it.

**Lifecycles:**
- Each object type gets a `state_machine`.
- A `transition` is a built-in action. Its guard is a binding decision or a `logic.compare`
  condition list.
- Entry and exit effects are queued.
- SLA timers live in `state_timers`.
- The UI calls this "Lifecycle" only as a tab on the object type. The existing `app/lifecycle.py`
  (authoring review stages) keeps its own meaning.

**Communications:**
- **Send path:** a `send_communication` effect:
  1. resolves the contact point through a link path;
  2. checks consent by purpose and the suppression list;
  3. applies quiet hours in the recipient's timezone (`scheduled_for`);
  4. applies frequency caps and dedupe;
  5. applies the idempotency key;
  6. sends via the comms role, with backoff and a dead-letter queue.

  Every resulting `message.*` event lands on the subject object, so triggers can escalate.
- **Adapters:** each has three methods, `send` / `parse_status_callback` / `verify_signature`.
  - **Generic HTTP adapter.** It works with *any* provider: a templated request through the
    egress guard, with response and status mapping.
  - **First-party adapters:** SMTP (`mailer.send_text_email`), Twilio SMS/WhatsApp, WhatsApp Cloud,
    FCM v1, APNs, Slack, Teams.
- **Templates:** logic-less, built on `templates.resolve_template`, with HTML escaping, locale
  formatting, WhatsApp parameter mapping and India DLT template IDs.
- **Inbound:** STOP/START messages update consent.

**Agents, MCP and the copilot:**
- `app/llm/` gateway:
  - tool calling for Anthropic, OpenAI and Gemini, plus `openai_compatible` (Ollama/vLLM);
  - internal hosts are allowed only by operator config (`RULEMIND_LLM_INTERNAL_HOSTS`);
  - URLs set by a tenant go through `egress.vet_url`.
- **Tools are generated from the ontology:** describe, query, get, decide, plus one tool per
  action type. Every tool call goes through the action engine with `actor_type=agent`.
- Agents run as Temporal workflows. Approvals are Temporal Updates.
- **MCP server:**
  - endpoint `/mcp`, using streamable HTTP and the official `mcp` SDK in stateless mode;
  - auth by API key or OAuth2 client-credentials, with scopes
    `objects:read|write, decide, actions:run:<name>|*, agents:run`;
  - publishes protected-resource metadata;
  - ships a stdio launcher.
- **Copilot:**
  - emits a *package draft* covering many definitions, shown as a diff;
  - applying it installs into dev only, never promotes;
  - the lending-only `ai.is_in_scope` vocabulary (`ai.py:32-59`) is generalised.

**Maps, telematics, sync:**
- **`app/geo/` providers.** OSRM, Valhalla, VROOM and Nominatim (self-hosted), or Google, Mapbox
  and HERE.
- **Telematics.**
  - **Ingest:** HTTP batch, plus an MQTT bridge. Points go into daily partitions, then `XADD`.
  - **Processing:** the processor handles geofences (with hysteresis), thresholds, sliding
    windows, dwell time and going offline. Only derived events go through actions.
  - **Writes and retention:** `last_location` is throttled to 10 s / 100 m. Raw points are kept
    7 days, 1-minute rollups 90 days.
- **Sync.**
  - **Sources:** HTTP pull, DB query-poll, CSV import, inbound webhooks, and a Debezium Server
    preset.
  - **Writes:** a changed-only bulk upsert:
    `ON CONFLICT … WHERE properties IS DISTINCT FROM excluded.properties`.
  - **Runs** execute as `SyncRunWorkflow`.

**Apps, portal, mobile:**
- An `app` definition holds an audience (internal, portal or mobile), navigation, pages and
  widgets: table, detail, form, kanban by state, map, timeline, metric, inbox, chat, markdown.
- Each widget binds to object queries and action references.
- **Web runtime:** `app/(apps)/apps/[appId]/[[...page]]`. Moving a kanban card is a transition
  action. Forms are generated from the schema; their rules run in the TS engine, and they submit
  through actions.
- **Portal:** `app/(portal)/p/[workspace]/[[...page]]`.
  - Login by magic link or OIDC, reusing `sso.py`.
  - A separate cookie and the `/portal/v1/*` API.
  - Rows are limited by grants plus restrictive RLS.
- **Mobile:** experience manifest v2 adds screens for object list, detail, form, action, task
  inbox and map, with version negotiation. The renderers move out of the sample apps and into the
  SDK libraries.

### Temporal specifics
- **Workflow IDs:**
  - `proc:{t}:{env}:{process}:{subject|uuid}`, with `USE_EXISTING` for "one per subject";
  - `agent:…`, `sync:…`, `pause:{t}:{execution}`, `reeval:{t}:{decision}`, `sched:{t}:{id}`;
  - workflows started by triggers embed the event id, for dedupe.
- **`ProcessInterpreterWorkflow`:**
  - **Input:** `{tenant, env, definition_id, version, spec_hash, subject, inputs}`.
  - **Spec loading:** the `load_definition` activity checks the hash and caches the spec in a
    per-worker LRU.
  - **Branch conditions** run in-workflow (`processes/expr.py`, delegating to the pure
    `logic.compare`). All I/O happens in activities.
  - **Signals:** `object_event`, `callback`, `cancel`. **Updates:** `complete_task`,
    `approve_tool_call`. **Query:** `state()`.
  - **Timers:** `workflow.sleep` / `wait_condition`.
  - **Continue-as-new** when it is suggested or after 5k events.
  - **Activity idempotency key:** `{workflow_id}/{node_id}/{iteration}`, which becomes the
    `action_executions.idempotency_key`.
- **Determinism:**
  - interpreter changes go behind `workflow.patched(...)`;
  - CI replays a corpus of recorded histories (`tests/fixtures/temporal_histories/`) with
    `Replayer`;
  - workflow files get CODEOWNERS.
- **Existing step types and jobs:**
  - **Pausing steps:** `review_gate` and async `action` keep their policy semantics. A pause
    starts `PausedPolicyWorkflow`. Review decisions and callbacks then signal it, with the
    exactly-once claim from 0.7 kept as a backstop.
  - **Timers and re-evaluation:**
    - a timer replaces `scheduler.check_review_timeouts`;
    - in-flight pauses are migrated by a one-shot job;
    - monitor `reevaluateInDays`, a silent no-op today (`executor.py:972-989`), becomes a real
      `ReevaluateWorkflow`.
  - **Schedules:** tenant cron schedules and scheduled reports move to Temporal Schedules, with a
    reconciler run on boot. **System sweeps stay on APScheduler** under the lease leader.
- **Tests:**
  - `WorkflowEnvironment.start_time_skipping()` for timers and service level agreements (SLAs);
  - `ActivityEnvironment`;
  - `Replayer`;
  - a new `temporal` lane in `run_all_tests.sh` and CI. Under `RULEMIND_REQUIRE_ALL_LANES=1`, a
    skipped lane fails.

## UX/IA redesign and DESIGN.md

### What the inventory found (apps/web)
- **Navigation and duplication.**
  - 31 nav items with only 11 distinct icons, and no role gating.
  - A payload can be run through a policy on about 7 different screens.
  - Two separate release state machines, each with wrong copy.
  - Models are split across 4 routes.
  - Collab is a demo.
  - "Exports" is actually config backup.
- **Styling.**
  - Three styling dialects, with about 1,700 inline `style={}`.
  - Two incompatible kits: `src/v3/kit.tsx` and `src/v3/ui.tsx`.
  - No Table, Tabs, Toast or Tooltip components.
  - React Query is installed but unused.
- **The canvas** (`app/workflow-builder/page.tsx`, 844 lines) is a hand-rolled vertical list of
  cards: no edges, zoom, minimap, auto-layout, undo or run. `PoliciesPage.tsx` has a second,
  reduced step editor.
- **First run.**
  - Sign-in lands on a Dashboard with no empty state.
  - **Session members get `apiKey ""`** (`store.ts:119`), which blanks 11 pages with no way to
    fix it. To be proved in the browser first, then fixed (B2).

### New information architecture (sections by job; nav items appear only when relevant)

| Section | Items (old routes folded in → redirects in `next.config.js`) |
|---|---|
| **Home** | Attention feed (my tasks, failing runs, alerts); getting-started checklist until done; templates gallery. Replaces `/` and `/onboarding`. |
| **Data** | **Objects:** ontology canvas. **Records:** any type as table, kanban by state, map or timeline. **Sources:** connectors, sync, webhooks, devices, CSV import, provider library (from `/api-console`). **Variables.** |
| **Logic** | Policies (one canvas editor; `/workflow-builder` + `/policies` merged, reduced editor deleted). Rules, Decision tables, Scorecards. Lifecycles. **Models:** tabs Registry · Sets · Evaluation · Monitoring (folds `/models`, `/model-sets`, `/evaluations`, `/predictors`). |
| **Automate** | Triggers, Processes, Agents. **Messages:** channels, templates, consent, delivery log. Schedules. |
| **Apps** | App builder for internal, portal and mobile apps (folds `/journeys`); Portal settings. |
| **Operate** | Inbox (folds `/review-queue`). Live map (only when some type has a geo property). Decisions (explorer + replay + explain; folds audit decisions). Runs. Reports. Audit. |
| **Ship** | Releases: env + stage + closure promotion (folds `/deploy`, `/lifecycle`). Experiments (folds `/ab-tests` and the Shadow tab). Backtests (folds `/simulation`). |
| **Settings** | Members & access. Integrations: AI, maps, message providers (folds the AI settings now spread over `/ai`, Branding and Onboarding). Branding. Import/export (was `/exports`). Data protection. Platform admin. |

**Cross-cutting patterns** (each built once, used everywhere):
- **Artifact anatomy.**
  - Header: name, env badge, status, one primary action.
  - Tabs: `Design · Test · Releases · History · Usage`.
  - The **Test drawer** replaces `/test-console`, `/debugger` and the What-if tab. It offers a
    single run with trace, step-through, replay of a past decision, and "test with a real
    object", which shows the hydrated payload.
- **⌘K palette** with navigate, create, search-objects and "Ask AI". The copilot is this palette
  plus a side panel on every builder. It replaces the `/ai` page and the floating button.
- **Progressive disclosure.**
  - Forms, tables and click-built conditions come first.
  - Code (Python, JSON) is one toggle away.
  - Triggers are written as sentences: "When … If … Then …".
- **One canvas kit** for ontology, lifecycle, policy and process:
  - @xyflow/react;
  - drag-in from the palette;
  - typed ports with validation at connect time;
  - ELK auto-layout in a worker that preserves manual positions;
  - minimap, fit-view and search;
  - 100-step undo/redo;
  - autosave of drafts;
  - a run/trace overlay;
  - **a keyboard-accessible list twin of every canvas**.
- **Empty states teach.** Each says what the thing is, offers one primary action, and links to
  "start from a template" and "ask Copilot".
- **Responsive.** Operate, Records and Inbox are fully mobile. Builders are desktop-first, with a
  usable read-only mobile view.

### DESIGN.md (repo root, written before any UI code — step UX0)

**Contents:**
- principles;
- information architecture and navigation map;
- artifact anatomy;
- canvas interaction spec;
- component inventory and when to use each;
- tokens: DTCG 2025.10 JSON → CSS variables → Tailwind v4 `@theme`, mapped over the existing
  `--rm-*` variables so tenant branding keeps working; OKLCH; light and dark;
- type, spacing and radius scales;
- motion;
- accessibility: WCAG 2.2 AA, keyboard map, focus, a contrast gate;
- content style: verbs on buttons, errors that say how to fix;
- empty, loading and error states;
- the first-run journey;
- logistics and lending walkthroughs;
- portal and mobile rules;
- performance budgets: about 250 KB gzip of JS per console route; heavy libraries loaded lazily.

**Research cited:**
- Palantir Workshop (a UI driven by objects, links and actions);
- progressive disclosure (Nielsen 1995);
- Pega's low-code UX principles (defaults over blank starts);
- canvas validation and auto-layout patterns;
- rule-authoring patterns (decision tables, a constrained vocabulary, explanation templates).

**Stack:**
- Tailwind v4;
- Radix primitives in shadcn style;
- cmdk;
- TanStack Table and Virtual;
- react-hook-form + zod;
- @xyflow/react v12 + elkjs;
- dnd-kit;
- MapLibre;
- React Query (already installed);
- openapi-fetch over the sdk-js generated types, replacing the hand-written `src/v3/types.ts`.

## Dead code and weight to remove

Each item is re-verified with grep before deletion. The audit evidence is summarised here.

**Packages and web:**
- **Unused packages.**
  - `packages/widget` (2,483 lines): no importers.
  - `packages/ui` (shim) and `packages/sdk` (alias; first fix the README example at :461).
  - Also drop these from web deps, `transpilePackages`, `apps/web/Dockerfile`,
    `tsconfig.base.json`, `vitest.config.ts` and the CHANGELOG index.
- **`shared/src/schemas.ts` and `zod`:** 0 users. Removing them also retires Dependabot #192.
  The `*.spec.json` fixtures in `shared` stay.
- **Web leftovers.**
  - `pages/_app.tsx` and `pages/_document.tsx`.
  - 3 layouts that only pass children through.
  - 9 dead exports: `rulemindRequest`, `getBearerToken`, `useWorkbenchStore`, `connectorLabel`,
    `sourceMark`, `ConnectorIconMark`, `rootStyle`, `_internal`, `ConnectorId`.
  - Duplicate admin login inside `/branding`.
- **`xlsx` from an unhashed CDN tarball.** Replace it with a single parser in `src/lib/csv.ts`,
  pinned with an integrity hash.

**Backend:**
- **Kafka, WAL and shims.**
  - The Kafka decision outbox, `decision_consumer.py` and the compose `kafka` profile.
  - `decision_wal.py`.
  - `core/lambda_handler.py` and `core/service.py`.
- **Unused or default-off features.**
  - `scim.py` and its 9 routes.
  - `mtls.py`.
  - `quota.py` metering, which removes one upsert per decision.
  - The archiver's ClickHouse and S3 sinks; their dependencies aren't even installed. The
    retention logic stays.
- **Endpoints and code no client calls.**
  - `/sdk/v1/blocks` and `compiler.py` lines 434–563.
  - 3 unused `/analytics/*` endpoints.
  - Undocumented endpoints: `/workflows/loop-debug`, `/deploy/promote`,
    `/schedules/{id}/history`, `/executions/stuck`, `/settings/governance`, `/usage*`, the
    evaluations backtest alias, `/reports/{id}/run`, the duplicate executions resume route, and
    the `/api/v1/health` alias.
  - About 170 lines of dead functions.
- **The `/decide` branch for `FAST_DECIDE`** (`routers/runtime.py:221-231`), which the README
  already retracts. `fast_decide.py` stays for batch simulation and caching.
- **The collab stack:** `collab_hub.py`, `crdt.py`, `routers/collab.py`, `ws_auth.py`, the web
  `/collab` page, the FastAPI pin it forced, and `websockets`.
- **Demo code.** `demo-app`, `flutter-ui`, and about 1,000 lines of static demo fixtures in
  `experience_studio.py`. The `sdk-android/sample-app` and `sdk-flutter/example` apps are kept
  but rewritten as generic shells that render a manifest.
- **The Rust packages** (see decision 2).

**Repo:**
- **Historical docs and reports:** `TESTING_RESULTS.md`, `docs/SWEEP-FINDINGS.md`,
  `docs/DEPENDABOT-TRIAGE.md`, `docs/PRE-PUBLICATION-AUDIT.md`.
- **Committed artifacts:** `docs/deck/`, `design/`, `qa/results/mobile-visual/*.png`, and the
  `qa/e2e-full` output JSONs.
- **Infra duplicates:** `infra/` and `k8s/rulemind.yaml`. `k8s/kind/deps.yaml` stays.
- **Unused scripts:** `scripts/github_*_cleanup.sh`, `scripts/render_teardown.sh` and
  `.env.render.example` (only after owner action O5), and `qa/edge-case-smoke.sh`.
- **`docs/parity/`**, together with its gate at `run_all_tests.sh:561`.
- **`PLAN.md`** (2,672 lines, all 293 items done) is replaced by the Round 6 checklist. Its
  history stays in git.

**Image and hosting:**
- The API image is 923 MB, of which the pip layer is 545 MB.
  - Split requirements into `requirements.txt` (core), `-ml`, `-saml`, `-otel` and `-dev`.
    Imports are already lazy.
  - Make the build multi-stage, with an optional `-ml` image tag.
  - Add the missing `opentelemetry-instrumentation-logging`.
  - **Target: core image under 250 MB.**
- CodeMirror loads through `next/dynamic`, and `rulemind-page.tsx` is split per route.
- `alert-relay` and `watchdog-monitor` merge into one image.

**GitHub** (outward action, authorised by approving this plan):
- Delete the **73 remote branches whose PR is merged and whose tip equals the merged head**
  (PRs #12–#87). None is the head or base of an open PR.
- Run `git fetch --prune`.
- Never touch `round5/*`, Dependabot heads, or `round-5-production-readiness`. That last branch
  is deleted only after the stack lands.

**Deferred:** squashing the 37 Alembic migrations. The schema currently has two managers,
Alembic and `create_all` plus runtime column adds. Consolidating them needs a stamp path for
existing installs, so it gets its own round.

## Owner actions (GitHub/account settings — documented in `docs/SECURITY-SETUP.md`, step 0.8)

- **O1. Actions settings:**
  - fork-PR approval: `first_time_contributors` → **all external contributors**;
  - workflow permissions: read-only;
  - Actions may not create or approve PRs;
  - require actions pinned to a full SHA.
- **O2. Remove the self-hosted runner** `rulemind-local-arm64`, which is online. On a public repo,
  an approved fork PR can target it.
- **O3. Author emails in public history.** Switch to the GitHub noreply address and turn on
  "Keep my email private" and "Block command line pushes that expose my email". Rewriting
  history is not recommended: it breaks every open PR and forks keep the old commits anyway.
- **O4. Ruleset "Main Protection"** (id 14380128) requires 4 checks from deleted workflows, so no
  PR can ever pass. Replace them with `ci-gate` once PR 0.1 has reported it.
- **O5. Retired hosting.** Tear down the retired Render services with `scripts/render_teardown.sh`
  and your own Render key, before D8 deletes the script.
- **O6. Merging is yours:** first #197→#261 in order, then the `r6/*` stack. If hosted jobs still
  don't start after the repo went public, check the Actions billing banner.

## Stages and PR units (execution order; ☐ = one PR, ticked in PLAN.md as it lands)

**Stage 0: security and CI**

- [x] **0.0** `docs(plan):` this checklist replaces the finished Round 1–5 log (history stays in git).
- [ ] **0.1** `ci:` the GitHub-hosted PR gate. The first `r6` PR.
  - Triggers: `ci.yml` runs on `pull_request` with no branch filter, `merge_group` and dispatch.
  - Permissions: `contents: read`; no secrets; actions pinned by SHA.
  - Lanes: python-sqlite, python-pg, web, ts-parity, kotlin, flutter, contracts, migrations,
    image-build, e2e, and `ci-gate`.
  - Plus `scripts/check_workflow_triggers.py`.
- [ ] **0.2** `ci:` remaining workflows to hosted runners.
  - CodeQL and dependency review.
  - Hosted versions of security, load and backup.
  - Multi-arch release images on `ubuntu-24.04` and `-arm`: buildx, SBOM, cosign, dispatch-only.
  - Delete the self-hosted workflows.
- [ ] **0.3** `fix(scheduler):` `http_pull` goes through the egress guard.
  - Extract a pinned client from `webhook_dispatch.deliver:205-226`.
  - Use it in `scheduler.fetch_batch_items:418`.
  - Check URLs with `vet_url` on schedule create and update.
- [ ] **0.4** `fix(rbac):` `/ai/config`, `/ai/budget` and `/ai/usage/reset` require MANAGE_ACCESS
  (`rbac.py:84,150`).
- [ ] **0.5** `fix(deploy):` secure-by-default compose.
  - Ports bound to 127.0.0.1.
  - `docker-compose.prod.yml` with `${VAR:?}` secrets and `RULEMIND_ENV=production`.
  - `scripts/gen_secrets.sh`.
- [ ] **0.6** `fix(webhooks):` the HMAC is computed over the raw body, with an
  `x-webhook-timestamp` 5-minute replay window. The legacy form keeps working until its sunset.
- [ ] **0.7** `fix(runtime):` a paused execution resumes exactly once. The callback and review routes
  use a conditional `UPDATE … WHERE status='paused'`.
- [ ] **0.8** `docs(security):` `docs/SECURITY-SETUP.md` (O1–O6) and CODEOWNERS.

**Stage D: dead code and weight**
- [ ] **D1** web and packages cleanup (the `widget`, `ui` and `sdk` packages, deps, exports, pages,
  xlsx).
- [ ] **D2** delete the Rust packages. Parity becomes 4 engines; update CI, the scripts and CLAUDE.md.
- [ ] **D3** delete the Kafka outbox and consumer and the WAL. This fixes the unbounded outbox.
- [ ] **D4** delete SCIM, mTLS, quota metering, the archive sinks and the core shims.
- [ ] **D5** delete the unused endpoints and dead functions; regenerate OpenAPI and sdk-js.
- [ ] **D6** delete the collab stack.
- [ ] **D7** requirements split, multi-stage image (core under 250 MB, plus `-ml`), Redis profile,
  merged observability image.
- [ ] **D8** purge docs and artifacts; `PLAN.md` becomes the R6 checklist; fix README/HLD counts.
- [ ] **D9** demo apps out; remove `k8s/rulemind.yaml`, `infra/` and the dead scripts.
- [ ] **D10** GitHub branch cleanup (73 merged-PR branches) and prune.

**Stage B: confirmed bugs**
- [ ] **B1** the workflow-builder outcome is now saved and decides.
  - Accept the legacy `outcome` on `PolicyStepModel` (`main.py:399-406`) and normalise it
    recursively in `validate_policy_steps:898`.
  - A data migration normalises stored policies.
- [ ] **B2** member sessions load data. Gate on `apiKey || member` in `kit.tsx:86,119` and
  `app-shell.tsx:214,676`.
- [ ] **B3** `streamDecisions` sends `credentials:"include"` (`api.ts:141`).
- [ ] **B4** loop `config.itemSource`: rules see variables recomputed per item.
  - Implemented in Python `_execute_loop`/`_run_loop_parallel`, Kotlin `executeLoop` and Dart.
  - TS fails closed.
  - Covered by the `shared/loop-item-source.spec.json` parity corpus.
- [ ] **B5** the mobile session manifest carries tenant journeys. A shared
  `tenant_experience_manifest()` is used by both `main.py:1219` and `routers/sdk.py:63`.
- [ ] **B6** AI explain works for any decision: use `storage.get_decision`, and analyse rejections
  with `iter_decisions_window`.
- [ ] **B7** the AI scope filter is generic, not tied to lending vocabulary (`ai.is_in_scope`).

**Stage UX: design and shell**
- [ ] **UX0** `docs:` write `DESIGN.md`.
- [ ] **UX1** tokens: DTCG JSON → CSS variables → Tailwind v4 over `--rm-*`. Tests prove branding
  and dark mode survive.
- [ ] **UX2** Radix primitives (Button, Input, Select, Combobox, Dialog, Sheet, Popover, Tooltip,
  Menu, Tabs, Toast, Badge, Skeleton, EmptyState, PageHeader, Breadcrumbs, Kbd) and a `/dev/ui`
  gallery, with axe checks on every component.
- [ ] **UX3** data and form kit: DataTable with saved views, a schema-driven Form, ConditionBuilder
  (operators from `shared/operators.spec.json`), JsonView, and a lazy CodeEditor.
- [ ] **UX4** typed API client (openapi-fetch), React Query hooks, `useSession`, `X-RuleMind-Env`,
  SSE, and error toasts.
- [ ] **UX5** shell v2:
  - the new IA nav;
  - route groups `(console)`, `(auth)`, `(apps)`, `(portal)`;
  - the ⌘K palette;
  - redirects from old URLs;
  - `e2e/route-health.spec.ts` descends into route groups.
- [ ] **UX6** canvas kit.
- [ ] **UX7** Test drawer.
- [ ] **UX8** Home, templates gallery, and empty states that teach.
- [ ] **UX9** gates: jsx-a11y, an axe sweep of every route, an i18n key lint, and JS bundle budgets.

**Stage R: consolidate existing routes into the new IA** (one PR per cluster; old URLs redirect)
- [ ] **R1** Home (from `/` and `/onboarding`); copilot moves into the palette and side panel.
- [ ] **R2** Data: Sources (connectors and the provider library), Variables.
- [ ] **R3** Logic: Rules, Decision tables, Scorecards.
- [ ] **R4** Logic: Models (4 routes become tabs).
- [ ] **R5** Logic: Policies. The one canvas editor rebuilt on the kit, with branch and loop as group
  nodes; `PoliciesPage`'s reduced editor deleted.
- [ ] **R6** Ship: Releases, Experiments, Backtests.
- [ ] **R7** Operate: Inbox, Decisions, Reports, Audit; Schedules moves under Automate.
- [ ] **R8** Settings (including Import/export), auth pages, admin, error pages.
- [ ] **R9** delete the `src/v3` kit, `kit.tsx`, the old shell and `src/v3/types.ts`.

**Stage F: ontology foundation**
- [ ] **F1** definitions + promotion core (migration 0037; `app/promotion.py` extracted from
  `main.promote_entity`).
- [ ] **F2** object and link type specs plus a compatibility checker; `routers/ontology.py`.
- [ ] **F3** records, links and grants tables (migration 0038, JSONB on Postgres); API key
  environment enforced.
- [ ] **F4** records repository: compare-and-swap, soft delete, link/unlink, `__epoch` values,
  on-demand indexes.
- [ ] **F5** object query and aggregate API. One filter DSL compiles to Postgres SQL, SQLite SQL and
  an in-memory evaluator, and all three must return identical results.
- [ ] **F6** field variables + managed connectors, with the `shared/field-variables.spec.json`
  corpus in all 4 engines.
- [ ] **F7** neighbourhood hydration + `POST /objects/{type}/{id}/decide` + decision lineage
  (migration 0039). Benchmark: p50 under 5 ms with 50 neighbours.
- [ ] **F8** UI: ontology studio (canvas + list twin).
- [ ] **F9** UI: Records explorer (table, filters, detail, related, timeline, saved views).

**Stage E: events**
- [ ] **E1** `object_events`, partitioned. Ships with the RLS fix for partitions; partitions are
  maintained by the archiver.
- [ ] **E2** dispatcher role: leases, SKIP LOCKED, NOTIFY, idempotency, DLQ and replay API, lag
  metrics.
- [ ] **E3** triggers: kind, compiled per tenant and env, depth and rate caps; `routers/triggers.py`.
- [ ] **E4** outbound object events through webhook subscriptions.
- [ ] **E5** UI: sentence-style trigger builder, live timeline, dispatcher health.

**Stage A: actions**
- [ ] **A1** action types + engine + `OPERATE`/`operator` (migration 0040).
- [ ] **A2** `POST /actions/{name}/run` (with `dry_run`) and an execution log.
- [ ] **A3** effects role and hourly Merkle anchoring.
- [ ] **A4** human tasks + approval-gated actions + one Inbox over legacy `review_tasks`.
- [ ] **A5** UI: action builder, generated action forms, Inbox.

**Stage T: Temporal**
- [ ] **T1** compose: `temporal-schema` job, one `temporal` container, namespace and
  search-attribute job, UI on 127.0.0.1, `worker --roles` includes `temporal`.
- [ ] **T2** `app/temporal/`: client, AES-GCM codec, tenant interceptor that fails closed, readiness
  check in `main._dependency_status`, and a `temporal` CI lane.
- [ ] **T3** `PausedPolicyWorkflow`, `ReevaluateWorkflow`, migration of in-flight executions.
- [ ] **T4** tenant schedules and scheduled reports move to Temporal Schedules, plus a reconciler.
- [ ] **T5** Helm: the official Temporal chart as an optional dependency, or an external cluster;
  worker Deployments per role.
- [ ] **Checkpoint: walking skeleton (API only).** Stop ETA changes → late decision → task + message
  → process waits for an SLA. Must pass before Stage C widens the work.

**Stage C: capabilities**
- **Lifecycles:**
  - [ ] L1 state machines with `state_timers`;
  - [ ] L2 lifecycle canvas + simulator on a real object.
- **Processes:**
  - [ ] P1 spec and validator: start, decide, action, branch, set_vars, wait_timer, wait_event,
    human_task, parallel, join, loop, sub_process, ai_step, agent, send_message, end;
  - [ ] P2 interpreter core and the `process_instances` mirror;
  - [ ] P3 waits, tasks, callbacks (`/callbacks/{token}`);
  - [ ] P4 parallel, loops, sub-processes, continue-as-new, and the replay corpus;
  - [ ] P5 processes API;
  - [ ] P6 process studio and run viewer.
- **Messages:**
  - [ ] C1 data model;
  - [ ] C2 templates and preview (DLT, WhatsApp parameters);
  - [ ] C3 send pipeline (consent, quiet hours, caps, dedupe);
  - [ ] C4 adapters: generic HTTP, SMTP, Slack, Teams;
  - [ ] C5 adapters: Twilio, WhatsApp Cloud, FCM v1, APNs;
  - [ ] C6 inbound callbacks, signatures, STOP handling;
  - [ ] C7 UI.
- **AI:**
  - [ ] G1 LLM gateway, including `openai_compatible`;
  - [ ] G2 ontology tool registry (impact gating, PII redaction);
  - [ ] G3 agent runs;
  - [ ] G4 AI process nodes (classify, extract, generate, with confidence branches);
  - [ ] G5 MCP server and scopes;
  - [ ] G6 builder copilot (package drafts, diff, dev only);
  - [ ] G7 agents studio and run viewer.
- **Maps:**
  - [ ] M1 provider layer (OSRM, Valhalla, VROOM, Nominatim, Google, Mapbox, HERE; haversine fallback);
  - [ ] M2 `maps` compose profile (OSRM prep job, VROOM, Nominatim small region, PMTiles);
  - [ ] M3 geo property types, H3, radius and bbox queries, geocode/route/optimise actions;
  - [ ] M4 MapLibre widget and Live map.
- **Telematics:**
  - [ ] TL1 devices, partitioned points, rollups, `telemetry_rule` kind;
  - [ ] TL2 HTTP batch ingest + MQTT bridge (mosquitto);
  - [ ] TL3 processor (geofence, threshold, window, dwell, offline, ETA);
  - [ ] TL4 fleet UI (rule builder, track replay).
- **Sync:**
  - [ ] S1 mapping + changed-only bulk upsert;
  - [ ] S2 `SyncRunWorkflow` (HTTP pull using `oauth2_client`, DB query-poll);
  - [ ] S3 files (local volume or S3) + CSV import;
  - [ ] S4 inbound webhook sources + Debezium Server preset;
  - [ ] S5 wizard UI with drag-and-drop CSV.
- **Apps:**
  - [ ] AP1 app spec and resolver;
  - [ ] AP2 app runtime, with forms running the TS engine;
  - [ ] AP3 drag-and-drop app builder;
  - [ ] AP4 portal identity and row scope;
  - [ ] AP5 portal runtime;
  - [ ] AP6 mobile experience manifest v2 (including `mobile_session_payload`);
  - [ ] AP7 Flutter renderer library;
  - [ ] AP8 Android Compose renderer module.

**Stage K: templates as installable packages**
- [ ] **K1** package format, installer, export/import. Generalises `routers/runtime.py:648,783`;
  validate everything, then apply, idempotently.
- [ ] **K2** Logistics I: Vehicle, Driver, Depot, Route, Stop, Shipment, Customer, Geofence;
  lifecycles; late-risk, dispatch-eligibility and driver-hours policies.
- [ ] **K3** Logistics II: dispatch and delivery process, ETA escalation, customer SMS/WhatsApp,
  reschedules, VROOM optimisation, telematics rules.
- [ ] **K4** Logistics III: dispatcher console, driver mobile app, tracking portal, seed data,
  `scripts/sim/fleet_sim.py`.
- [ ] **K5** Lending: wraps `seed_data.py`. Application lifecycle; underwriting with document AI,
  review, e-sign callback and disbursal; collections cadence; underwriter workbench; applicant
  portal and mobile.
- [ ] **K6** Field service.
- [ ] **K7** Insurance claims, with an AI triage agent.

**Stage H: self-hosted scale and cost**
- [ ] **H1** PgBouncer in transaction mode for the API and workers. Temporal connects directly.
- [ ] **H2** KEDA ScaledObjects for each worker role (event backlog, outbox depth, Temporal
  schedule-to-start, stream lag), with HPA as fallback.
- [ ] **H3** files on S3, Temporal DB backups, retention jobs.
- [ ] **H4** Caddy as a single-origin TLS proxy (`/`, `/api`, `/sdk`, `/mcp`, `/portal`, `/p`) with
  HSTS.
- [ ] **H5** `scripts/seed_scale.py` (10M objects via COPY), k6 scenarios, `docs/PERFORMANCE.md`.
- [ ] **H6** Grafana dashboards and alerts: event lag, DLQ, Temporal, comms failures, telematics
  lag, action errors.

**Stage Z: docs and acceptance**
- [ ] **Z1** HLD, CLAUDE.md (four engines, roles, profiles, hosted CI) and README.
- [ ] **Z2** self-hosting guide: lite and scale, Temporal operations and upgrades, map regions, MQTT,
  hardening, cost.
- [ ] **Z3** builder guides, template walkthroughs, MCP and agents guide, regenerated `API.md`.
- [ ] **Z4** the final acceptance run (below), recorded in `TEST_RESULTS.md`.

## Critical files and utilities to reuse (verified)

**Backend** (`apps/python-executor/app/`):
- **Engine and API surface:**
  - `executor.py`: `_payloads_by_source:503`, `_execute_loop:1013`, `_execute_action:1765`,
    `_execute_review_gate:2137`.
  - `main.py`: `validate_policy_steps:898`, `promote_entity:1123`, `mobile_session_payload:1206`.
  - `rbac.py`, `routers/runtime.py`, `routers/ai.py`, `ai.py`, `scheduler.py`,
    `experience_studio.py`, `models.py`.
- **Security and tenancy:**
  - `rls.py`: `install`, `enforcement`, `scope_transaction`, `tenant_tables`.
  - `context.tenant_scope`.
  - `egress.py`: `vet_url`, `build_pinned_request`, clamps.
  - `auth.hmac_signature` / `verify_hmac_signature`, `oauth2_server.py`, `oauth2_client.py`,
    `sso.py`, `rate_limit.py`, `pagination.py`, `idempotency.py`,
    `security_config.verify_production_secrets`.
- **Storage:** `storage.py`: `add_promotion:2530`, `last_promotion_actor:2579`,
  `dual_control_enabled:2593`, `get_decision:2236`, `iter_decisions_window:2329`,
  `add_audit_event:2912`, `create_review_task:4256`, `try_acquire_scheduler_lease:5527`.
- **Execution reuse:**
  - `webhook_dispatch.py`: `deliver`, `drain`, `deliver_claimed`, `BACKOFF_SECONDS`,
    `validate_event_types`.
  - `connector_fetch.fetch_source`, `mailer.send_text_email`, `templates.resolve_template`,
    `audit_chain.compute_hash`, `logic.compare`, `compiler.compile_variable` / `compile_bundle`,
    `variable_runner.compute_variables`, `variable_reachability`, the `fast_decide` epoch cache
    pattern, `reviews.submit_review_decision`, `openapi_contract.py` + `api_models.py` +
    `scripts/openapi_gate.py`.

**Web** (`apps/web/`):
- `src/lib/api.ts`, `src/lib/store.ts`, `src/v3/theme.ts` + `branding.ts` (the token bridge).
- `src/components/app-shell.tsx`: `useAuthGate`, `NAVIGATION`.
- `src/v3/command-palette.tsx`, `src/lib/csv.ts`, `next.config.js` (redirects).

**Packages and harness:**
- `packages/sdk-js` (`gen:types`, `contract-check.ts`), `packages/shared/*.spec.json`.
- `scripts/run_all_tests.sh`, `check_config_surface.py`, `verify_chart.py`.
- `tests/test_tenant_isolation_matrix.py`, `test_rls.py`, `test_zzz_lane_intact.py`.
- `docker-compose.yml`, `helm/rulemind/`.

## Verification

**Every PR:**
- `ci-gate` is green on GitHub-hosted runners: both Python lanes, TS parity, Kotlin, Flutter,
  contracts, migrations up and down, the image build, and e2e. CLAUDE.md warns the image build is
  the lane that catches what no test does.
- UI PRs are also checked in the browser preview: console and network clean, keyboard path
  works, and a screenshot is attached to the PR.
- Bug fixes are mutation-checked.

**Stage gates:**
- **Stage 0:**
  - `check_workflow_triggers.py` passes.
  - A dry run of the release lists amd64 and arm64.
  - The prod compose refuses to boot with default secrets.
  - The SSRF matrix passes: 169.254, 127.1, `::ffff:`, a 302 to metadata, DNS rebinding.
- **Stage D:**
  - Every lane is still green after each deletion.
  - The core image is under 250 MB.
  - `docker compose up` brings up the 5-container lite stack and passes the smoke test.
  - The outbox no longer grows when Kafka is unset.
- **Foundation:**
  - RLS covers every new table, including partitions.
  - Portal restrictive policy tests pass.
  - Event writes are atomic: a rollback leaves no event.
  - Dispatcher crash tests: kill it mid-batch and exactly one effect still happens.
  - The field-variable and loop corpora pass in all 4 engines.
  - Hydration is deterministic.
  - Codec and interceptor tests, and a 48-hour durable pause under time skipping.
  - The walking skeleton passes.
- **Capabilities:**
  - Interpreter tests under time skipping, plus `Replayer`.
  - The comms suppression matrix.
  - MCP conformance with the official client SDK: initialize, tool listing, calls, scope denial,
    and a 401 that carries resource metadata.
  - Telematics replay.
  - A sync re-run emits no events.

**Final acceptance** (compose stack + OSRM fixture region + `qa/mocks/comms_sink.py` +
`qa/mocks/llm_mock.py` + the fleet simulator):

1. **`e2e/acceptance/logistics-from-scratch.spec.ts`, entirely through the UI:**
   1. Sign up, starting blank.
   2. Draw the types and links on the canvas, then repeat one through the keyboard list twin.
   3. Build the lifecycles.
   4. Build the late-risk policy from object properties.
   5. Add the trigger "When Stop ETA changes, if late, send Delay SMS and create a dispatcher
      task".
   6. Set up a generic HTTP channel, a template and quiet hours.
   7. Build a delivery process with an SLA escalation.
   8. Drag and drop the dispatcher app, the tracking portal and the driver mobile app.
   9. Drop in a CSV of shipments.
   10. Run VROOM to optimise the routes.
   11. Run the simulator. Assert the following:
       - the markers move;
       - a geofence marks a stop as arrived;
       - the late decision fires;
       - **exactly one** SMS reaches the sink;
       - the dispatcher task completes.
   12. Promote dev → uat → prod, with dual control by a second member.
   13. Through a portal magic link, customer A sees only A's shipment.
   14. Axe passes on every screen visited.
2. **`e2e/acceptance/lending.spec.ts`:**
   1. Install the package.
   2. An applicant applies through the portal; the decision matches the seeded scorecards.
   3. Mock-LLM document extraction runs.
   4. The review is approved.
   5. The e-sign callback resumes the process.
   6. Disbursal happens.
   7. A collections message is sent, respecting consent.
3. **`qa/chaos/temporal_resume.py`:**
   - Run 50 processes and kill `worker` mid-run, then the dispatcher, then the Temporal server.
   - Restart each one.
   - All 50 must complete, with no duplicate `action_executions` or `messages`.
4. **Tenant isolation:**
   - Two tenants, each with two portal users, across every new route.
   - An activity with no tenant fails closed.
   - Every visibility query is filtered by tenant.
5. **Load:**
   - Reference cluster: 3×8 vCPU, plus Postgres on 8 vCPU / 32 GB.
   - Targets:

     | Scenario | Target |
     |---|---|
     | Object decide | 500/s, p50 ≤20 ms, p95 <50 ms |
     | Event dispatcher | 1k events/s, lag p95 <2 s |
     | Process starts | 50/s |
     | Telemetry | 1k points/s, derived events p95 <3 s |
     | Messages | 200/s, zero duplicates |
     | Indexed queries over 10M objects | p95 <150 ms |

   - Also measure the lite stack's RAM at idle and under 50 decisions/s, and record it in
     `docs/PERFORMANCE.md`. This is the "cheap" number.
6. **The full gate** (`make test-all`) regenerates `TEST_RESULTS.md`, and every lane is green.

## Risks and non-goals

**Risks, with mitigations:**
- **Size.** About 130 PRs.
  - Feature flags (`RULEMIND_FEATURES`), the walking-skeleton checkpoint, and a strict stage
    order.
  - Review bandwidth is the real limit.
- **Temporal operations for self-hosters.** An idempotent schema job, an upgrade runbook, and
  `TEMPORAL_MODE=off`.
- **Interpreter non-determinism.** `patched()`, the replay corpus, CODEOWNERS.
- **Postgres as the event bus beyond the target load.** Benchmark in H5 early.
- **JSONB at 10M rows.** On-demand indexes, and prod refuses to filter large types on unindexed
  properties.
- **Large map regions** need GBs of RAM and disk. Default to a small region. Colima's VM disk is
  the local constraint.
- **Prompt injection through object data.** Per-agent tool allowlists, approvals for
  high-impact actions, budgets.
- **Messaging compliance** (DLT, WhatsApp templates, consent). Built into the pipeline, not left
  to docs.
- **Version pins.** `fastapi` must stay compatible with `mcp` and `temporalio`, and every new UI
  library must support React 19. Checked in each library's PR.
- **Hosted CI may still not start** if the account billing block also applies to public repos.
  Verified on PR 0.1; the fix is owner action O6.

**Non-goals:**
- multi-region active-active or sharding;
- a Temporal namespace per tenant;
- immutable versions for existing policies;
- migrating running process instances to a new version;
- real-time co-editing;
- building our own CDC, tile pipeline or VRP solver;
- making PostGIS, TimescaleDB, Kafka or Redis mandatory;
- a native iOS SDK;
- offline-first object sync on mobile;
- a voice channel;
- agent autonomy over high-impact actions without approval;
- a package marketplace;
- squashing Alembic migrations.
