# RuleMind Flutter SDK

On-device decisioning for Flutter. Evaluates a compiled, signed policy bundle
locally and syncs the resulting decisions back to the backend.

## Install

```yaml
dependencies:
  rulemind: ^4.1.0-beta.1
```

## Initialize

```dart
await RuleMind.initialize(
  RuleMindConfig(
    baseUrl: "https://api.your-company.com",
    apiKey: "rm_live_xxx",
  ),
);
```

Never hardcode the API key in a shipped app — it is a tenant credential. Read it
from your build configuration or a secure store.

`initialize` opens local storage, restores the durable decision outbox, and
registers a periodic background sync. Call it once, at startup.

## Evaluate

```dart
final decision = await RuleMind.evaluate(
  "policy_pl_underwriting",
  {
    "bureau": {
      "scores": [
        {"scoreName": "CIBILTUSC3", "score": "00760"}
      ]
    }
  },
  userId: "user-123",
);
```

`evaluate` fetches the bundle on first use if one is not already cached, so an
explicit `syncNow()` beforehand is optional. Call `syncNow()` yourself when you
want to control *when* the download happens — on Wi-Fi, or at a natural pause —
rather than on the first decision.

## What edge mode does and does not evaluate

This is the single most important thing to understand about the SDK.

**Evaluated on device:** variables, rules, scorecards, policies.

**Not evaluated on device:** workflow-only steps — connectors, external API
actions, review gates, sub-workflows. These require the server.

When any step is skipped, the SDK lists it:

```dart
if (decision.serverOnlyStepsSkipped.isNotEmpty) {
  // This is a PARTIAL decision. It is not equivalent to a server decision,
  // and comparing the two is not a like-for-like check.
  print('Skipped: ${decision.serverOnlyStepsSkipped.join(', ')}');
}
```

Treat a non-empty `serverOnlyStepsSkipped` as *"partial — needs the server"*, not
as a disagreement between engines. Surface it in your UI rather than presenting
the outcome as final. For full workflow execution, use server mode via
`/sdk/v1/decide`.

## Getting decisions back to the backend

`evaluate` enqueues every completed decision into a durable, deduplicated outbox.
The outbox drains in two ways:

```dart
await RuleMind.syncPendingDecisions();          // drain now
final queued = await RuleMind.pendingDecisionCount();  // how many are waiting
```

…and automatically, via the periodic background sync registered by `initialize`.

Because the outbox is durable and server-side ingestion is idempotent, decisions
taken offline survive an app restart and are uploaded once connectivity returns —
a retry after a lost acknowledgement will not double-log. Show
`pendingDecisionCount()` in your UI if users need to know whether their decisions
have reached the backend.

## Paused executions

A policy that hits a review gate returns a paused decision rather than a final
one. Retrieve and resume it:

```dart
final paused = await RuleMind.getExecution(executionId);
final resumed = await RuleMind.resumeExecution(executionId, reviewDecision);
await RuleMind.flushPendingOperations();  // dispatch any queued operations
```

## Configuration worth knowing

| Field | Default | Effect |
|---|---|---|
| `enableServerFallback` | `true` | When no bundle is available, fall back to a server decision. Set `false` only when "on-device" must mean strictly on-device — e.g. a parity test — since a silent round trip would otherwise masquerade as an edge decision. |
| `decisionCacheTtlMs` | 5 minutes | Repeat evaluations of an identical payload replay the cached verdict. Set `0` to force re-evaluation, which is what you want when testing engine changes. |
| `bundleSyncIntervalMinutes` | `15` | How often the background sync refreshes the bundle and drains the outbox. |

## Related

- [Android SDK](../sdk-android) — the Kotlin equivalent.
- [`@rulemind/sdk-js`](../sdk-js), [Python SDK](../sdk-python) — server-side clients.
- [`../../INTEGRATION.md`](../../INTEGRATION.md) — backend setup.
