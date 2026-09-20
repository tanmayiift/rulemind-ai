#!/usr/bin/env python3
"""Derive alert thresholds from traffic this system has actually served.

WHY THIS EXISTS. Every number in observability/alerts.yml was measured on one laptop, and the
file says so: "RE-DERIVE THESE FROM YOUR OWN TRAFFIC after the first month. The table above is a
laptop, and a threshold calibrated against someone else's hardware is still a guess."

That instruction has sat there unexecuted because re-deriving meant opening Prometheus, writing
quantile queries by hand, and deciding what multiple of normal counts as abnormal -- an
afternoon's work that is easy to postpone and easy to get subtly wrong. This makes it one
command, so the excuse for running on a stranger's numbers is gone.

    scripts/calibrate_thresholds.py --window 7d
    scripts/calibrate_thresholds.py --window 30d --apply    # rewrite alerts.yml in place

WHAT IT DOES NOT DO. It does not invent an SLO. A threshold is a statement about what you
consider unacceptable, and that is a product decision; this only tells you where your system
actually sits so the statement is informed. The two are different and conflating them is how an
alert ends up firing nightly at 3am for behaviour that was always normal.

THE METHOD, and why it is not simply "p95 of the window":

  * A ceiling set at observed p95 fires on half your bad-but-normal days. The defaults here take
    a HEADROOM MULTIPLE over a high quantile, so the alert means "materially worse than this
    system's own bad days" rather than "slightly worse than typical".
  * The quantile is computed over the window's own distribution of per-minute p95s, not over
    request latencies -- you are choosing a threshold against the shape of the metric the alert
    actually evaluates, which is what makes the two comparable.
  * Warning and critical are derived from the SAME measurement with different multiples, so they
    cannot cross. Hand-set pairs drift into a critical below its warning, and then the critical
    never fires because the inhibit rule suppresses it.

REFUSES TO GUESS. If the window holds too few samples the answer is "not enough data", not a
number derived from six points. A threshold computed from a quiet afternoon is worse than the
laptop number it replaced, because it carries the authority of having been "measured".
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import sys
import urllib.parse
import urllib.request

PROM = "http://127.0.0.1:9090"
ALERTS = pathlib.Path(__file__).resolve().parents[1] / "observability" / "alerts.yml"

# Minimum samples before a number is trustworthy. A 7d window at 1-minute resolution is ~10k
# points; anything under a few hundred means the system was mostly idle and the quantile is
# describing noise.
MIN_SAMPLES = 200


def query(prom: str, expr: str) -> list[float]:
    url = prom.rstrip("/") + "/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=60) as res:
        body = json.load(res)
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus: {body.get('error')}")
    out = []
    for series in body["data"]["result"]:
        try:
            out.append(float(series["value"][1]))
        except (KeyError, IndexError, ValueError):
            continue
    return out


def scalar(prom: str, expr: str):
    vals = query(prom, expr)
    return vals[0] if vals else None


def measure(prom: str, window: str) -> dict:
    """Each entry: what the alert evaluates, and the same metric's own distribution."""
    # Per-minute p95 decision latency, then the 95th percentile OF THAT over the window --
    # i.e. "on the worst 5% of minutes, p95 was at least this".
    lat_expr = (
        f'quantile_over_time(0.95, '
        f'histogram_quantile(0.95, sum(rate(rulemind_decision_latency_seconds_bucket[5m])) by (le))'
        f'[{window}:1m])'
    )
    lat_median = (
        f'quantile_over_time(0.50, '
        f'histogram_quantile(0.95, sum(rate(rulemind_decision_latency_seconds_bucket[5m])) by (le))'
        f'[{window}:1m])'
    )
    err_expr = (
        f'quantile_over_time(0.95, '
        f'(sum(rate(rulemind_decisions_total{{status="error"}}[5m])) / '
        f'clamp_min(sum(rate(rulemind_decisions_total[5m])), 0.001))[{window}:1m])'
    )
    samples = f'count_over_time(sum(rate(rulemind_decisions_total[5m]))[{window}:1m])'

    return {
        "samples": scalar(prom, samples),
        "latency_p95_of_p95": scalar(prom, lat_expr),
        "latency_median_of_p95": scalar(prom, lat_median),
        "error_rate_p95": scalar(prom, err_expr),
    }


def derive(m: dict, warn_mult: float, crit_mult: float) -> dict:
    lat = m.get("latency_p95_of_p95")
    if lat is None:
        return {}
    return {
        "latency_warning_s": round(lat * warn_mult, 3),
        "latency_critical_s": round(lat * crit_mult, 3),
    }


