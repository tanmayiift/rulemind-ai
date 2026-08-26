"""Minimal E2E harness: records structured results, prints a live log, emits JSON."""
from __future__ import annotations
import json, os, time, traceback, statistics
from typing import Any, Dict, List, Optional
import httpx

BASE = os.getenv("RM_BASE", "http://127.0.0.1:8080")
KEY = os.getenv("RM_KEY", "rm_live_devlocaltenantkey000000000000")
OUT = os.getenv("RM_OUT", "results.json")

class Result:
    def __init__(self, cid, name, status, detail="", data=None, ms=None):
        self.cid, self.name, self.status, self.detail, self.data, self.ms = cid, name, status, detail, data, ms
    def as_dict(self):
        return {"id": self.cid, "name": self.name, "status": self.status,
                "detail": self.detail, "data": self.data, "ms": self.ms}

class Runner:
    def __init__(self):
        self.results: List[Result] = []
        self.client = httpx.Client(base_url=BASE, headers={"x-api-key": KEY}, timeout=60.0)
        self.raw = httpx.Client(base_url=BASE, timeout=60.0)  # no auth header

    # ---- http helpers ----
    def get(self, p, **kw): return self.client.get(p, **kw)
    def post(self, p, **kw): return self.client.post(p, **kw)
    def put(self, p, **kw): return self.client.put(p, **kw)
    def delete(self, p, **kw): return self.client.delete(p, **kw)

    # ---- assertion recorder ----
    def case(self, cid: str, name: str):
        """Context manager: `with r.case('1.1','desc') as c: c.ok(cond, detail)`"""
        return _Case(self, cid, name)

    def record(self, r: Result):
        self.results.append(r)
        icon = {"pass": "✅", "fail": "❌", "warn": "⚠️ ", "skip": "⏭ ", "block": "⛔"}.get(r.status, "?")
        ms = f" [{r.ms:.0f}ms]" if r.ms is not None else ""
        print(f"{icon} {r.cid:<6} {r.name}{ms}" + (f"\n        → {r.detail}" if r.detail else ""), flush=True)

    def dump(self):
        summary = {}
        for r in self.results:
            summary[r.status] = summary.get(r.status, 0) + 1
        payload = {"summary": summary, "results": [r.as_dict() for r in self.results]}
        with open(OUT, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        print("\n" + "=" * 70)
        print("SUMMARY:", " ".join(f"{k}={v}" for k, v in sorted(summary.items())))
        print("=" * 70)
        return payload

class _Case:
    def __init__(self, runner, cid, name):
        self.r, self.cid, self.name = runner, cid, name
        self.status, self.detail, self.data = "pass", "", None
        self.t0 = None
    def __enter__(self):
        self.t0 = time.perf_counter(); return self
    def ok(self, cond, detail=""):
        if not cond and self.status == "pass":
            self.status, self.detail = "fail", detail
        return cond
    def warn(self, detail):
        if self.status == "pass": self.status, self.detail = "warn", detail
    def fail(self, detail): self.status, self.detail = "fail", detail
    def skip(self, detail): self.status, self.detail = "skip", detail
    def block(self, detail): self.status, self.detail = "block", detail
    def info(self, data): self.data = data
    def __exit__(self, et, ev, tb):
        ms = (time.perf_counter() - self.t0) * 1000
        if et is not None:
            self.status = "fail"
            self.detail = f"{et.__name__}: {ev}"[:400]
            self.r.record(Result(self.cid, self.name, self.status, self.detail, self.data, ms))
            return True  # swallow, keep suite running
        self.r.record(Result(self.cid, self.name, self.status, self.detail, self.data, ms))
        return False

def pct(vals, p):
    if not vals: return 0.0
    s = sorted(vals); k = max(0, min(len(s) - 1, int(round((p / 100) * len(s) + 0.5)) - 1))
    return s[k]
