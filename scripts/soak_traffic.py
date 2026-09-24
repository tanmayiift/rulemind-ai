#!/usr/bin/env python3
"""Continuous decision traffic with a realistic SHAPE, for the month-long calibration run.

WHY NOT JUST RUN THE LOAD TEST ON A LOOP. A fixed arrival rate answers "where does this fall
over", which test/load/ already answers. It does not produce the data a month is for. Thresholds
derived from flat traffic are thresholds for traffic nobody sends: no diurnal trough to make the
daytime p95 look abnormal, no burst to exercise the queue, one payload shape so the sandbox
caches everything after the first minute and latency reads far better than it will in production.

So this varies the three things that actually move the numbers:

  ARRIVAL RATE   a 24h sine between --min-rps and --max-rps, so the quantiles see a real day
  CONTENTION     a daily window at --peak-rps, because a curve that never reaches the knee
                 produces a FLAT latency distribution and nothing worth calibrating against
  BURSTS         a short multiplier at a random hour, because retry storms are how queues die
  PAYLOAD SHAPE  sampled per request across the seeded policies and value ranges, so the
                 variable cache is exercised rather than warmed once and coasted on

THE GENERATOR USED TO BE SINGLE-THREADED AND BLOCKING, and it lied about it. One synchronous
POST per loop iteration caps the achievable rate at 1/latency -- about 22 rps here -- while the
reported `rps=` was the rate `rate_at()` had CONFIGURED, never the rate achieved. Measured:
asked for 60 rps it printed `rps=60.00` and sent 330 requests in 15 seconds, which is 22.

That matters beyond the number. The first three weeks of the calibration run produced a
per-minute p95 that ranged 47.9ms to 48.0ms across 3,860 sampled minutes -- utterly flat, and
correctly refused by scripts/calibrate_thresholds.py -- because the traffic never came close to
the knee and could not have. Raising --max-rps would have changed the log line and not the load.

So: a worker pool, and every report carries BOTH the target and what was actually achieved,
plus a shed count when the pool cannot keep up. A generator that cannot tell you it is behind
is a generator that quietly decides what your thresholds will be.

IT IS NOT PRODUCTION TRAFFIC. It has no user behaviour in it, no correlated failures, no tenant
that suddenly sends 50x. `docs/PRODUCTION-READINESS.md` is explicit that shadowing a real
integration beats this; run it when there is nothing to shadow, and read the resulting
thresholds as a floor rather than as truth.

    scripts/soak_traffic.py                       # run until interrupted
    scripts/soak_traffic.py --max-rps 8 --hours 24
    scripts/soak_traffic.py --peak-rps 65 --peak-hours 2   # daily contention window

MEASURED ON THIS MACHINE (2026-09-23, 12 services running).

A single-policy bench (qa/e2e-full/bench.py shape) saturates near 90 rps:

    concurrency   1 -> 39.6 rps, p50 24.8ms      8 -> 85.9 rps, p50  90.0ms
                  2 -> 58.2 rps, p50 32.9ms     16 -> 90.0 rps, p50 167.3ms
                  4 -> 73.8 rps, p50 55.2ms     32 -> 89.5 rps, p50 343.2ms

SOAK-SHAPED traffic -- mixed policies, varied payloads, decisions written -- does NOT, and the
difference is the number that matters here. Achieved rate against target, 2-minute windows:

    target 26 -> 23.4 rps, p50 23ms, p95 49ms, api  85% CPU
    target 30 -> 27.9 rps, p50 30ms, p95 97ms, api 124% CPU
    target 34 -> 32.1 rps, p50 26ms, p95 57ms, api  87% CPU

It flattens around 32 rps with both uvicorn workers pegged (185% of 4 VM cores). Quoting the
90 rps figure here would have set a target this workload cannot reach, and the generator would
have spent the window queueing rather than deciding. --peak-rps defaults to 40: above the
ceiling so the window genuinely saturates, close enough that the backlog stays bounded.

One measurement to distrust: an early reading of p50 370ms at 31 rps was taken immediately
after an API restart -- cold bundles, cold sandbox pool, cold caches -- and is a cold-start
artifact, not the knee. Warm the stack before believing any number from it.

WHY A WINDOW RATHER THAN A HIGHER SINE. Storage. A decision costs ~13.2 KB
(docs/PERFORMANCE.md), retention granularity is whole days, and a sine averaging 37 rps would
write ~43 GB/day. A 2-hour window at 65 rps puts 8.3% of minutes into contention -- comfortably
more than the top 5% that q0.95 reads -- for ~8.5 GB/day.
"""
from __future__ import annotations

