#!/usr/bin/env python3
"""Every alert rule must query a metric something can actually produce.

A rule whose expression names a metric Prometheus has no series for is not a rule, it is a
comment. It evaluates to empty forever and can never fire, while `/alerts` renders it green and
indistinguishable from a healthy one -- so the louder the dashboard, the more confident you are
about a thing nobody is watching.

This is not hypothetical. `RuleMindPagingIsFailing` -- the one rule whose entire job is to catch
"alerts are firing and NOT arriving" -- reads `alertmanager_notifications_failed_total`, and
`observability/prometheus.yml` had no scrape job for Alertmanager at all. The `alerting:` block
points Prometheus AT Alertmanager, which LOOKS like the two are wired together, but it only says
where to SEND alerts; it scrapes nothing. The metric therefore had no series, the rule could
never fire, and this stack logged 440 genuine delivery failures underneath it.

**Two reasons a metric has no series, and conflating them makes the check useless:**

*Undefined* -- the name appears nowhere in the source, so nothing can ever emit it. That is the
real failure, and it is what this exits non-zero on.

*Defined but not yet emitted* -- a labelled `Counter` only creates its series on the first
`.inc()`. `rulemind_bundle_syncs_total` is defined at `app/main.py:184` and simply has had no
device sync in this stack. The rule is UNTESTED, not dead. Reported as a warning, because
"nothing has ever exercised this code path" is worth knowing and is not a defect.

**One limitation, stated because it bit the mutation test.** "Has no series" is asked with an
instant query, so it inherits Prometheus's 5-minute staleness window: a metric that WAS being
scraped and then stops still answers for five minutes. Removing the alertmanager scrape job and
re-running this 18 seconds later therefore reports nothing wrong. That is the right sensitivity
-- the question is "can this rule fire right now", and for five minutes after a scrape job
disappears the answer is genuinely yes -- but it means this check is not a substitute for
watching `up{}` per target, and a mutation test of it has to wait out the window.

Exit codes: 0 = every rule is backed, 1 = at least one rule queries an undefined metric,
2 = Prometheus could not be reached (unknown, not healthy).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

# PromQL function and keyword names look exactly like metric names to a regex. Listing the ones
# these rules actually use is cheaper and far less brittle than parsing PromQL properly.
RESERVED = {
    "rate", "irate", "increase", "sum", "avg", "min", "max", "count", "count_values", "by",
    "without", "on", "ignoring", "group_left", "group_right", "and", "or", "unless", "offset",
    "bool", "vector", "scalar", "time", "abs", "ceil", "floor", "round", "clamp_max",
    "clamp_min", "delta", "idelta", "deriv", "predict_linear", "histogram_quantile", "quantile",
    "quantile_over_time", "avg_over_time", "max_over_time", "min_over_time", "sum_over_time",
    "count_over_time", "last_over_time", "stddev", "stdvar", "topk", "bottomk", "absent",
    "absent_over_time", "changes", "resets", "label_replace", "label_join", "e", "inf", "nan",
    "start", "end", "group", "ln", "log2", "log10", "exp", "sqrt", "sgn",
}

# A bare identifier NOT followed by "(" -- i.e. not a function call.
NAME = re.compile(r"\b([a-zA-Z_:][a-zA-Z0-9_:]*)\b(?!\s*\()")
# Label lists that sit OUTSIDE any braces: by (le), without (source), on (tenant). Easy to miss,
# and without this every histogram_quantile(... by (le) ...) reports `le` as a missing metric.
MODIFIER = re.compile(r"\b(?:by|without|on|ignoring|group_left|group_right)\s*\(([^)]*)\)")
BRACES = re.compile(r"\{[^}]*\}")

SOURCE_DIRS = ("apps/python-executor/app", "packages", "observability")
SOURCE_EXTS = (".py", ".ts", ".rs", ".kt", ".dart", ".go")
PRUNE = {"node_modules", "__pycache__", "target", ".venv", "build", "dist", ".next"}
# A metric name only counts as "defined" if it is being constructed or registered.
CONSTRUCTORS = ("Counter(", "Gauge(", "Histogram(", "Summary(", "get_or_create(",
                "new_counter", "register_int_counter", "IntCounter", "register_counter")
QUOTED = re.compile(r"""['"]([a-zA-Z_:][a-zA-Z0-9_:]*)['"]""")


def metric_names_in(expr: str) -> set:
    """Metric names referenced by a PromQL expression, with labels and functions removed."""
    names = {m for m in NAME.findall(expr) if m not in RESERVED and not m.isdigit()}
    for block in BRACES.findall(expr):
        names -= set(NAME.findall(block))
    for block in MODIFIER.findall(expr):
        names -= set(NAME.findall(block))
    return names


def defined_metric_names(root: str) -> set:
    """Metric names the source constructs. Textual, on purpose: we only need to know that
    something COULD emit the metric, not to import the module that does."""
    names = set()
    for sub in SOURCE_DIRS:
        base = os.path.join(root, sub)
        if not os.path.isdir(base):
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in PRUNE]
            for fn in filenames:
                if not fn.endswith(SOURCE_EXTS):
                    continue
                try:
                    with open(os.path.join(dirpath, fn), encoding="utf-8", errors="ignore") as fh:
                        text = fh.read()
                except OSError:
                    continue
                for line in text.splitlines():
                    if any(c in line for c in CONSTRUCTORS):
                        names.update(QUOTED.findall(line))
    # A prometheus_client Counter named "x" is exposed as "x_total"; the rules use the exposed
    # spelling, so record both or every Counter reads as undefined.
    return names | {n + "_total" for n in names}


def _get(url: str, timeout: int = 10):
    with urllib.request.urlopen(url, timeout=timeout) as fh:
        return json.load(fh)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prom", default=os.environ.get("RULEMIND_PROM_URL", "http://127.0.0.1:9090"))
    ap.add_argument("--root", default=os.environ.get("RM_REPO_ROOT", os.getcwd()))
    args = ap.parse_args()

    try:
        rules = _get(args.prom + "/api/v1/rules")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print("UNKNOWN: could not read Prometheus rules from %s (%s)" % (args.prom, exc))
        return 2

    defined = defined_metric_names(args.root)
    seen: dict = {}

    def has_series(metric: str) -> bool:
        if metric not in seen:
            url = args.prom + "/api/v1/query?" + urllib.parse.urlencode(
                {"query": "count(%s)" % metric})
            try:
                seen[metric] = bool(_get(url).get("data", {}).get("result"))
            except Exception:
                seen[metric] = True  # cannot tell -> do not cry wolf
        return seen[metric]

    undefined, unemitted, checked = [], [], 0
    for group in rules.get("data", {}).get("groups", []):
        for rule in group.get("rules", []):
            if rule.get("type") != "alerting":
                continue
            checked += 1
            # `vector(1)` literals (the Watchdog) reference no metric at all, by design.
            for metric in sorted(metric_names_in(rule.get("query", ""))):
                if has_series(metric):
                    continue
                entry = "%s -> %s" % (rule.get("name", "?"), metric)
                (unemitted if metric in defined else undefined).append(entry)

    for entry in undefined:
        print("UNDEFINED %s" % entry)
    for entry in unemitted:
        print("UNEMITTED %s" % entry)
    print("CHECKED %d" % checked)
    return 1 if undefined else 0


if __name__ == "__main__":
    sys.exit(main())
