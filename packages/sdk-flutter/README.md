# RuleMind Flutter SDK

`packages/sdk-flutter` provides the Flutter-facing RuleMind mobile SDK.

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

## Sync and Evaluate

```dart
await RuleMind.syncNow();

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

## Notes

- Edge mode evaluates variables, rules, scorecards, and policies.
- Workflow-only steps are skipped locally and surfaced via `serverOnlyStepsSkipped`.
- Use server mode through `/sdk/v1/decide` when you need full workflow execution.
