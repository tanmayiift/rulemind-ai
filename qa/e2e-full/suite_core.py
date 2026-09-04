"""Suites 1-6: auth/tenancy, data+import, authoring+sandbox, evaluation, visibility, audit."""
import json, time, uuid, os
from harness import Runner, pct

def run(r: Runner):
    # ================= 1. AUTH / TENANCY =================
    with r.case("1.1", "Unauthenticated request is rejected") as c:
        resp = r.raw.get("/api/v1/rules")
        c.ok(resp.status_code in (401, 403), f"expected 401/403, got {resp.status_code}")

    with r.case("1.2", "Seeded dev API key is accepted") as c:
        c.ok(r.get("/api/v1/rules").status_code == 200)

    with r.case("1.3", "Malformed/unknown API key rejected") as c:
        bad = r.raw.get("/api/v1/rules", headers={"x-api-key": "rm_live_totally_bogus_key_000000"})
        c.ok(bad.status_code in (401, 403), f"got {bad.status_code}")

    with r.case("1.3b", "Empty API key rejected") as c:
        resp = r.raw.get("/api/v1/rules", headers={"x-api-key": ""})
        c.ok(resp.status_code in (401, 403), f"got {resp.status_code}")

    with r.case("1.4", "API key CRUD + role scoping") as c:
        created = r.post("/api/v1/access/keys", json={"label": f"e2e-{uuid.uuid4().hex[:6]}",
                                                      "environment": "dev", "role": "viewer"})
        if created.status_code not in (200, 201):
            c.fail(f"create key -> {created.status_code}: {created.text[:200]}")
        else:
            body = created.json()
            c.info({"keys": list(body.keys())})
            token = body.get("key") or body.get("plaintext") or body.get("apiKey")
            kid = body.get("id")
            if not token:
                c.warn(f"no plaintext key returned; keys={list(body.keys())}")
            else:
                # viewer role must NOT be able to mutate
                w = r.raw.post("/api/v1/rules", headers={"x-api-key": token},
                               json={"id": f"e2e_viewer_{uuid.uuid4().hex[:6]}", "name": "x", "tree": {}})
                if w.status_code == 200:
                    c.fail("viewer-role key was allowed to CREATE a rule (privilege escalation)")
                rd = r.raw.get("/api/v1/rules", headers={"x-api-key": token})
                c.ok(rd.status_code == 200, f"viewer read -> {rd.status_code}")
            if kid:
                r.delete(f"/api/v1/access/keys/{kid}")

    with r.case("1.9", "Rate limiting present (headers or 429 under burst)") as c:
        codes, headers_seen = [], {}
        for _ in range(80):
            resp = r.get("/api/v1/rules")
            codes.append(resp.status_code)
            for h in resp.headers:
                if "ratelimit" in h.lower() or "retry-after" in h.lower():
                    headers_seen[h] = resp.headers[h]
        c.info({"429s": codes.count(429), "rl_headers": headers_seen})
        if 429 not in codes and not headers_seen:
            c.warn("no 429 and no RateLimit-* headers across 80 rapid requests")

    # ================= 2. DATA / CONFIG IMPORT =================
    with r.case("2.1", "Connectors list + schema present") as c:
        resp = r.get("/api/v1/connectors")
        c.ok(resp.status_code == 200, f"{resp.status_code}")
        conns = resp.json()
        c.ok(isinstance(conns, list) and len(conns) > 0, "no connectors")
        c.info({"count": len(conns), "ids": [x.get("id") for x in conns]})

    with r.case("2.2", "Config import validate — well-formed payload") as c:
        payload = {"connectors": [], "variables": [], "rules": [], "scorecards": [], "policies": []}
        resp = r.post("/api/v1/import/validate", json=payload)
        c.ok(resp.status_code == 200, f"{resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 200:
            c.ok("summary" in resp.json(), "no summary in validation report")

    with r.case("2.3", "Config import validate — malformed entity flagged, not 500") as c:
        payload = {"rules": [{"id": None, "name": 12345, "tree": "not-a-tree"}]}
        resp = r.post("/api/v1/import/validate", json=payload)
        c.ok(resp.status_code in (200, 422), f"expected 200/422, got {resp.status_code}: {resp.text[:200]}")

    # ================= 3. AUTHORING + SANDBOX =================
    with r.case("3.1", "Variable draft executes in sandbox") as c:
        # source_id is required by the endpoint (authoring.py:126). Omitting it
        # made this a 422 that looked like a sandbox failure rather than a
        # malformed request -- suite_sandbox.py:14 has always sent it.
        resp = r.post("/api/v1/variables/test-draft", json={
            "source_id": "custom",
            # The sandbox executes a decorated function, not a bare script --
            # a bare `result = ...` raises "name 'payload' is not defined".
            # suite_sandbox.py:10 has always used this form.
            "code": '@variable(source="custom")\ndef probe(payload, variables, apis):\n'
                    "    return payload.get('a', 0) + payload.get('b', 0)",
            "payload": {"a": 2, "b": 40}})
        c.ok(resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 200:
            body = resp.json(); c.info(body)
            res = body.get("result", body)
            got = res.get("value") if isinstance(res, dict) else res
            c.ok(got == 42, f"expected 42, got {got!r} (body={json.dumps(body)[:200]})")

    # Each escape is a DECORATED FUNCTION BODY, and every request carries source_id -- the same
    # two corrections already applied to case 3.1 above, which were never applied to this loop.
    # Without them the endpoint 422s on a malformed request before the sandbox is ever reached,
    # and the old `if status != 200: c.ok(True)` below turned that into a pass. All seven of
    # these cases, plus 3.3, have therefore been asserting nothing:
    #
    #   sent:      {"code": "import os\nresult = ...", "payload": {}}
    #   received:  422 {"detail":[{"loc":["body","source_id"],"msg":"Field required"}]}
    #   recorded:  PASS
    #
    # The sandbox itself was fine all along -- a well-formed attempt returns
    # {"error": "Import not allowed: os"}. It is the test that was broken, which is worse: a
    # green row asserting the opposite of nothing.
    SANDBOX_ESCAPES = [
        ("import os", "    import os\n    return os.listdir('/')"),
        ("__import__", "    return __import__('os').listdir('/')"),
        ("open file", "    return open('/etc/passwd').read()"),
        ("builtins via subclass", "    return ().__class__.__bases__[0].__subclasses__()"),
        ("eval", "    return eval(\"__import__('os').getcwd()\")"),
        ("exec", "    exec('import os')\n    return 1"),
        ("globals", "    return globals()['__builtins__']"),
    ]
    for i, (label, body_src) in enumerate(SANDBOX_ESCAPES, start=1):
        with r.case(f"3.2.{i}", f"Sandbox blocks: {label}") as c:
            code = ('@variable(source="custom")\n'
                    "def probe(payload, variables, apis):\n" + body_src)
            resp = r.post("/api/v1/variables/test-draft",
                          json={"source_id": "custom", "code": code, "payload": {}})
            # A 422 here means WE sent a malformed request, not that the sandbox blocked
            # anything. That must fail loudly rather than masquerade as a block.
            if resp.status_code == 422:
                c.fail(f"malformed request, sandbox never reached: {resp.text[:200]}")
                continue
            # A non-200, non-422 rejection IS a block -- the request was refused before
            # execution -- but say so explicitly rather than silently accepting any status.
            if resp.status_code != 200:
                c.info({"status": resp.status_code, "body": resp.text[:200]})
                c.ok(True, f"rejected with {resp.status_code}")
                continue
            body = resp.json()
            res = body.get("result", body)
            err = (res.get("error") if isinstance(res, dict) else None) or body.get("error") or ""
            val = res.get("value") if isinstance(res, dict) else res
            blocked = bool(err) and val in (None, "")
            c.ok(blocked, f"NOT BLOCKED — error={str(err)[:120]!r} value={str(val)[:160]!r}")
            c.info({"error": str(err)[:200], "result": str(val)[:200]})

    with r.case("3.3", "Sandbox enforces timeout on infinite loop") as c:
        # Same two corrections. Previously this 422'd in about a millisecond and the only
        # assertion was `elapsed < 15000`, which a 422 satisfies trivially -- so it passed
        # without ever starting a loop, let alone timing one out.
        code = ('@variable(source="custom")\n'
                "def probe(payload, variables, apis):\n"
                "    while True:\n"
                "        pass\n")
        t0 = time.perf_counter()
        resp = r.post("/api/v1/variables/test-draft",
                      json={"source_id": "custom", "code": code, "payload": {}}, timeout=30)
        elapsed = (time.perf_counter() - t0) * 1000
        c.info({"elapsed_ms": round(elapsed), "status": resp.status_code,
                "body": resp.text[:200]})
        if resp.status_code == 422:
            c.fail(f"malformed request, sandbox never reached: {resp.text[:200]}")
        else:
            c.ok(elapsed < 15000, f"took {elapsed:.0f}ms — timeout not enforced")
            # The loop must actually have RUN and been killed, not been rejected instantly.
            # A sub-100ms result means nothing executed, which is how this passed before.
            c.ok(elapsed > 100, f"returned in {elapsed:.0f}ms — nothing was executed")
            if resp.status_code == 200:
                res = resp.json().get("result", {})
                err = res.get("error") if isinstance(res, dict) else None
                c.ok(bool(err), f"no timeout error reported: {str(res)[:160]}")

    with r.case("3.4", "Rule test endpoint returns per-condition detail") as c:
        rules = r.get("/api/v1/rules").json()
        rid = rules[0]["id"]
        resp = r.post(f"/api/v1/rules/{rid}/test", json={"payload": {}})
        c.ok(resp.status_code == 200, f"{resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 200:
            b = resp.json()
            c.ok("conditions" in b or "result" in b, f"no condition detail: {list(b.keys())}")
            c.info({"keys": list(b.keys())})

    with r.case("3.10", "Excel function library exposed") as c:
        resp = r.get("/api/v1/excel-functions")
        c.ok(resp.status_code == 200, f"{resp.status_code}")
        if resp.status_code == 200:
            fns = resp.json()
            # `all_functions`, not `functions` -- the endpoint returns
            # {total_functions, categories, all_functions} (main.py:1632). This case read a key
            # that never existed, so it reported "no excel functions returned" against a
            # perfectly healthy 230-function library.
            n = len(fns) if isinstance(fns, list) else (
                fns.get("total_functions") or len(fns.get("all_functions", [])))
            c.info({"count": n}); c.ok(n > 0, "no excel functions returned")

    # ================= 4. DECISIONING =================
    policies = r.get("/api/v1/policies").json()
    prod_policies = [p for p in policies if p.get("status") == "prod"] or policies
    pol = prod_policies[0]["id"]

    with r.case("4.1", "/decide returns an outcome") as c:
        resp = r.post("/api/v1/decide", json={"policyId": pol, "payload": {"age": 34}, "userId": "e2e-4-1"})
        c.ok(resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 200:
            b = resp.json()
            c.ok(b.get("outcome") in ("approve", "reject", "review", "refer", "decline"),
                 f"unexpected outcome {b.get('outcome')!r}")
            c.info({"outcome": b.get("outcome"), "score": b.get("score"), "latency_ms": b.get("latency_ms")})

    with r.case("4.1b", "/decide on unknown policy → 404, not 500") as c:
        resp = r.post("/api/v1/decide", json={"policyId": "does_not_exist", "payload": {}, "userId": "e2e"})
        c.ok(resp.status_code in (400, 404, 422), f"got {resp.status_code}: {resp.text[:200]}")

    with r.case("4.4", "/decide tolerates empty / null / junk payload") as c:
        for label, p in [("empty", {}), ("nulls", {"age": None, "x": None}),
                         ("wrong types", {"age": "not-a-number", "score": []}),
                         ("extra fields", {"zzz_unknown": "x" * 500})]:
            resp = r.post("/api/v1/decide", json={"policyId": pol, "payload": p, "userId": "e2e-4-4"})
            if resp.status_code >= 500:
                c.fail(f"payload '{label}' caused {resp.status_code}: {resp.text[:200]}"); break
            if not c.ok(resp.status_code == 200, f"payload '{label}' -> {resp.status_code}"):
                break

    with r.case("4.7", "Determinism: identical payload ⇒ identical outcome") as c:
        outs, scores = [], []
        for _ in range(6):
            b = r.post("/api/v1/decide", json={"policyId": pol, "payload": {"age": 34, "score": 700},
                                               "userId": "e2e-det"}).json()
            outs.append(b.get("outcome")); scores.append(b.get("score"))
        c.info({"outcomes": outs, "scores": scores})
        if len(set(outs)) > 1 or len(set(scores)) > 1:
            c.fail(f"non-deterministic: outcomes={set(outs)} scores={set(scores)}")

    with r.case("4.9", "Batch decide") as c:
        resp = r.post("/api/v1/decide/batch", json={"policyId": pol,
                                                    "payloads": [{"age": 30}, {"age": 40}, {"age": 50}]})
        c.ok(resp.status_code == 200, f"{resp.status_code}: {resp.text[:300]}")
        if resp.status_code == 200:
            b = resp.json()
            # `rows`, not `results`/`decisions` -- /decide/batch returns
            # {targetType, targetId, rows, count, performance} (runtime.py:375).
            rows = b if isinstance(b, list) else b.get("rows", b.get("results", b.get("decisions", [])))
            c.ok(len(rows) == 3, f"expected 3 results, got {len(rows)}")

    # ================= 5. VISIBILITY / EXPLAINABILITY =================
    with r.case("5.1", "Decision trace covers every policy step") as c:
        pdef = r.get(f"/api/v1/policies/{pol}").json()
        steps = [s["id"] for s in pdef.get("steps", [])]
        b = r.post("/api/v1/decide", json={"policyId": pol, "payload": {"age": 34}, "userId": "e2e-5-1"}).json()
        traced = [t.get("step", {}).get("id") for t in b.get("trace", [])]
        missing = [s for s in steps if s not in traced]
        c.info({"policy_steps": len(steps), "traced": len(traced), "missing": missing})
        c.ok(not missing, f"steps missing from trace: {missing}")

    with r.case("5.2", "Trace exposes per-condition variable, operator, threshold, value") as c:
        b = r.post("/api/v1/decide", json={"policyId": pol, "payload": {"age": 34}, "userId": "e2e-5-2"}).json()
        conds = [cc for rr in b.get("rule_results", [])
                 for cc in (rr.get("result") or {}).get("conditions", [])]
        c.ok(conds, "no conditions in rule_results")
        if conds:
            need = {"variable_id", "operator", "threshold", "value", "passed"}
            miss = need - set(conds[0].keys())
            c.ok(not miss, f"condition missing fields: {miss}")
            c.info({"conditions": len(conds), "sample": conds[0]})

    with r.case("5.3", "Decision is persisted and retrievable by id") as c:
        b = r.post("/api/v1/decide", json={"policyId": pol, "payload": {"age": 34}, "userId": "e2e-5-3"}).json()
        did = b.get("decision_id") or b.get("id")
        if not did:
            c.warn(f"/decide response carries no decision id (keys={list(b.keys())}) — cannot deep-link a case")
        else:
            got = r.get(f"/api/v1/decisions/{did}/reason-codes")
            c.ok(got.status_code == 200, f"reason-codes -> {got.status_code}")

    # ================= 6. AUDIT / HISTORY =================
    with r.case("6.1", "Decision history queryable (case-wise)") as c:
        marker = f"e2e-hist-{uuid.uuid4().hex[:8]}"
        for i in range(3):
            r.post("/api/v1/decide", json={"policyId": pol, "payload": {"age": 30 + i}, "userId": marker})
        time.sleep(2.0)  # async decision log
        # user_id, the SUBJECT filter. This case previously found 1 of 3 and was written off
        # as a test bug; it was a product bug. /decide wrote `userId` into client_id, which is
        # under UNIQUE(tenant_id, client_id), so decisions 2 and 3 were silently discarded --
        # the caller had a 200 and a decision_id that never existed. Subject and idempotency
        # key are now separate columns.
        resp = r.get("/api/v1/audit/decisions", params={"user_id": marker, "limit": 50})
        c.ok(resp.status_code == 200, f"{resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 200:
            b = resp.json()
            rows = b if isinstance(b, list) else b.get("items", b.get("decisions", b.get("results", [])))
            mine = [x for x in rows if x.get("user_id") == marker]
            c.info({"returned": len(rows), "matching_marker": len(mine)})
            c.ok(len(mine) >= 3, f"expected >=3 decisions for {marker}, found {len(mine)}")

    with r.case("6.2", "Audit log records mutations (who/what/when)") as c:
        rid = f"e2e_audit_{uuid.uuid4().hex[:6]}"
        mk = r.post("/api/v1/rules", json={
            "id": rid, "name": "E2E Audit Probe",
            "tree": {"type": "group", "id": "g", "logic": "AND",
                     "children": [{"type": "condition", "id": "c1", "variable": "age",
                                   "operator": ">=", "value": 18}],
                     "onPass": "pass", "onFail": "reject"}})
        c.info({"create_status": mk.status_code, "body": mk.text[:200]})
        if mk.status_code not in (200, 201):
            c.fail(f"could not create rule: {mk.status_code} {mk.text[:200]}")
        else:
            time.sleep(1.0)
            resp = r.get("/api/v1/audit", params={"limit": 100})
            if resp.status_code != 200:
                resp = r.get("/api/v1/audit/decisions", params={"limit": 100})
            c.ok(resp.status_code == 200, f"audit list -> {resp.status_code}")
            if resp.status_code == 200:
                txt = resp.text
                c.ok(rid in txt, f"new rule {rid} not found in audit trail")
            r.delete(f"/api/v1/rules/{rid}")

    with r.case("6.6", "PII redaction configurable + applied") as c:
        resp = r.get("/api/v1/settings")
        c.ok(resp.status_code == 200, f"{resp.status_code}")
        marker = "4111111111111111"
        # Sent twice: once under an obviously-sensitive key, once under an innocuous one. The
        # second is the case key-name matching alone cannot catch, and it is how a card number
        # actually reaches the decision log -- the caller picks the field names, not us.
        b = r.post("/api/v1/decide", json={"policyId": pol,
                                           "payload": {"age": 30, "card_number": marker,
                                                       "external_ref": marker},
                                           "userId": "e2e-pii"})
        c.info({"decide": b.status_code})
        time.sleep(1.5)
        hits = r.get("/api/v1/audit/decisions", params={"limit": 20})
        if hits.status_code == 200 and marker in hits.text:
            # Was a warn(). It is a fail: the value is stored verbatim in payload_preview and
            # returned in full to any viewer-role user, so downgrading it hid a live PII leak.
            c.fail("raw card-like value echoed back in audit payload (default redaction not applied)")
