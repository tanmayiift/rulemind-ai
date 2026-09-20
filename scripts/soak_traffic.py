#!/usr/bin/env python3
"""Continuous decision traffic with a realistic SHAPE, for the month-long calibration run.

WHY NOT JUST RUN THE LOAD TEST ON A LOOP. A fixed arrival rate answers "where does this fall
over", which test/load/ already answers. It does not produce the data a month is for. Thresholds
derived from flat traffic are thresholds for traffic nobody sends: no diurnal trough to make the
daytime p95 look abnormal, no burst to exercise the queue, one payload shape so the sandbox
caches everything after the first minute and latency reads far better than it will in production.

So this varies the three things that actually move the numbers:

  ARRIVAL RATE   a 24h sine between --min-rps and --max-rps, so the quantiles see a real day
  BURSTS         a short multiplier at a random hour, because retry storms are how queues die
  PAYLOAD SHAPE  sampled per request across the seeded policies and value ranges, so the
                 variable cache is exercised rather than warmed once and coasted on

IT IS NOT PRODUCTION TRAFFIC. It has no user behaviour in it, no correlated failures, no tenant
that suddenly sends 50x. `docs/PRODUCTION-READINESS.md` is explicit that shadowing a real
integration beats this; run it when there is nothing to shadow, and read the resulting
thresholds as a floor rather than as truth.

    scripts/soak_traffic.py                       # run until interrupted
    scripts/soak_traffic.py --max-rps 8 --hours 24
"""
from __future__ import annotations

import argparse
import json
import math
import random
import signal
import sys
import time
import urllib.error
import urllib.request

STOP = False


def _stop(*_a):
    global STOP
    STOP = True


def post(base: str, key: str, path: str, body: dict, timeout: float = 30.0):
    req = urllib.request.Request(
        base.rstrip("/") + path, data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "x-api-key": key}, method="POST")
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            res.read()
            return res.status, time.perf_counter() - started
    except urllib.error.HTTPError as exc:
        exc.read()
        return exc.code, time.perf_counter() - started
    except (urllib.error.URLError, OSError):
        return 0, time.perf_counter() - started


def policies(base: str, key: str) -> list[str]:
    req = urllib.request.Request(base.rstrip("/") + "/api/v1/policies",
                                 headers={"x-api-key": key})
    with urllib.request.urlopen(req, timeout=30) as res:
        data = json.load(res)
    prod = [p["id"] for p in data if p.get("status") == "prod"]
    return prod or [p["id"] for p in data]


def payload() -> dict:
    """A different applicant each time.

    Fixed payloads make the sandbox's function cache look like the steady state after the first
    minute, and latency then reads far better than production ever will. Varying the values keeps
    the code path honest without changing which variables run.
    """
    return {
        "age": random.randint(18, 75),
        "bureau": {"score": random.randint(480, 830),
                   "enquiries_6m": random.randint(0, 9),
                   "delinquencies_24m": random.choice([0, 0, 0, 1, 2])},
        "bank": {"avg_balance": round(random.uniform(500, 90_000), 2),
                 "monthly_inflow": round(random.uniform(1_000, 40_000), 2)},
        "loan": {"amount": random.choice([50_000, 100_000, 250_000, 500_000]),
                 "tenure_months": random.choice([12, 24, 36, 60])},
    }


def rate_at(t: float, lo: float, hi: float, burst_hour: int, burst_mult: float) -> float:
    """Diurnal sine plus a one-hour burst. Trough at ~04:00, peak at ~16:00."""
    hour = (t / 3600.0) % 24.0
    phase = math.sin((hour - 10.0) / 24.0 * 2 * math.pi)  # -1 at 04:00, +1 at 16:00
    rate = lo + (hi - lo) * (phase + 1) / 2
    if int(hour) == burst_hour:
        rate *= burst_mult
    return max(0.1, rate)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--api-key", default="rm_live_devlocaltenantkey000000000000")
    ap.add_argument("--min-rps", type=float, default=0.4)
    ap.add_argument("--max-rps", type=float, default=4.0)
    ap.add_argument("--burst-multiple", type=float, default=6.0)
    ap.add_argument("--hours", type=float, default=0, help="0 = until interrupted")
    ap.add_argument("--report-every", type=int, default=300, help="seconds between summaries")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        ids = policies(args.base_url, args.api_key)
    except Exception as exc:
        print(f"cannot list policies at {args.base_url}: {exc}", file=sys.stderr)
        return 2
    if not ids:
        print("no policies to decide against — seed the workspace first", file=sys.stderr)
        return 2

    burst_hour = random.randint(0, 23)
    print(f"soak: {len(ids)} policies · {args.min_rps}-{args.max_rps} rps diurnal · "
          f"{args.burst_multiple}x burst at {burst_hour:02d}:00 · -> {args.base_url}", flush=True)

    started = time.time()
    window_start, sent, failed, lat = started, 0, 0, []
    deadline = started + args.hours * 3600 if args.hours else None

    while not STOP and (deadline is None or time.time() < deadline):
        rps = rate_at(time.time(), args.min_rps, args.max_rps, burst_hour, args.burst_multiple)
        status, took = post(args.base_url, args.api_key, "/api/v1/decide",
                            {"policyId": random.choice(ids), "payload": payload(),
                             "userId": f"soak-{int(time.time() * 1000)}"})
        sent += 1
        lat.append(took)
        # 429 is the limiter shedding load, which is correct behaviour and not an error.
        if status not in (200, 429):
            failed += 1

        if time.time() - window_start >= args.report_every:
            lat.sort()
            p50 = lat[len(lat) // 2] if lat else 0
            p95 = lat[int(len(lat) * 0.95)] if lat else 0
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  "
                  f"sent={sent} failed={failed} rps={rps:.2f} "
                  f"p50={p50 * 1000:.0f}ms p95={p95 * 1000:.0f}ms", flush=True)
            window_start, sent, failed, lat = time.time(), 0, 0, []

        # Poisson-ish spacing rather than a fixed sleep: real arrivals cluster, and a fixed
        # interval produces a latency distribution too clean to calibrate against.
        time.sleep(random.expovariate(rps))

    print("soak: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