def current_threshold(path: pathlib.Path, alert: str) -> str | None:
    """The comparison inside ONE named rule.

    A file-wide regex is wrong here and reported `0, 0` on the first run: alerts.yml opens with
    `rate(alertmanager_notifications_failed_total[10m]) > 0` and a decisions-stopped rule
    comparing to 0, so the first two matches in the file belong to neither latency rule. Scoping
    to the rule block is the same thing apply_to_alerts does, for the same reason.
    """
    text = path.read_text()
    i = text.find(f"- alert: {alert}")
    if i == -1:
        return None
    end = text.find("- alert:", i + 10)
    block = text[i:end if end != -1 else len(text)]
    m = re.search(r"\)\s*>\s*([0-9.]+)", block)
    return m.group(1) if m else None


def apply_to_alerts(path: pathlib.Path, warn_s: float, crit_s: float) -> bool:
    """Rewrite only the two latency comparisons, by their surrounding rule name.

    Deliberately a targeted substitution and not a YAML round-trip: this file is 240 lines of
    load-bearing comments explaining why each number is what it is, and a dump/reload would
    silently discard every one of them.
    """
    text = path.read_text()
    out = text
    for alert, value in (("RuleMindDecisionLatencyHigh", warn_s),
                         ("RuleMindDecisionLatencyCritical", crit_s)):
        i = out.find(f"- alert: {alert}")
        if i == -1:
            print(f"  ! {alert} not found; left alone", file=sys.stderr)
            continue
        end = out.find("- alert:", i + 10)
        block = out[i:end if end != -1 else len(out)]
        new_block, n = re.subn(r"\)\s*>\s*[0-9.]+", f") > {value}", block, count=1)
        if n == 0:
            print(f"  ! no comparison found in {alert}; left alone", file=sys.stderr)
            continue
        out = out[:i] + new_block + (out[end:] if end != -1 else "")
    if out == text:
        return False
    path.write_text(out)
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prometheus", default=PROM)
    ap.add_argument("--window", default="7d", help="e.g. 24h, 7d, 30d")
    ap.add_argument("--warning-multiple", type=float, default=1.5)
    ap.add_argument("--critical-multiple", type=float, default=4.0)
    ap.add_argument("--min-samples", type=int, default=MIN_SAMPLES,
                    help="refuse below this many sampled minutes (default %(default)s). Lower it "
                         "only when calibrating from a deliberate load test, where you know the "
                         "traffic is representative because you generated it.")
    ap.add_argument("--apply", action="store_true", help="rewrite observability/alerts.yml")
    args = ap.parse_args()

    try:
        m = measure(args.prometheus, args.window)
    except Exception as exc:
        print(f"could not query {args.prometheus}: {exc}", file=sys.stderr)
        print("start it with: make observability", file=sys.stderr)
        return 2

    samples = m.get("samples") or 0
    print(f"\n  window {args.window} · {int(samples)} sampled minutes\n")

    if samples < args.min_samples:
        print(f"  NOT ENOUGH DATA — {int(samples)} minutes of traffic, need {args.min_samples}.")
        print("  A threshold from a quiet afternoon is worse than the laptop number it would")
        print("  replace, because it carries the authority of having been measured.\n")
        return 1

    lat_med = m.get("latency_median_of_p95")
    lat_p95 = m.get("latency_p95_of_p95")
    err = m.get("error_rate_p95")
    if lat_med is not None:
        print(f"  decision p95, typical minute    {lat_med * 1000:8.1f} ms")
    if lat_p95 is not None:
        print(f"  decision p95, worst 5% of mins  {lat_p95 * 1000:8.1f} ms")
    if err is not None:
        print(f"  error rate, worst 5% of mins    {err * 100:8.2f} %")

    d = derive(m, args.warning_multiple, args.critical_multiple)
    if not d:
        print("\n  no latency histogram found — is the API being scraped?\n")
        return 1

    print(f"\n  proposed, from YOUR traffic:")
    print(f"    RuleMindDecisionLatencyHigh      > {d['latency_warning_s']}   "
          f"({args.warning_multiple}x the worst-5% minute)")
    print(f"    RuleMindDecisionLatencyCritical  > {d['latency_critical_s']}   "
          f"({args.critical_multiple}x)")
    print(f"\n  current in alerts.yml:")
    for alert in ("RuleMindDecisionLatencyHigh", "RuleMindDecisionLatencyCritical"):
        print(f"    {alert:<32} > {current_threshold(ALERTS, alert) or '(not found)'}")
    print()

    if args.apply:
        if apply_to_alerts(ALERTS, d["latency_warning_s"], d["latency_critical_s"]):
            print(f"  wrote {ALERTS}")
            print("  review the diff, then: docker compose --profile observability "
                  "restart prometheus\n")
        else:
            print("  nothing changed\n")
    else:
        print("  re-run with --apply to write these into observability/alerts.yml\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
