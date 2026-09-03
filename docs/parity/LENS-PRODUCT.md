# Product lens

Who asks for each gap, what it unblocks, and — importantly — where the right answer is *no*.

## The shape of the difference

RuleMind is a **decisioning engine**: connectors → variables → rules → scorecards → policies,
promoted `dev → uat → prod` with audit trails. The changelog describes a broader **platform**.
Of 22 features, 2 are present, 9 partial, 10 absent, 1 cut (re-audited 2026-09-04).

That is a scope difference, not a quality one, and the roadmap should be argued on customer
value rather than on closing a gap count.

## Where to say no

**SQL editor (P14) — cut.** There is no SQL datasource in this product. An editor needs
something to edit. Building one to match a changelog line would mean first inventing a SQL
datasource nobody asked for. If a customer needs SQL sources, *that* is the feature request,
and the editor follows it.

**Superflows (P19) — undefined.** Zero hits, and no obvious analogue. It reads like a naming
artifact of the other platform's object model rather than a capability this product lacks.
Not scheduled until someone can state what a superflow does that a policy composed of
sub-workflows cannot.

**Kafka + exactly-once (P09) — mostly answered more cheaply.** The buyer-visible need is
"decisions are not lost and not double-counted". A DLQ plus an `Idempotency-Key` convention on
the existing Redis bus and WAL delivers that in Phase 4. True exactly-once semantics is a
multi-month distributed-systems project with permanent operational cost, and should be taken
on against a paying requirement — not left on a roadmap as a checkbox.

## What actually unblocks deals

Ranked by how soon each one blocks a real customer, which is **not** the same as changelog order:

| rank | need | why it blocks | phase |
|---|---|---|---|
| 1 | **Audit immutability** | A regulated buyer's security review asks how you prove the audit log was not altered. Today an operator with DB access can rewrite history undetectably, and the archiver purges the hot copy. | Track B3 |
| 2 | **Usage metering** | There is no decision-volume metering and no quota, so there is no way to bill. `Tenant.plan` exists but only drives rate limits. | Track B4 |
| 3 | **Outbound webhooks** | Without them every customer integration is polling. This is the most-requested integration primitive in the whole list. | 4 |
| 4 | **Connector connection-test** | The first thing anyone does after configuring a data source is press "test". Its absence reads as unfinished. | 3 |
| 5 | **Canary + rollback** | Nobody promotes a policy to production traffic without a way to back out. | 5 |
| 6 | **Step-through debugger** | The difference between "the engine said reject" and "I can show you why". Sells the product in demos. | 5 |

**The top two are not on the changelog at all.** They came out of the B2B-readiness review.
That is the single most important finding in this analysis: *parity with the changelog is not
the same as being sellable*, and if the two compete for the same engineer, readiness wins.

## Cheap wins worth taking first

Each rides substrate that already exists, so it is days rather than weeks:

- **Connector `/test`** — the pattern already exists for AI providers.
- **Sub-workflow by alias** — resolution exists, keyed on id; add a lookup.
- **Nested-key validation** — flat validation and dotted-path resolution both exist, unjoined.
- **Retention in months** — the mechanics are there, only the unit is wrong.
- **~~`decision_table` in the builder palette~~ — DONE (2026-09).** The executor ran it and the
  UI never offered it: a supported capability, unreachable. It is now at
  `apps/web/app/workflow-builder/page.tsx:34`, and `apps/web/test/workflow-builder-palette.test.ts`
  parses both the palette and the executor so the two cannot silently diverge again. Kept here
  because it is the cleanest example of the category — a **bug wearing a feature's clothes**.

## MVP cut vs full

For the large items, ship the cut that answers the customer question and defer the rest:

| feature | MVP cut | full |
|---|---|---|
| Monaco (P13) | syntax highlighting in place of `<textarea>` — ~2 days | RuleMind-aware completion over variables, schemas and ~230 Excel functions — ~2 weeks |
| Predictor (P20) | entity + values, reusing the ClickHouse pipeline | drift reports and monitoring |
| Canary (P11) | percentage split on an existing release | scheduled ramp with auto-rollback on SLO breach |
| Matrix (P07) | n-dimensional decision table, hand-authored | XLSX import — where most of the effort actually is |
| Journey (P15) | — | no meaningful cut: it needs an SDK surface across five engines |
