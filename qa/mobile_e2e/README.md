# RuleMind Mobile E2E

This folder contains the reproducible closeout harness for the mobile super-app.

## Local core closure

Run:

```bash
bash qa/mobile_e2e/run_core_closure.sh
```

This runs:

- Python backend tests
- TypeScript typecheck and tests
- Android core and sample-app JVM tests
- Android sample-app debug and androidTest assembly
- Flutter package tests if `flutter` is installed
- the generated closeout report at [qa/results/mobile-e2e-closeout.md](/Users/whiteknight/Downloads/RuleMind.AI/rulemind-ai/qa/results/mobile-e2e-closeout.md)

## Live backend harness

Use these scripts for emulator-backed UI tests:

- `bash qa/mobile_e2e/start_fastapi.sh`
- `bash qa/mobile_e2e/wait_for_health.sh`
- `bash qa/mobile_e2e/stop_fastapi.sh`

By default the backend starts on `127.0.0.1:8080` with a deterministic SQLite database under `.runtime/`.

## Secret-dependent gates

These are wired in CI but require external inputs:

- Device-cloud validation requires BrowserStack or equivalent credentials.
- Signed sample APK generation requires:
  - `ANDROID_SAMPLE_KEYSTORE_BASE64`
  - `ANDROID_SAMPLE_KEYSTORE_PASSWORD`
  - `ANDROID_SAMPLE_KEY_ALIAS`
  - `ANDROID_SAMPLE_KEY_PASSWORD`
