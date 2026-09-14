# Performance and footprint

Measured, not asserted. Every number here came from a command in this repo, on one machine, and
each section says which command. Where a change is claimed, both sides were measured on the same
hardware in the same session — a before-number quoted from an older run is not a comparison.

Last updated 2026-09-14.

## Image footprint

| Image | Before | After | Change |
|---|---:|---:|---:|
| `rulemind-ai-web` | **2.54 GB** | **232 MB** | **-91%** (10.9x) |
| `rulemind-ai-api` | 923 MB | 923 MB | unchanged so far |
| `rulemind-ai-worker` | 923 MB | 923 MB | same image as api |

```bash
docker images --format '{{.Repository}}:{{.Tag}}\t{{.Size}}' | grep rulemind
```

### What the web image was carrying

`apps/web/Dockerfile` was a single stage. It did `COPY . .` — the entire repository — on top of a
full workspace `pnpm install` that includes devDependencies (`next` and `typescript` are dev
deps, so they cannot be skipped at build time), and then kept every bit of it in the shipped
layer alongside the build output. The running server uses a small fraction of that.

The fix is three stages where only the third ships, enabled by `output: "standalone"` in
`next.config.js` — Next traces the files actually reachable from the server and writes them to
`.next/standalone`. Without that trace the runner stage has no way to know which of the
workspace's packages are needed at request time, so the split is not possible.

Two details that are easy to get wrong and both fail *silently at runtime* rather than at build:

- **`outputFileTracingRoot` must point at the repo root.** pnpm symlinks into a store above
  `apps/web`, so a trace rooted at the package omits every workspace dependency and the
  container dies on `MODULE_NOT_FOUND` for `@rulemind/shared`.
- **`.next/static` is not traced into standalone.** Next expects it served from disk beside the
  server, so it is copied explicitly. Miss it and the app boots, answers 200, and serves a page
  with no CSS and no JS — which reads as an application bug, not a packaging one.

Verified beyond the size number: the container serves `/` and a deep link at 200, runs as uid
10001, and the CSS and JS the HTML references both resolve 200 (referenced is not the same as
served). `scripts/smoke_compose.sh` is 14/14 with the new image.

## Decision latency

Not yet measured in this pass. The tooling exists and is the next step:

```bash
make up                                   # stack on :8080
RM_LABEL=baseline python3 qa/e2e-full/bench.py     # p50/p99 + fast-vs-slow parity
k6 run test/load/evaluate.k6.js
k6 run test/load/saturation.k6.js
```

Nothing is claimed here until those have run on a quiet machine. A latency number taken while a
1,900-test suite and a Kubernetes cluster share the CPU measures the machine, not the engine.
