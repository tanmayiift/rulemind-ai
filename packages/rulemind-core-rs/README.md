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
- `evaluateTreeTraced(tree, variables) -> trace` — the same evaluation plus the per-condition /
  per-group trace (`conditions[] · groupResults[] · passed · outcome · logic`), the shape
  `app/logic.py::evaluate_rule_tree` returns and the Kotlin/Dart `RuleEvaluator`s emit. This is
  what lets the Rust engine assert the passed-condition **count** and the full per-condition
  bitstring in `packages/shared/large-policy.spec.json`, not just the final outcome.
- `decide(bundle, payload) -> outcome` — decide a **compiled bundle** (parsed once) against a payload. This is the hot path; on the server it powers the cached-bundle decode at ~94k decisions/sec/core.

The WASM/JS interface takes JSON **strings** (no marshalling cost); the PyO3
interface takes native Python objects; native Rust takes `serde_json::Value`.

### Limits shared with the other four engines

| Limit | Value | Behaviour past it |
|---|---|---|
| `MAX_RULE_TREE_DEPTH` | 200 | **Error**, not a false condition — `Err(EvalError::DepthExceeded)` in Rust, `ValueError` from PyO3, a thrown JS `Error` from WASM, and `422` from `rulemind-decide-service`. Rule trees are author-supplied, and an unbounded recursion in Rust is a process *abort*, not a catchable exception. |
| `REGEX_INPUT_MAX` | 4096 chars | The `regex` operator fails **closed** (false) without applying the pattern, exactly as `app/logic.py::_safe_regex_search` does. Measured in code points, matching Python's `len()`. |

Both mirror `app/logic.py`; the Rust `regex` crate is linear-time, so the input cap is about
behavioural parity and CPU proportionality, not ReDoS.

> **Caveat:** `serde_json`'s parser refuses input nested deeper than 128, so a tree *parsed*
> from JSON can never reach the 200 bound — it fails at parse time instead. The guard covers
> `Value`s built programmatically (`CompiledBundle::from_value`, PyO3, native callers). This is
> also a live divergence: a legitimately-authored 150-deep tree evaluates on the Python, TS,
> Kotlin and Dart engines but will not load here.

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
