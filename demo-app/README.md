# RuleMind Journeys — demo app

A Flutter app (one codebase, iOS + Android) that runs four **non-lending** decisioning
journeys against a RuleMind server, and against the on-device engine in the same
screen. Each scenario declares the decision the policy is *designed* to make, so the
app doubles as a live conformance check of whatever server it points at.

| Sector | Policy | Gates | Paths exercised |
|---|---|---|---|
| Insurance | Motor Claim Triage | documentation · fraud signals · high-value referral | approve · review · reject · review |
| E-commerce | Checkout Fraud Screening | chargeback block · velocity · device & address | approve · reject · review · review |
| Marketplace | Seller Onboarding (KYB) | sanctions · identity & banking · category risk | approve · reject · review · review |
| Telecom | Postpaid Eligibility | KYC · payment behaviour · line limit | approve · reject · review · review |

Every screen shows the verdict, the score, and each condition that fired — variable,
operator, threshold and the actual value — so the decision is inspectable rather than
just reported.

## Running it

Seed the four policies into a running server first:

```bash
python qa/e2e-full/seed_journeys.py
```

Then:

```bash
cd demo-app
flutter run                      # pick a simulator/emulator or device
```

The API base URL defaults per platform — `10.0.2.2:8080` on the Android emulator (the
host loopback as seen from the emulator) and `127.0.0.1:8080` on the iOS simulator.
Cleartext to those hosts is allowed in the debug config only.

## Server vs on-device

The toggle on each journey switches between:

* **Server** — `POST /api/v1/decide`.
* **On-device** — `RuleMind.evaluate()` against the compiled edge bundle, with server
  fallback deliberately **off**, so an edge decision cannot quietly become a network
  call and make a parity check meaningless.

The decision cache is disabled (`decisionCacheTtlMs: 0`): with the default five-minute
TTL, re-running a scenario replays the previous verdict and any engine change looks
like it had no effect.

## Tests

```bash
flutter test                                     # widget + journey-definition tests
flutter test integration_test/journeys_test.dart -d <device-id>
```

The integration suite runs all 16 scenarios against the live API, asserts the designed
outcome for each, checks that a rejection names the gate that caused it, drives one
decision through the UI, and compares every scenario server-vs-on-device.
