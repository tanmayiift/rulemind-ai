# RuleMind Android SDK

`packages/sdk-android` contains the Android edge-runtime and wrapper modules:

- `rulemind-core`: pure Kotlin evaluation runtime
- `rulemind-android`: Android networking, crypto, persistence, and sync wrapper
- `sample-app`: minimal integration example

## Install

Add the published artifact:

```kotlin
implementation("com.rulemind:rulemind-android:4.1.0-beta.1")
```

## Initialize

```kotlin
RuleMind.initialize(
    context = applicationContext,
    config = RuleMindConfig(
        baseUrl = "https://api.your-company.com",
        apiKey = "rm_live_xxx",
        serverPublicKeyPem = "-----BEGIN PUBLIC KEY-----...-----END PUBLIC KEY-----",
    ),
)
```

## Sync and Evaluate

```kotlin
RuleMind.syncNow()

val decision = RuleMind.evaluate(
    policyId = "policy_pl_underwriting",
    payload = mapOf(
        "bureau" to mapOf("scores" to listOf(mapOf("scoreName" to "CIBILTUSC3", "score" to "00760"))),
        "bank" to mapOf("summary" to mapOf("avgBalance" to 45200)),
    ),
    userId = "user-123",
)
```

## Upgrade Notes

- SDK 4.1 reads bundle `tree` rules and backward-compatible `nodes`.
- Edge mode skips workflow-only steps: `action`, `transform`, `review_gate`.
- Use `/sdk/v1/decide` server mode when you need full workflow execution.
