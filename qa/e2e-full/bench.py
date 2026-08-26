"""Perf + fast/slow parity benchmark for /decide."""
import os, sys, time, json, statistics, concurrent.futures as cf
import httpx

BASE=os.getenv("RM_BASE","http://127.0.0.1:8080"); KEY=os.getenv("RM_KEY","rm_live_devlocaltenantkey000000000000")
POLICY=os.getenv("RM_POLICY","policy_pl_underwriting")
LABEL=os.getenv("RM_LABEL","run")
H={"x-api-key":KEY,"content-type":"application/json"}

def pctl(v,p):
    s=sorted(v); k=min(len(s)-1,max(0,int(round(p/100*len(s)+0.5))-1)); return s[k]

def one(client,i):
    t0=time.perf_counter()
    r=client.post("/api/v1/decide",json={"policyId":POLICY,"payload":{"age":30+(i%40)},"userId":f"bench-{i}"})
    dt=(time.perf_counter()-t0)*1000
    return dt, r.status_code, (r.json().get("outcome") if r.status_code==200 else None)

def run(n=400, conc=1, warm=40):
    with httpx.Client(base_url=BASE,headers=H,timeout=60) as c:
        for i in range(warm): one(c,i)
        lat=[];codes=[];outs=[]
        t0=time.perf_counter()
        if conc==1:
            for i in range(n):
                d,s,o=one(c,i); lat.append(d); codes.append(s); outs.append(o)
        else:
            with cf.ThreadPoolExecutor(max_workers=conc) as ex:
                for d,s,o in ex.map(lambda i: one(c,i), range(n)):
                    lat.append(d); codes.append(s); outs.append(o)
        wall=time.perf_counter()-t0
    return {"n":n,"concurrency":conc,"ok":codes.count(200),"errors":len(codes)-codes.count(200),
            "p50":round(pctl(lat,50),2),"p95":round(pctl(lat,95),2),"p99":round(pctl(lat,99),2),
            "min":round(min(lat),2),"max":round(max(lat),2),"mean":round(statistics.mean(lat),2),
            "rps":round(n/wall,1),"wall_s":round(wall,2),
            "outcome_dist":{o:outs.count(o) for o in set(outs)}}

if __name__=="__main__":
    out={}
    for conc in (1,4,16):
        r=run(n=300 if conc==1 else 400, conc=conc)
        out[f"conc{conc}"]=r
        print(f"[{LABEL}] conc={conc:<3} p50={r['p50']:>7}ms p95={r['p95']:>7}ms p99={r['p99']:>7}ms "
              f"rps={r['rps']:>7} ok={r['ok']}/{r['n']} err={r['errors']}", flush=True)
    print(json.dumps({"label":LABEL,"policy":POLICY,"results":out}))
