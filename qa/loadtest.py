"""Find where /decide breaks, and whether it degrades gracefully when it does.

`qa/e2e-full/bench.py` measures a healthy system at concurrency 1/4/16. This one asks the two
questions that decide whether you can put something in production:

  1. WHERE IS THE KNEE? Ramp concurrency until throughput stops rising. Throughput that keeps
     climbing means headroom; throughput that flattens while latency climbs means saturation,
     and the concurrency at which that happens is your capacity number.

  2. DOES IT DEGRADE OR COLLAPSE? Past the knee a healthy service gets slower and keeps serving.
     An unhealthy one starts erroring, and WHICH error matters: a 504
     `evaluation_budget_exceeded` is a bound working as designed, a 500 is not. This script
     separates them, because "5% errors" means completely different things in those two cases.

  3. DOES IT HOLD? A soak at the knee, to catch what a short burst hides — connection-pool
     exhaustion, memory growth, a cache that stops being a cache.

    python qa/loadtest.py                 # ramp + soak
    RM_SOAK_S=300 python qa/loadtest.py   # longer soak
"""
import json
import os
import statistics
import sys
import time
import concurrent.futures as cf

import httpx

BASE = os.getenv("RM_BASE", "http://127.0.0.1:8080")
KEY = os.getenv("RM_KEY", "rm_live_devlocaltenantkey000000000000")
POLICY = os.getenv("RM_POLICY", "policy_instant_personal_loan")
SOAK_S = int(os.getenv("RM_SOAK_S", "60"))
H = {"x-api-key": KEY, "content-type": "application/json"}

PAYLOAD = {"bureau_score": 720, "dti_ratio": 0.35, "income": 50000, "amount": 5000}


def pctl(values, p):
    s = sorted(values)
    k = min(len(s) - 1, max(0, int(round(p / 100 * len(s) + 0.5)) - 1))
    return s[k]


def one(client, i):
    started = time.perf_counter()
    try:
        r = client.post("/api/v1/decide",
                        json={"policyId": POLICY, "payload": PAYLOAD, "userId": "load-%d" % i})
        code = r.status_code
        # SHEDDING IS NOT FAILING, and conflating them is how a load test lies in both
        # directions: a correctly-shedding service looks broken, and a genuinely broken one
        # looks merely busy. Two kinds here, both deliberate responses:
        #   429 — the per-tenant rate limiter (1000 rpm by default) refusing politely
        #   504 — the evaluation budget abandoning a decision that ran too long
        # The first run of this script counted 429s as errors and reported "152 errors per
        # window" for a system that was behaving exactly as designed.
        shed = code == 429 or (code == 504 and "budget" in r.text.lower())
    except Exception:
        code, shed = 0, False
    return (time.perf_counter() - started) * 1000, code, shed


def burst(n, conc):
    with httpx.Client(base_url=BASE, headers=H, timeout=120) as c:
        for i in range(min(20, n)):          # warm: first-request costs are not the measurement
            one(c, i)
        lat, codes, sheds = [], [], 0
        t0 = time.perf_counter()
        with cf.ThreadPoolExecutor(max_workers=conc) as ex:
            for d, code, shed in ex.map(lambda i: one(c, i), range(n)):
                lat.append(d); codes.append(code); sheds += 1 if shed else 0
        wall = time.perf_counter() - t0
    ok = codes.count(200)
    return {
        "conc": conc, "n": n, "ok": ok,
        "server_errors": sum(1 for c in codes if c >= 500 and c != 504),
        "shed": sheds,
        "client_timeouts": codes.count(0),
        "p50": round(pctl(lat, 50), 1), "p95": round(pctl(lat, 95), 1),
        "p99": round(pctl(lat, 99), 1), "max": round(max(lat), 1),
        "rps": round(n / wall, 1), "wall_s": round(wall, 1),
    }


def main():
    print("target %s  policy=%s" % (BASE, POLICY))
    try:
        r = httpx.get(BASE + "/health", timeout=10)
        r.raise_for_status()
    except Exception as exc:
        print("API not reachable: %s" % exc)
        return 2

    print()
    print("RAMP — looking for the knee (throughput stops rising while latency climbs)")
    print("%-6s %-8s %-9s %-9s %-9s %-11s %-6s %-6s %-7s" %
          ("conc", "rps", "p50", "p95", "p99", "max", "5xx", "shed", "hung"))
    results = []
    best_rps, knee = 0.0, None
    for conc in (1, 4, 8, 16, 32, 64):
        r = burst(n=max(200, conc * 20), conc=conc)
        results.append(r)
        print("%-6d %-8s %-9s %-9s %-9s %-11s %-6d %-6d %-7d" %
              (r["conc"], r["rps"], "%.1fms" % r["p50"], "%.1fms" % r["p95"],
               "%.1fms" % r["p99"], "%.1fms" % r["max"], r["server_errors"],
               r["shed"], r["client_timeouts"]))
        # The knee: throughput within 5% of the best seen so far means it stopped scaling.
        if r["rps"] > best_rps * 1.05:
            best_rps = max(best_rps, r["rps"])
        elif knee is None:
            knee = r["conc"]

    knee = knee or results[-1]["conc"]
    print()
    print("knee at concurrency ~%d, peak ~%.0f rps" % (knee, best_rps))

    print()
    print("SOAK — %ds at the knee, to catch what a burst hides" % SOAK_S)
    deadline = time.time() + SOAK_S
    windows = []
    with httpx.Client(base_url=BASE, headers=H, timeout=120) as c:
        i = 0
        while time.time() < deadline:
            lat, errs = [], 0
            t0 = time.perf_counter()
            with cf.ThreadPoolExecutor(max_workers=knee) as ex:
                for d, code, shed in ex.map(lambda k: one(c, k), range(i, i + knee * 10)):
                    lat.append(d)
                    # Shed responses are not errors. See `one()`.
                    errs += 0 if (code == 200 or shed) else 1
            i += knee * 10
            windows.append({"p50": pctl(lat, 50), "p95": pctl(lat, 95), "errors": errs,
                            "rps": len(lat) / (time.perf_counter() - t0)})
    for n, w in enumerate(windows):
        print("  window %-2d p50=%-8s p95=%-8s rps=%-7s errors=%d" %
              (n + 1, "%.1fms" % w["p50"], "%.1fms" % w["p95"], "%.0f" % w["rps"], w["errors"]))

    # Drift across the soak is the finding. A service that is 40% slower at the end than the
    # start is leaking something, and a burst test would have called it healthy.
    verdict = 0
    if len(windows) >= 2:
        first, last = windows[0]["p50"], windows[-1]["p50"]
        drift = (last - first) / first * 100 if first else 0
        print()
        print("  p50 drift across soak: %+.1f%% (%.1fms -> %.1fms)" % (drift, first, last))
        if drift > 30:
            print("  !! latency grew more than 30%% during a steady load — something is leaking")
            verdict = 1
    total_errors = sum(w["errors"] for w in windows)
    print("  errors during soak: %d" % total_errors)
    if total_errors:
        verdict = 1

    print()
    print(json.dumps({"ramp": results, "knee": knee, "peak_rps": best_rps,
                      "soak_windows": windows}, default=float))
    return verdict


if __name__ == "__main__":
    sys.exit(main())