import argparse
import json
import math
import queue
import random
import signal
import statistics
import sys
import threading
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


def rate_at(t: float, lo: float, hi: float, burst_hour: int, burst_mult: float,
            peak_rps: float = 0.0, peak_start: int = 13, peak_hours: float = 0.0) -> float:
    """Diurnal sine, a daily CONTENTION WINDOW, and a one-hour burst.

    The window is the part that makes the month worth calibrating from. A sine between 0.4 and
    4 rps never approaches the knee (measured at ~90 rps saturation, latency climbing from
    concurrency 2), so every minute looks identical and the distribution has no dynamic range --
    3,860 sampled minutes whose per-minute p95 spanned 47.9ms to 48.0ms.

    It is a WINDOW and not a taller sine because retention granularity is whole days and a
    decision costs ~13.2 KB: a sine averaging 37 rps writes ~43 GB/day, which no laptop volume
    survives. Two hours at 65 rps is 8.3% of the day -- more than the top 5% that q0.95 reads --
    for ~8.5 GB/day.
    """
    hour = (t / 3600.0) % 24.0
    phase = math.sin((hour - 10.0) / 24.0 * 2 * math.pi)  # -1 at 04:00, +1 at 16:00
    rate = lo + (hi - lo) * (phase + 1) / 2
    # BURST FIRST, WINDOW SECOND, and the order matters. Applying the burst last multiplied the
    # window: a 6x burst landing inside a 40 rps window asked for 240 rps on a system that
    # flattens at 32, which produces a queue and a timeout, not a measurement. The burst hour is
    # random, so that would have happened on roughly one day in twelve and quietly produced one
    # unusable outlier per occurrence.
    #
    # This way the window is a FLOOR the burst cannot exceed, and outside it the burst still does
    # its job -- 6x the sine peak is ~24 rps, close enough to the ceiling to exercise the queue
    # without collapsing it.
    if int(hour) == burst_hour:
        rate *= burst_mult
    if peak_rps > 0 and peak_hours > 0:
        end = peak_start + peak_hours
        # The window may wrap past midnight; compare on the 24h circle rather than a raw range.
        in_window = (peak_start <= hour < end) or (end > 24 and hour < end - 24)
        if in_window:
            rate = max(rate, peak_rps)
    return max(0.1, rate)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8080")
    ap.add_argument("--api-key", default="rm_live_devlocaltenantkey000000000000")
    ap.add_argument("--min-rps", type=float, default=0.4)
    ap.add_argument("--max-rps", type=float, default=4.0)
    ap.add_argument("--burst-multiple", type=float, default=6.0)
    # The contention window. Defaults chosen from the measured knee (saturation ~90 rps, latency
    # climbing from concurrency 2) and the storage budget (~13.2 KB per decision, whole-day
    # retention granularity). See the module docstring.
    ap.add_argument("--peak-rps", type=float, default=40.0,
                    help="rate during the daily contention window; 0 disables it")
    ap.add_argument("--peak-start", type=int, default=13, help="UTC hour the window opens")
    ap.add_argument("--peak-hours", type=float, default=2.0, help="window length in hours")
    ap.add_argument("--workers", type=int, default=16,
                    help="concurrent senders. One blocking request per loop caps the rate at "
                         "1/latency (~22 rps here) however high --peak-rps is set")
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
    peak_desc = (f"{args.peak_rps:g} rps for {args.peak_hours:g}h from {args.peak_start:02d}:00 UTC"
                 if args.peak_rps > 0 and args.peak_hours > 0 else "none")
    print(f"soak: {len(ids)} policies · {args.min_rps}-{args.max_rps} rps diurnal · "
          f"contention window {peak_desc} · {args.burst_multiple}x burst at {burst_hour:02d}:00 · "
          f"{args.workers} workers · -> {args.base_url}", flush=True)

    # A WORKER POOL, because one blocking POST per loop iteration caps the achievable rate at
    # 1/latency -- about 22 rps here -- regardless of what --max-rps says. The scheduler below
    # owns arrival timing (so the Poisson shape is preserved exactly as before); the workers
    # only carry out the request.
    work: "queue.Queue[dict]" = queue.Queue(maxsize=args.workers * 4)
    lock = threading.Lock()
    stats = {"sent": 0, "failed": 0, "shed": 0, "limited": 0, "lat": []}

    def worker() -> None:
        while True:
            item = work.get()
            if item is None:            # shutdown sentinel
                work.task_done()
                return
            status, took = post(args.base_url, args.api_key, "/api/v1/decide", item)
            with lock:
                stats["sent"] += 1
                # 429 is the limiter shedding load -- correct behaviour, not an error, and NOT a
                # decision either. Counted separately and kept OUT of the latency sample, because
                # a rejection returns in ~8ms against ~195ms for a real decision: mixing them
                # drags p50 down and makes an overloaded, mostly-rejected soak look fast.
                # Measured before this split: 57% 429s reported as p50=2ms.
                if status == 429:
                    stats["limited"] += 1
                else:
                    stats["lat"].append(took)
                    if status != 200:
                        stats["failed"] += 1
            work.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(args.workers)]
    for th in threads:
        th.start()

    started = time.time()
    window_start = started
    deadline = started + args.hours * 3600 if args.hours else None
    target_area = 0.0                    # integral of target rps over the window, for achieved-vs-target

    while not STOP and (deadline is None or time.time() < deadline):
        now = time.time()
        rps = rate_at(now, args.min_rps, args.max_rps, burst_hour, args.burst_multiple,
                      args.peak_rps, args.peak_start, args.peak_hours)
        try:
            work.put_nowait({"policyId": random.choice(ids), "payload": payload(),
                             "userId": f"soak-{int(now * 1000)}"})
        except queue.Full:
            # EVERY worker is busy. The generator cannot keep up, so the arrival rate the month
            # is actually seeing is lower than the one configured -- which is the failure this
            # rewrite exists to make visible rather than silent.
            with lock:
                stats["shed"] += 1

        gap = random.expovariate(rps)
        target_area += gap * rps         # ~1 per scheduled arrival; kept explicit for clarity
        time.sleep(gap)

        if time.time() - window_start >= args.report_every:
            elapsed = time.time() - window_start
            with lock:
                sent, failed, shed = stats["sent"], stats["failed"], stats["shed"]
                limited = stats["limited"]
                lat = sorted(stats["lat"])
                stats["sent"] = stats["failed"] = stats["shed"] = stats["limited"] = 0
                stats["lat"] = []
            achieved = sent / elapsed if elapsed else 0.0
            decided = (sent - limited) / elapsed if elapsed else 0.0
            p50 = lat[len(lat) // 2] if lat else 0
            p95 = lat[int(len(lat) * 0.95)] if lat else 0
            # BOTH numbers, always. The old line printed the configured rate and called it rps,
            # so a generator delivering 22 of a requested 60 looked perfectly healthy.
            notes = ""
            if achieved < rps * 0.9:
                notes += f"  BEHIND TARGET by {rps - achieved:.1f} rps"
            if sent and limited / sent > 0.05:
                # The soak is measuring the RATE LIMITER, not the decision engine. The same trap
                # once made test/load/ report "exactly 1499 succeeded" twice in a row.
                notes += (f"  RATE-LIMITED {100 * limited / sent:.0f}% — raise the soak tenant's "
                          f"rate_limit_rpm or lower the target")
            print(f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}  "
                  f"sent={sent} failed={failed} shed={shed} limited={limited} "
                  f"target={rps:.2f}rps achieved={achieved:.2f}rps decided={decided:.2f}rps "
                  f"p50={p50 * 1000:.0f}ms p95={p95 * 1000:.0f}ms{notes}", flush=True)
            window_start, target_area = time.time(), 0.0

    for _ in threads:
        try:
            work.put_nowait(None)
        except queue.Full:
            pass
    print("soak: stopped", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
