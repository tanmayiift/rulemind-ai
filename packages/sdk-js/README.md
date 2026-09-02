# RuleMind JavaScript/TypeScript SDK

`@rulemind/sdk-js` — a thin, typed client for the RuleMind **server** decisioning
endpoints. Node and browser.

> **Server-side evaluation only.** This package calls the backend for every
> decision. If you need on-device evaluation from a compiled edge bundle, use the
> [Flutter SDK](../sdk-flutter) or the [Android SDK](../sdk-android), which embed
> the rule engine. See [Edge vs server](#edge-vs-server-evaluation).

## Install

```bash
pnpm add @rulemind/sdk-js
```

## Use

```ts
import { RuleMindServerClient } from "@rulemind/sdk-js";

const client = new RuleMindServerClient({
  baseUrl: "https://rulemind.example.com",
  apiKey: process.env.RULEMIND_API_KEY!,
});

const decision = await client.decide({
  policyId: "policy_travel_claim",
  payload: { claim: { amount: 42000, priorClaims24m: 1 } },
  userId: "user-123",
});

console.log(decision.outcome, decision.score);
```

Never hardcode the API key. It is a tenant credential.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `decide(request)` | `POST /sdk/v1/decide` | Evaluate a policy. Full workflow support. |
| `sendEvents(events)` | `POST /sdk/v1/events` | Batch-upload client telemetry. |
| `getBundle(options)` | `GET /sdk/v1/bundle` | Fetch a compiled edge bundle. Returns `null` when the tenant has no published bundle. Pass `clientPublicKey` to receive an encrypted bundle. |

Config: `{ baseUrl, apiKey, timeoutMs?, sdkVersion? }`.

## Edge vs server evaluation

RuleMind evaluates in two places, and they are **not** equivalent:

- **Server** (this package): evaluates everything — variables, rules, scorecards,
  policies *and* workflow steps such as connectors, external actions and review
  gates.
- **Edge** (Flutter/Android SDKs, from a bundle): evaluates variables, rules,
  scorecards and policies. **Workflow-only steps are skipped** and reported in
  the decision's `serverOnlyStepsSkipped`.

So a decision produced at the edge is only comparable to a server decision when
`serverOnlyStepsSkipped` is empty. Treat a non-empty list as "partial — needs the
server", not as a disagreement between engines.

## Related

- [`@rulemind/sdk`](../sdk) — an alias that re-exports this package.
- [Flutter SDK](../sdk-flutter), [Android SDK](../sdk-android) — on-device engines.
- [`INTEGRATION.md`](../../INTEGRATION.md) — backend setup and configuration.
