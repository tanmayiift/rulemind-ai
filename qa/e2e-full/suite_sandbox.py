"""Suite 3.2/3.3 — Python variable sandbox: escape attempts + resource limits.

Variables are functions registered with @variable(source=...) taking
(payload, variables, apis). A test is only meaningful if the BENIGN control
executes successfully first — otherwise a schema error masquerades as 'blocked'.
"""
import time, json
from harness import Runner

DECOR = '@variable(source="custom")\ndef probe(payload, variables, apis):\n'

def _draft(r, body, timeout=30):
    return r.post("/api/v1/variables/test-draft",
                  json={"source_id": "custom", "code": body, "payload": {"a": 2, "b": 40}},
                  timeout=timeout)

def _result(resp):
    if resp.status_code != 200:
        return None, f"HTTP {resp.status_code}: {resp.text[:200]}"
    b = resp.json().get("result", {})
    return b.get("value"), b.get("error")

ESCAPES = [
    ("import os",              DECOR + "    import os\n    return os.listdir('/')"),
    ("__import__ builtin",     DECOR + "    return __import__('os').listdir('/')"),
    ("open() file read",       DECOR + "    return open('/etc/passwd').read()"),
    ("subclass walk to Popen", DECOR + "    return [c.__name__ for c in ().__class__.__bases__[0].__subclasses__()]"),
    ("eval()",                 DECOR + "    return eval(\"1+1\")"),
    ("exec()",                 DECOR + "    return exec('x=1')"),
    ("globals() access",       DECOR + "    return list(globals().keys())"),
    ("func __globals__",       DECOR + "    return probe.__globals__"),
    ("class __mro__ escape",   DECOR + "    return type(payload).__mro__"),
    ("str.format leak",        DECOR + "    return '{0.__class__}'.format(payload)"),
    ("import via importlib",   DECOR + "    import importlib\n    return importlib.import_module('os')"),
    ("read env",               DECOR + "    import os\n    return os.environ.get('RULEMIND_CONFIG_KEY')"),
    ("socket egress",          DECOR + "    import socket\n    return socket.gethostbyname('example.com')"),
    ("subprocess",             DECOR + "    import subprocess\n    return subprocess.check_output(['id'])"),
    ("write file",             DECOR + "    return open('/tmp/rm_pwned','w').write('x')"),
]

def run(r: Runner):
    # ---- CONTROL: benign code must actually work, else every 'block' is a false pass
    with r.case("3.1", "CONTROL — benign variable executes and returns correct value") as c:
        val, err = _result(_draft(r, DECOR + "    return payload.get('a',0) + payload.get('b',0)"))
        c.info({"value": val, "error": err})
        c.ok(err is None, f"benign code errored: {err}")
        c.ok(val == 42, f"expected 42, got {val!r}")

    control_ok = r.results[-1].status == "pass"
    if not control_ok:
        with r.case("3.2", "Sandbox escape matrix") as c:
            c.block("control case failed — escape results would be meaningless")
        return

    for i, (label, code) in enumerate(ESCAPES, start=1):
        with r.case(f"3.2.{i}", f"Sandbox blocks: {label}") as c:
            resp = _draft(r, code)
            val, err = _result(resp)
            c.info({"value": str(val)[:220], "error": str(err)[:220]})
            if err:
                c.ok(True)                      # rejected with an error → blocked
            elif val is None:
                c.warn("no error but value is None — blocked silently (weak signal)")
            else:
                c.fail(f"NOT BLOCKED — returned {str(val)[:200]!r}")

    with r.case("3.2.16", "Secret not readable from sandbox even if import slips") as c:
        val, err = _result(_draft(r, DECOR + "    return str(payload)"))
        c.ok(err is None, f"{err}")

    # ---- resource limits
    with r.case("3.3", "Infinite loop is terminated by timeout") as c:
        t0 = time.perf_counter()
        resp = _draft(r, DECOR + "    \n    while True:\n        pass\n", timeout=60)
        el = (time.perf_counter() - t0) * 1000
        val, err = _result(resp)
        c.info({"elapsed_ms": round(el), "error": str(err)[:200], "http": resp.status_code})
        c.ok(el < 20000, f"took {el:.0f}ms — no effective timeout")
        c.ok(err is not None, "loop returned without an error — timeout not reported")

    with r.case("3.3b", "Memory bomb does not take down the API") as c:
        t0 = time.perf_counter()
        try:
            resp = _draft(r, DECOR + "    x = 'a' * (1024*1024*1024)\n    return len(x)", timeout=60)
            val, err = _result(resp)
            c.info({"elapsed_ms": round((time.perf_counter()-t0)*1000),
                    "http": resp.status_code, "error": str(err)[:200]})
        except Exception as e:
            c.fail(f"request died: {type(e).__name__}: {e}")
        # API must still be alive afterwards
        h = r.get("/api/v1/rules")
        c.ok(h.status_code == 200, f"API unhealthy after memory bomb: {h.status_code}")

    with r.case("3.3c", "Deep recursion does not crash the worker") as c:
        resp = _draft(r, DECOR + "    def f(n):\n        return f(n+1)\n    return f(0)", timeout=60)
        val, err = _result(resp)
        c.info({"error": str(err)[:200]})
        h = r.get("/api/v1/rules")
        c.ok(h.status_code == 200, f"API unhealthy after recursion: {h.status_code}")

    with r.case("3.3d", "ReDoS guard on catastrophic regex") as c:
        code = DECOR + "    import re\n    return bool(re.match(r'(a+)+$', 'a'*40 + 'b'))"
        t0 = time.perf_counter()
        resp = _draft(r, code, timeout=60)
        el = (time.perf_counter() - t0) * 1000
        val, err = _result(resp)
        c.info({"elapsed_ms": round(el), "error": str(err)[:200]})
        c.ok(el < 20000, f"catastrophic regex ran {el:.0f}ms")
