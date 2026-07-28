# RuleMind Stateless Decision Core (`app/core`)

A pure, **DB-free** evaluation engine. `decide(bundle, payload, context)` is a
deterministic function of its inputs — no storage, network, or global state. It is
the primary building block of the **Kubernetes-first** scalability story: run N
identical stateless replicas behind a Service/Ingress with an HPA on RPS/latency;
pods are disposable and interchangeable because they hold no state.

The same core also runs serverless (see `lambda_handler.py`, a deprioritized
proof-of-portability) and is mirrored by the mobile SDKs' offline engines.

## Contract

```python
from app.core import decide

result = decide(
    bundle={"policy": {...}, "rules": {...}, "scorecards": {...},
            "experiments": [...], "input_schema": {...}},
    payload={"customer_id": "C1", "credit_score": 720, ...},
    context={"policy_id": "credit_v1", "subject_id": "C1", "variables": {...}},
)
# -> {"outcome", "score", "rule_results", "scorecard_result", "trace",
#     "experiment_variant", "input_valid", "validation_errors", ...}
```

- **Rules/scorecards/outcome** steps are evaluated in the core.
- **Connector / action / review_gate / transform / model** steps are the *host's*
  responsibility (they need I/O) and are recorded as `deferred` — connectors are
  injected, not called by the core.
- **Input validation** (`input_schema`) is enforced in the core, so every runtime
  (server, edge, SDK host) gets the same guardrail.
- **Experiment assignment + override injection** are pure and deterministic.

The core imports only `app.logic` and `app.experiments` (both DB-free) and is
guarded by a test asserting it loads **zero** storage/db modules.

## Run the stateless service (the K8s artifact)

```bash
RULEMIND_BUNDLE_PATH=bundle.json uvicorn app.core.service:app --workers 4
# POST /decide {"payload": {...}, "policy_id": "...", "subject_id": "..."}
# GET  /health   GET /ready
```

## Validation, simulation & load

```bash
# 10k-customer correctness + A/B champion/challenger validation
python -m simulation.harness --customers 10000

# HTTP load test to >= 200 TPS against the stateless service
python -m simulation.loadtest --requests 20000 --concurrency 64 --workers 4
```

Measured on a laptop: **~33,000 decisions/sec single-core** in-process; **447 TPS**
over HTTP with 4 workers (0 errors) — throughput scales horizontally with replicas.

## Tests

- `tests/test_core_engine.py` — core semantics + DB-free import proof
- `tests/test_operators.py` — 12-operator conformance vs the shared spec
- `tests/test_champion_challenger.py` — roles, ramp, guardrails, recommendation
- `tests/test_simulation.py` — correctness-at-scale + A/B recommendation
- `tests/test_smoke.py` — end-to-end health/decide/MECE/core/promotion gate
