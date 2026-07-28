#!/usr/bin/env bash
# Build the RuleMind eval-core to WebAssembly (edge / browser / on-device).
# Requires: rustup, the wasm32 target, and wasm-pack.
#   rustup target add wasm32-unknown-unknown
#   cargo install wasm-pack
set -euo pipefail
cd "$(dirname "$0")"

TARGET="${1:-web}"   # web | bundler | nodejs
OUT="${2:-pkg}"

echo "Building rulemind-core-rs to WASM (target=$TARGET)…"
wasm-pack build --target "$TARGET" --out-dir "$OUT" -- --no-default-features --features wasm
echo "Done → $OUT/  (import { compare, evaluateTree, decide } from './$OUT')"
