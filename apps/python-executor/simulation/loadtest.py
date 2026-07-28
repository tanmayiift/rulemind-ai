"""HTTP load test for the stateless decision service (app.core.service).

Spins up the microservice under uvicorn (multiple workers, no DB), fires a
concurrent load, and reports achieved throughput (TPS) + latency percentiles.
This models the primary Kubernetes deployment: N stateless replicas serving
POST /decide. Asserts the >= 200 TPS scalability target.

Run:  python -m simulation.loadtest --requests 20000 --concurrency 64
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List

APP_ROOT = Path(__file__).resolve().parents[1]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

import httpx  # noqa: E402

from simulation.harness import build_credit_bundle, build_deep_bundle, generate_customers  # noqa: E402


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(len(ordered) * pct))
    return ordered[idx]


async def _run_load(base_url: str, payloads: List[dict], total: int, concurrency: int):
    latencies: List[float] = []
    errors = 0
    sem = asyncio.Semaphore(concurrency)
    limits = httpx.Limits(max_connections=concurrency, max_keepalive_connections=concurrency)

    async with httpx.AsyncClient(base_url=base_url, limits=limits, timeout=10.0) as client:
        async def one(index: int):
            nonlocal errors
            payload = payloads[index % len(payloads)]
            async with sem:
                start = time.perf_counter()
                try:
                    resp = await client.post("/decide", json={"payload": payload})
                    if resp.status_code != 200:
                        errors += 1
                except Exception:
                    errors += 1
                    return
                latencies.append((time.perf_counter() - start) * 1000)

        wall_start = time.perf_counter()
        await asyncio.gather(*(one(i) for i in range(total)))
        wall = time.perf_counter() - wall_start

    return wall, latencies, errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Load test for the stateless decision service")
    parser.add_argument("--requests", type=int, default=20_000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--target-tps", type=float, default=200.0)
    parser.add_argument("--conditions", type=int, default=0, help="If >0, use a policy that runs this many sequential conditions per case.")
    args = parser.parse_args()

    # Write the bundle for the service to load at startup.
    bundle = build_deep_bundle(args.conditions) if args.conditions > 0 else build_credit_bundle()
    if args.conditions > 0:
        print(f"Policy: {args.conditions} sequential conditions evaluated per case.")
    bundle_path = Path(APP_ROOT) / ".runtime" / "loadtest_bundle.json"
    bundle_path.parent.mkdir(exist_ok=True)
    bundle_path.write_text(json.dumps(bundle))

    payloads = generate_customers(500, seed=7)  # a pool of realistic payloads
    port = _free_port()
    env = {**os.environ, "RULEMIND_BUNDLE_PATH": str(bundle_path)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.core.service:app",
         "--host", "127.0.0.1", "--port", str(port), "--workers", str(args.workers), "--log-level", "warning"],
        cwd=str(APP_ROOT), env=env,
    )
    base_url = f"http://127.0.0.1:{port}"

    try:
        # Wait for readiness.
        deadline = time.time() + 30
        ready = False
        while time.time() < deadline:
            try:
                if httpx.get(f"{base_url}/ready", timeout=1.0).status_code == 200:
                    ready = True
                    break
            except Exception:
                time.sleep(0.3)
        if not ready:
            print("Service failed to become ready.")
            return 1

        print(f"Load: {args.requests:,} requests @ concurrency {args.concurrency}, {args.workers} uvicorn workers")
        # Warm up.
        asyncio.run(_run_load(base_url, payloads, min(500, args.requests), args.concurrency))
        wall, latencies, errors = asyncio.run(_run_load(base_url, payloads, args.requests, args.concurrency))

        completed = len(latencies)
        tps = completed / wall if wall else 0
        print("\n=== Load test results (stateless core over HTTP) ===")
        print(f"  Completed:   {completed:,} / {args.requests:,}  (errors: {errors})")
        print(f"  Wall time:   {wall:.2f}s")
        print(f"  Throughput:  {tps:,.0f} TPS")
        print(f"  Latency p50: {_percentile(latencies, 0.50):.2f}ms")
        print(f"  Latency p95: {_percentile(latencies, 0.95):.2f}ms")
        print(f"  Latency p99: {_percentile(latencies, 0.99):.2f}ms")
        passed = tps >= args.target_tps and errors == 0
        print(f"  TARGET >= {args.target_tps:.0f} TPS: {'PASS' if passed else 'FAIL'}")
        return 0 if passed else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())
