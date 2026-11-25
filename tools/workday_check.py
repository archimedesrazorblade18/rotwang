"""One-off diagnostic: find which Workday addresses answer, from GitHub's runner.
Prints one line per hit. Safe to delete."""
import json, re, sys, time
from concurrent.futures import ThreadPoolExecutor
import requests

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0", "Accept": "application/json"})
PODS = [1, 3, 5, 12, 103, 108, 2, 10, 501]

def root(tenant, n):
    url = f"https://{tenant}.wd{n}.myworkdayjobs.com/"
    try:
        r = S.get(url, allow_redirects=False, timeout=12)
        return r.status_code, r.headers.get("Location", "")
    except Exception as e:
        return "ERR", e.__class__.__name__

def jobs(tenant, n, site):
    try:
        r = S.post(f"https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
                   json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""}, timeout=15)
        if r.ok:
            d = r.json()
            return r.status_code, d.get("total")
        return r.status_code, None
    except Exception as e:
        return "ERR", e.__class__.__name__

def check(item):
    name, tenants, known = item
    out = []
    for t, n, site in known:
        out.append(f"KNOWN {name} | {t}.wd{n}/{site} -> {jobs(t, n, site)} | root {root(t, n)}")
    for t in tenants:
        for n in PODS:
            st, loc = root(t, n)
            if st not in (404, "ERR") and not (st == 200 and not loc):
                out.append(f"ROOT {name} | {t}.wd{n} -> {st} {loc[:120]}")
            elif st == 200:
                out.append(f"ROOT200 {name} | {t}.wd{n}")
    return out

items = json.load(open(sys.argv[1]))
with ThreadPoolExecutor(max_workers=10) as ex:
    for lines in ex.map(check, items):
        for l in lines:
            print(l, flush=True)
