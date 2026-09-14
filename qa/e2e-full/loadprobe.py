"""Latency/throughput probe. Usage: loadprobe.py <port> [policyId]"""
import httpx, time, concurrent.futures as cf, collections, os, sys
# RM_KEY override to match bench.py / harness.py / seed_journeys.py -- this was the one
# harness here with the key hard-coded and no way to point it at another workspace.
BASE=f"http://127.0.0.1:{sys.argv[1]}"; H={"x-api-key":os.environ.get("RM_KEY","rm_live_devlocaltenantkey000000000000"),"content-type":"application/json"}
POL=sys.argv[2] if len(sys.argv)>2 else "policy_claim_triage"
def payload(i): return {"policyId":POL,"payload":{"claim":{"claim_amount_inr":20000+(i%50)*1000,"policy_age_months":i%60,
        "prior_claims_12m":i%4,"documents_complete":i%2,"police_report_filed":1,"incident_severity":1+(i%5),
        "claimant_tenure_days":100+(i%900)}},"userId":f"load-{i%500}"}
def pct(v,p):
    s=sorted(v); return s[min(len(s)-1,max(0,int(round(p/100*len(s)+.5))-1))]
def run(n, conc):
    lat=[]; codes=collections.Counter(); outs=collections.Counter()
    def one(i):
        with httpx.Client(base_url=BASE, headers=H, timeout=120) as c:
            t=time.perf_counter(); r=c.post("/api/v1/decide", json=payload(i))
            return (time.perf_counter()-t)*1000, r.status_code, (r.json().get("outcome") if r.status_code==200 else None)
    t0=time.perf_counter()
    with cf.ThreadPoolExecutor(max_workers=conc) as ex:
        for dt,sc,o in ex.map(one, range(n)): lat.append(dt); codes[sc]+=1; outs[o]+=1
    wall=time.perf_counter()-t0
    return pct(lat,50), pct(lat,95), pct(lat,99), n/wall, dict(codes), dict(outs)
for conc in (1,4,16,32):
    n = 300 if conc==1 else 800
    p50,p95,p99,rps,codes,outs = run(n, conc)
    print(f"  conc={conc:<3} p50={p50:8.1f}ms p95={p95:8.1f}ms p99={p99:8.1f}ms rps={rps:7.1f} {codes}")
    if conc==16: print(f"        outcomes @16: {outs}")
    if conc==32: print(f"        outcomes @32: {outs}")
