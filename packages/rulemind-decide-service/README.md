# rulemind-decide-service

A **standalone Rust decision service** — the dedicated high-throughput `/decide` hot path for
RuleMind. It serves a compiled bundle over HTTP and evaluates a policy's variables + rules
entirely in native Rust: **no Python, no database on the request path**.

It is **use-case agnostic** — it evaluates whatever decisioning bundle it is given (lending,
fraud, insurance eligibility, pricing, content moderation, …). Nothing in it is domain-specific.

## Architecture — where it fits

RuleMind's FastAPI control plane keeps everything stateful and complex: authoring, admin, and the
policy steps that need I/O or Python (connectors, scorecards, ML models, human review). This
service takes over the **pure-compute rule decisions** — the vast majority of production traffic —
and absorbs them at very high QPS.

```
            author + admin (stateful)              decide (hot path, stateless)
  ┌───────────────────────────────────┐   compile   ┌─────────────────────────────┐
  │  FastAPI control plane (Python)   │ ─ bundle ──▶ │  rulemind-decide-service    │
  │  connectors · scorecards · review │             │  (Rust: variables + rules)  │
  └───────────────────────────────────┘             └─────────────────────────────┘
```

It consumes the **same compiled bundle** the Kotlin/Dart SDKs run on-device, so it is covered by
the identical cross-engine conformance spec (`packages/shared/operators.spec.json`) — the Rust
decisions cannot silently drift from the Python source of truth.

**Scope:** rule-based policies (compiled variables + rule trees) — exactly the subset the fast
path already serves. Policies that need connectors, scorecards, decision tables, or human review
stay on the FastAPI executor.

## Performance

Single core, in-process: **~575,000 decisions/sec (~1.7 µs/decision)** — see the
`decide_throughput_far_exceeds_1000_tps` conformance test. That is ~575× the 1000 TPS / <100 ms
target on one core, before horizontal scaling.

## Run

```bash
# 1. Export a compiled bundle from the control plane (the /sdk/v1/blocks/{policy} block,
#    or a compiled bundle) to a JSON file.
# 2. Run the service pointed at it:
RULEMIND_BUNDLE_PATH=./bundle.json RULEMIND_DECIDE_ADDR=0.0.0.0:8090 \
  cargo run --release

# Decide:
curl -s localhost:8090/decide -H 'content-type: application/json' \
  -d '{"payload": {"amount": 3000, "income": 10000}}'
# -> {"outcome":"approve","latency_us":33,"bundleVersion":"..."}
```

`POST /decide` accepts `{"payload": {...}}` (computes variables) or `{"variables": {...}}`
(pre-computed). `GET /healthz` / `GET /readyz` for probes.

## Config

| Env | Default | Description |
| --- | --- | --- |
| `RULEMIND_BUNDLE_PATH` | _(required)_ | Path to the compiled bundle JSON |
| `RULEMIND_DECIDE_ADDR` | `0.0.0.0:8090` | Listen address |

## Test / build

```bash
cargo test  --manifest-path packages/rulemind-core-rs/Cargo.toml --no-default-features   # conformance + throughput
cargo build --release                                                                    # this crate
docker build -f packages/rulemind-decide-service/Dockerfile -t rulemind-decide .          # container
```
