# RuleMind Python SDK

A typed, dependency-light client for the RuleMind **server** decisioning
endpoints.

> **Server-side evaluation only.** Every decision is a network call. On-device
> evaluation from a compiled edge bundle is available in the
> [Flutter](../sdk-flutter) and [Android](../sdk-android) SDKs. See
> [Edge vs server](#edge-vs-server-evaluation).

## Install

```bash
pip install -e packages/sdk-python
```

## Use

```python
from rulemind import RuleMindServerClient

client = RuleMindServerClient(
    base_url="https://rulemind.example.com",
    api_key=os.environ["RULEMIND_API_KEY"],
)

decision = client.decide(
    policy_id="policy_travel_claim",
    payload={"claim": {"amount": 42000, "priorClaims24m": 1}},
    user_id="user-123",
)

print(decision.outcome, decision.score)
```

Never hardcode the API key. It is a tenant credential.

## API

| Method | Endpoint | Purpose |
|---|---|---|
| `decide(...)` | `POST /sdk/v1/decide` | Evaluate a policy. Full workflow support. |
| `send_events(events)` | `POST /sdk/v1/events` | Batch-upload client telemetry. |
| `get_bundle(...)` | `GET /sdk/v1/bundle` | Fetch a compiled edge bundle. Returns `None` when the tenant has no published bundle. Pass `client_public_key` for an encrypted bundle. |

Constructor: `RuleMindServerClient(base_url, api_key=None, timeout=5.0, sdk_version="4.0.0")`.

`RuleEngineClient` is also exported as a backwards-compatible name.

## Edge vs server evaluation

RuleMind evaluates in two places, and they are **not** equivalent:

- **Server** (this package): evaluates everything — variables, rules, scorecards,
  policies *and* workflow steps such as connectors, external actions and review
  gates.
- **Edge** (Flutter/Android SDKs, from a bundle): evaluates variables, rules,
  scorecards and policies. **Workflow-only steps are skipped** and reported in
  the decision's `serverOnlyStepsSkipped`.

An edge decision is only comparable to a server decision when
`serverOnlyStepsSkipped` is empty. A non-empty list means "partial — needs the
server", not a disagreement between engines.

## Related

- [Flutter SDK](../sdk-flutter), [Android SDK](../sdk-android) — on-device engines.
- [`INTEGRATION.md`](../../INTEGRATION.md) — backend setup and configuration.
