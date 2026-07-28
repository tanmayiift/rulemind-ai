# rulemind-core-rs

The RuleMind decision eval-core in Rust. Pure logic operates on `serde_json::Value`
with **no** Python/JS dependency, so it compiles to three targets from one source:

| Target | Build | Use |
|---|---|---|
| **Native** (rlib) | `cargo test --no-default-features` | Rust consumers, tests |
| **CPython** (PyO3) | `maturin develop --release` | The Python API's fast decode path |
| **WASM** (wasm-bindgen) | `./build-wasm.sh web` | Edge / browser / on-device |

It is the **5th engine** conforming to the shared operator contract
(`packages/shared/operators.spec.json`) — validated by
`apps/python-executor/tests/test_rust_core.py`.

## API (all three targets)

- `compare(actual, operator, expected, expected2, fieldType) -> bool` — the 12-operator contract.
- `evaluateTree(tree, variables) -> outcome` — evaluate a v2 rule tree.
- `decide(bundle, payload) -> outcome` — decide a **compiled bundle** (parsed once) against a payload. This is the hot path; on the server it powers the cached-bundle decode at ~94k decisions/sec/core.

The WASM/JS interface takes JSON **strings** (no marshalling cost); the PyO3
interface takes native Python objects; native Rust takes `serde_json::Value`.

## Build WASM

```bash
rustup target add wasm32-unknown-unknown
cargo install wasm-pack
./build-wasm.sh web        # or: bundler | nodejs
```

Produces `pkg/` (gitignored) with the optimized `.wasm`, JS glue, and `.d.ts`:

```js
import init, { compare, decide } from "./pkg/rulemind_core_rs.js";
await init();
decide(bundleJson, JSON.stringify({ score: 800 })); // -> "approve"
```

The same core thus makes decisions identically on a Kubernetes pod, in a browser,
at the edge (Cloudflare/WASM), and on-device — one implementation, no drift.
