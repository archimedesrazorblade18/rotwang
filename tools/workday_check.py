"""One-off diagnostic: which Workday addresses answer when asked slowly, from GitHub's runner."""
import json, sys, time
import requests

S = requests.Session()
S.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json,text/html"})
PODS = [1, 5, 3, 12]
GAP = 1.3
last = [0.0]

def call(method, url, **kw):
    for attempt in range(3):
        wait = GAP - (time.time() - last[0])
        if wait > 0:
            time.sleep(wait)
        last[0] = time.time()
        try:
            r = S.request(method, url, timeout=15, **kw)
        except Exception as e:
            return None, e.__class__.__name__
        if r.status_code == 429:
            ra = r.headers.get("Retry-After", "")
            print(f"  429 on {url[:70]} retry-after={ra!r}", flush=True)
            time.sleep(min(60, int(ra) if ra.isdigit() else 20))
            continue
        return r, ""
    return None, "429x3"

def jobs(t, n, site):
    r, err = call("POST", f"https://{t}.wd{n}.myworkdayjobs.com/wday/cxs/{t}/{site}/jobs",
                  json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""})
    if r is None:
        return err
    try:
        return f"{r.status_code} total={r.json().get('total')}" if r.ok else str(r.status_code)
    except ValueError:
        return f"{r.status_code} not-json"

items = json.load(open(sys.argv[1]))
t0 = time.time()
for name, tenants, known in items:
    for t, n, site in known:
        print(f"KNOWN {name} | {t}.wd{n}/{site} -> {jobs(t, n, site)}", flush=True)
        r, err = call("GET", f"https://{t}.wd{n}.myworkdayjobs.com/", allow_redirects=False)
        print(f"KNOWNROOT {name} | {t}.wd{n} -> {r.status_code if r is not None else err} {r.headers.get('Location','') if r is not None else ''}", flush=True)
    for t in tenants[:1]:
        for n in PODS:
            r, err = call("GET", f"https://{t}.wd{n}.myworkdayjobs.com/", allow_redirects=False)
            if r is None:
                print(f"ROOT {name} | {t}.wd{n} -> {err}", flush=True); continue
            loc = r.headers.get("Location", "")
            print(f"ROOT {name} | {t}.wd{n} -> {r.status_code} {loc[:140]}", flush=True)
            if r.status_code in (301, 302, 303, 307, 308) and "myworkdayjobs" in (loc or t) and "/wday/" not in loc:
                break
    if time.time() - t0 > 1500:
        print("STOP time budget", flush=True); break
