"""Careers pages: find a company's website, its careers page and the system
behind it, then read that system.

Most life-sciences companies don't post on the boards radar.py probes by name
(Greenhouse, Lever, Ashby). Their careers page links to iCIMS, Workday,
BambooHR, UKG, Oracle and a dozen others, or carries the jobs in the page
itself. This module goes the long way round: website -> careers page ->
careers system -> roles.
"""
from __future__ import annotations

import html as _html
import json
import re
import socket
from urllib.parse import urljoin, urlparse, quote

import rotwang as rw

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36 rotwang-radar",
      "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
      "Accept-Language": "en-US,en;q=0.8"}
JSON_H = {**UA, "Accept": "application/json"}
T = (6, 15)

_LEGAL = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
          "llc", "plc", "sa", "ag", "nv", "se", "bv", "gmbh", "holdings", "holding",
          "group", "the", "lp", "de", "kk", "and"}
# Words that don't identify a company on their own
_GENERIC = {"bio", "biosciences", "bioscience", "biotherapeutics", "therapeutics", "pharma",
            "pharmaceuticals", "pharmaceutical", "medical", "health", "healthcare", "labs",
            "laboratories", "laboratory", "diagnostics", "sciences", "science", "technologies",
            "technology", "biotech", "biologics", "oncology", "genomics", "international",
            "global", "us", "usa", "america", "americas", "systems", "solutions", "research",
            "clinical", "medicines", "innovations", "partners", "services", "devices"}
_PARKED = re.compile(r"domain (is )?for sale|buy this domain|hugedomains|sedo\.com|dan\.com|"
                     r"godaddy\.com/domainsearch|parkingcrew|this domain (may be|is) for sale|"
                     r"afternic|domain has expired|namecheap", re.I)


def words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (name or "").lower().replace("&", " "))
            if w and w not in _LEGAL]


def distinct(name: str) -> list[str]:
    return [w for w in words(name) if w not in _GENERIC and len(w) >= 3]


def _get(s, url, json_ok=False, **kw):
    try:
        r = s.get(url, headers=JSON_H if json_ok else UA, timeout=kw.pop("timeout", T),
                  allow_redirects=True, **kw)
    except Exception:
        return None
    return r if r is not None and r.ok else None


def _text(html: str) -> str:
    """Page source with escaped slashes and entities undone, for link hunting."""
    return _html.unescape((html or "").replace("\\/", "/").replace("\\u002F", "/"))


def _resolves(host: str) -> bool:
    try:
        socket.setdefaulttimeout(4)
        socket.gethostbyname(host)
        return True
    except Exception:
        return False


# ── Finding the website ──────────────────────────────────────────────────────

def domain_guesses(name: str) -> list[str]:
    ws, ds = words(name), distinct(name)
    if not ws:
        return []
    joined, first = "".join(ws), (ds[0] if ds else ws[0])
    out = [f"{joined}.com", f"{'-'.join(ws)}.com"]
    if len(ws) > 1:
        for suf in ("bio", "tx", "therapeutics", "pharma", "rx", "inc", "dx", "labs", ""):
            out.append(f"{first}{suf}.com")
    out += [f"{joined}.bio", f"{first}.bio", f"{joined}.co", f"{first}.io"]
    seen, res = set(), []
    for d in out:
        if d not in seen and len(d) > 6:
            seen.add(d)
            res.append(d)
    return res


def _title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
    t = m.group(1) if m else ""
    m = re.search(r'property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)', html or "", re.I)
    return _html.unescape(f"{t} {m.group(1) if m else ''}").lower()


_LIFE = re.compile(r"therapeut|pharma|clinical|patient|biotech|diagnos|medic|drug|oncolog|disease|"
                   r"\btrials?\b|biolog|molecul|genom|assay|laborator|health|device|surgical|implant|"
                   r"vaccin|antibod|cell therap|gene therap|rare disease|pipeline", re.I)


def is_company_page(html: str, name: str, domain: str = "") -> bool:
    """The page belongs to this company, not a namesake: its name is in the page
    title and the page talks about medicine, or the domain carries the name and
    the page spells the whole name. Parked domains never count."""
    if not html or _PARKED.search(html[:20000]):
        return False
    ds, ws = distinct(name), words(name)
    key = ds or ws
    if not key:
        return False
    title = _title(html)
    body = re.sub(r"[^a-z0-9]+", " ", re.sub(r"<[^>]+>", " ", html[:150000].lower()))
    squashed = re.sub(r"[^a-z0-9]", "", title)
    full = " ".join(ws)
    in_title = all(w in squashed for w in key)
    full_in_body = f" {full} " in f" {body} "
    if in_title and (full_in_body or len(set(m.group(0) for m in _LIFE.finditer(body))) >= 2):
        return True
    return bool(domain) and key[0] in domain.lower() and full_in_body


def find_website(s, name: str):
    """(domain, homepage html) or (None, None). The name's own .com first, then
    the domain Clearbit gives for exactly this name, then common patterns."""
    ws = words(name)
    primary = [f"{''.join(ws)}.com"] if ws else []
    clear = []
    r = _get(s, f"https://autocomplete.clearbit.com/v1/companies/suggest?query={quote(name)}", True)
    if r is not None:
        try:
            for c in (r.json() or [])[:3]:
                d = (c.get("domain") or "").lower()
                if d and words(c.get("name", "")) == ws:      # the same name, word for word
                    clear.append(d)
        except ValueError:
            pass
    cands = []
    for d in primary + clear + domain_guesses(name):
        if d not in cands:
            cands.append(d)
    for i, d in enumerate(cands[:10]):
        if not _resolves(d):
            continue
        r = _get(s, f"https://{d}/") or _get(s, f"https://www.{d}/")
        if r is None:
            continue
        host = (urlparse(r.url).hostname or d).lower()
        if re.search(r"\.(edu|gov|mil)(\.[a-z]{2})?$|\.ac\.[a-z]{2}$|\.k12\.", host):
            continue                              # a school or government namesake
        if is_company_page(r.text, name, d):
            return host.removeprefix("www."), r.text
    return None, None


# ── Recognising the careers system ───────────────────────────────────────────
# Each pattern finds the system and its identifier in a page's links, frames
# or scripts. Order matters: a page that embeds a Greenhouse board also links
# to its own careers URL.

_NOT_TENANT = {"www", "app", "api", "cdn", "static", "help", "support", "marketing", "assets",
               "images", "blog", "status", "developers", "go", "info", "careers", "jobs"}

PATTERNS = [
    ("greenhouse", r"greenhouse\.io/(?:embed/job_board(?:/js)?\?for=|v1/boards/)([A-Za-z0-9_-]+)"),
    ("greenhouse", r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/([A-Za-z0-9_-]+)"),
    ("lever", r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)"),
    ("lever", r"api\.(?:eu\.)?lever\.co/v0/postings/([A-Za-z0-9_.-]+)"),
    ("ashby", r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)"),
    ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)"),
    ("workable", r"apply\.workable\.com/([A-Za-z0-9_-]+)"),
    ("recruitee", r"//([a-z0-9-]+)\.recruitee\.com"),
    ("workday", r"([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)"),
    ("paylocity", r"recruiting\.paylocity\.com/Recruiting/Jobs/All/([0-9a-fA-F-]{36})"),
    ("ukg", r"(recruiting\d*\.ultipro\.com|[a-z0-9]+\.rec\.pro\.ukg\.net)/([A-Za-z0-9]+)/JobBoard/([0-9a-fA-F-]{36})"),
    ("oracle", r"([a-z0-9-]+\.fa(?:\.[a-z0-9-]+)?\.oraclecloud\.com)/hcmUI/CandidateExperience/[a-z]{2}(?:-[A-Z]{2})?/sites/([A-Za-z0-9_]+)"),
    ("icims", r"//((?:careers|jobs|uscareers|external)-[a-z0-9-]+\.icims\.com)"),
    ("icims", r"//([a-z0-9-]+\.icims\.com)"),
    ("bamboohr", r"//([a-z0-9-]+)\.bamboohr\.com"),
    ("breezy", r"//([a-z0-9-]+)\.breezy\.hr"),
    ("jazzhr", r"//([a-z0-9-]+)\.applytojob\.com"),
    ("jazzhr", r"app\.jazz\.co/widgets/[a-z]+/create/([a-z0-9-]+)"),
    ("personio", r"//([a-z0-9-]+)\.jobs\.personio\.(?:de|com)"),
    ("isolved", r"//([a-z0-9-]+)\.isolvedhire\.com"),
    ("trinet", r"app\.trinethire\.com/companies/([A-Za-z0-9_-]+)"),
    ("jobscore", r"careers\.jobscore\.com/(?:careers|jobs)/([A-Za-z0-9_-]+)"),
    ("jobvite", r"jobs\.jobvite\.com/([A-Za-z0-9_-]+)"),
    ("rippling", r"ats\.rippling\.com/([A-Za-z0-9_-]+)"),
    ("pinpoint", r"//([a-z0-9-]+)\.pinpointhq\.com"),
    ("teamtailor", r"//([a-z0-9-]+\.teamtailor\.com)"),
    ("adp", r"workforcenow\.adp\.com/mascsr/default/mdf/recruitment/recruitment\.html\?cid=([0-9a-fA-F-]{36})"),
    ("eightfold", r"//([a-z0-9-]+\.eightfold\.ai)"),
]
# Systems recognised but not read (yet): counted so the next reader is chosen by need.
KNOWN_UNREAD = [
    ("successfactors", r"successfactors\.(?:com|eu)|jobs\.sap\.com|/career\?company="),
    ("taleo", r"taleo\.net"),
    ("phenom", r"phenompeople|phApp\.ddo|cdn\.phenompeople"),
    ("dayforce", r"dayforcehcm\.com"),
    ("avature", r"avature\.net"),
    ("paycom", r"paycomonline\.net"),
    ("paycor", r"recruitingbypaycor\.com"),
    ("brassring", r"brassring\.com"),
    ("jobylon", r"jobylon\.com"),
    ("hirebridge", r"hirebridge\.com"),
    ("clearcompany", r"clearcompany\.com"),
    ("comeet", r"comeet\.(?:co|com)"),
    ("findem", r"findemtalent\.com"),
    ("rippling-embed", r"ripplingcdn\.com"),
]


def detect(page: str, page_url: str = ""):
    """(ats, token) for a readable system, ("unread:<name>", "") for a known
    one we can't read, or None."""
    t = _text(page)
    # Phenom sites carry their job list as data in the page (and Workday apply
    # links inside it), so they are recognised before the other systems.
    if "phApp.ddo" in t:
        b = re.search(r'"baseUrl"\s*:\s*"https?://([a-z0-9.-]+)/((?:[a-z]{2}(?:-[a-z]{2})?|global)/[a-z]{2})/?"',
                      t, re.I)
        if b:
            return "phenom", f"{b.group(1).lower()}|{b.group(2).lower()}"
    if page_url and re.search(r"rmkcdn\.successfactors\.com|/platform/csb/", t):
        host = (urlparse(page_url).hostname or "").lower()
        if host and "successfactors" not in host:
            return "successfactors", host
    sx = re.search(r"https?://(career\d*\.successfactors\.(?:com|eu))/career\?company=([A-Za-z0-9_]+)", t, re.I)
    if sx:
        return "sfxml", f"{sx.group(1).lower()}|{sx.group(2)}"
    for ats, pat in PATTERNS:
        for m in re.finditer(pat, t, re.I):
            g = m.groups()
            if ats == "workday":
                if g[2].lower() in ("wday", "cxs"):
                    continue
                return ats, f"{g[0].lower()}.{g[1].lower()}.myworkdayjobs.com|{g[0].lower()}|{g[2]}"
            if ats == "ukg":
                return ats, f"{g[0].lower()}|{g[1]}|{g[2]}"
            if ats == "oracle":
                return ats, f"{g[0]}|{g[1]}"
            tok = g[0]
            sub = tok.split(".")[0].lower()
            if sub in _NOT_TENANT or tok.lower() in ("embed", "v1", "js", "assets") or tok.startswith("_"):
                continue
            if ats == "eightfold":
                dom = re.search(r'["\']domain["\']\s*[:=]\s*["\']([a-z0-9.-]+\.[a-z]{2,})', t, re.I)
                return ats, f"{tok}|{dom.group(1) if dom else ''}"
            return ats, tok
    # JSON-LD job postings in the page itself
    if re.search(r'"@type"\s*:\s*"JobPosting"', t):
        return "jsonld", page_url
    for name, pat in KNOWN_UNREAD:
        if re.search(pat, t, re.I):
            return f"unread:{name}", ""
    # A careers site served by the system on the company's own domain
    if re.search(r"talentbrew|radancy", t, re.I) and page_url:
        return "radancy", urlparse(page_url).hostname
    if re.search(r"teamtailor", t, re.I) and page_url:
        return "teamtailor", urlparse(page_url).hostname
    if re.search(r"eightfold", t, re.I) and page_url:
        dom = re.search(r'["\']domain["\']\s*[:=]\s*["\']([a-z0-9.-]+\.[a-z]{2,})', t, re.I)
        return "eightfold", f"{urlparse(page_url).hostname}|{dom.group(1) if dom else ''}"
    return None


_CAREER_LINK = re.compile(r"career|jobs?\b|join[- ](us|our|the)|work[- ](with|at|for)[- ]us|"
                          r"opportunit|openings|vacanc|we.?re hiring|employment", re.I)
_OPENINGS_LINK = re.compile(r"open (positions|roles|jobs)|current (openings|opportunities|positions)|"
                            r"job openings|view (all )?(jobs|openings|positions|roles)|search (jobs|openings)|"
                            r"see (all )?(jobs|openings|positions|roles)|explore (jobs|roles|opportunities)|"
                            r"all jobs|job search|apply now|browse jobs", re.I)
_SKIP_HREF = re.compile(r"\.(pdf|jpg|png|svg|zip|docx?)$|^mailto:|^tel:|linkedin\.com|facebook\.com|"
                        r"twitter\.com|x\.com/|instagram\.com|youtube\.com|glassdoor\.com|indeed\.com", re.I)


def _links(page: str, base: str):
    for m in re.finditer(r"<a\b[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", page or "", re.I | re.S):
        href, label = m.group(1).strip(), re.sub(r"<[^>]+>", " ", m.group(2))
        if _SKIP_HREF.search(href):
            continue
        yield urljoin(base, _html.unescape(href)), _html.unescape(" ".join(label.split()))


def find_careers(s, name: str, domain: str, home: str = ""):
    """Look for the careers system starting from the homepage.
    Returns {"careers": url, "ats": ..., "token": ...}; ats is None when the
    careers page was found but its system wasn't recognised."""
    base = f"https://{domain}/"
    if not home:
        r = _get(s, base)
        home = r.text if r is not None else ""
    hit = detect(home, base)
    if hit and not hit[0].startswith("unread"):
        return {"careers": base, "ats": hit[0], "token": hit[1]}
    cands = []
    for url, label in _links(home, base):
        host = urlparse(url).hostname or ""
        if _CAREER_LINK.search(label) or _CAREER_LINK.search(urlparse(url).path) or host.startswith(("careers.", "jobs.")):
            if url not in cands:
                cands.append(url)
    for p in ("careers", "careers/", "company/careers", "about/careers", "about-us/careers",
              "join-us", "jobs", "careers/open-positions"):
        u = urljoin(base, p)
        if u not in cands:
            cands.append(u)
    found_page, unread = None, None
    tried = 0
    for url in cands:
        if tried >= 6:
            break
        tried += 1
        hit = detect(url, url)                    # the link itself may be the system
        if hit and not hit[0].startswith("unread") and hit[0] != "jsonld":
            return {"careers": url, "ats": hit[0], "token": hit[1]}
        r = _get(s, url)
        if r is None:
            continue
        page, final = r.text, r.url
        if re.search(r"404|not[-_ ]found", final, re.I) or re.search(r"page not found|404", _title(page)):
            continue
        hit = detect(final, final) or detect(page, final)
        if hit and not hit[0].startswith("unread"):
            return {"careers": final, "ats": hit[0], "token": hit[1]}
        if hit:
            unread = hit[0]
        found_page = found_page or final
        # one level deeper: "View open positions"
        for u2, label in _links(page, final):
            if not _OPENINGS_LINK.search(label):
                continue
            hit = detect(u2, u2)
            if hit and not hit[0].startswith("unread") and hit[0] != "jsonld":
                return {"careers": final, "ats": hit[0], "token": hit[1]}
            r2 = _get(s, u2)
            if r2 is None:
                continue
            hit = detect(r2.url, r2.url) or detect(r2.text, r2.url)
            if hit and not hit[0].startswith("unread"):
                return {"careers": r2.url, "ats": hit[0], "token": hit[1]}
            if hit:
                unread = hit[0]
            break
    if found_page:
        return {"careers": found_page, "ats": unread, "token": ""}
    return None


def find_system(s, url: str):
    """The readable system behind a careers page that uses Phenom or
    SuccessFactors: the page itself, or a jobs/careers site it links to.
    Returns (ats, token, page_url) or None."""
    r = _get(s, url)
    if r is None:
        return None
    hit = detect(r.text, r.url)
    if hit and not hit[0].startswith("unread"):
        return hit[0], hit[1], r.url
    own = (urlparse(r.url).hostname or "").removeprefix("www.")
    cands = []
    for u, label in _links(r.text, r.url):
        h = (urlparse(u).hostname or "").lower()
        if not h or u in cands:
            continue
        if _OPENINGS_LINK.search(label) or (h != own and h.startswith(("jobs.", "careers.", "career."))):
            cands.append(u)
    for u in cands[:5]:
        r2 = _get(s, u)
        if r2 is None:
            continue
        hit = detect(r2.text, r2.url)
        if hit and not hit[0].startswith("unread"):
            return hit[0], hit[1], r2.url
    return None


def page_hosts(s, url: str) -> str:
    """For testing: the outside hosts a page loads or links to."""
    r = None
    for h in (UA, {**UA, "User-Agent": UA["User-Agent"].replace(" rotwang-radar", "")}):
        try:
            r = s.get(url, headers=h, timeout=T, allow_redirects=True)
        except Exception as e:
            r = None
            err = e.__class__.__name__
            continue
        if r.ok:
            break
    if r is None:
        return f"{url}: {err}"
    if not r.ok:
        return f"{url}: HTTP {r.status_code} (server {r.headers.get('server', '?')}) {r.text[:120]!r}"
    own = (urlparse(r.url).hostname or "").removeprefix("www.")
    hosts = {}
    for m in re.finditer(r"""(?:src|href|data-src|action)=["']([^"']+)["']|https?://[a-z0-9.-]+\.[a-z]{2,}[^"'\s<>)]*""",
                         _text(r.text), re.I):
        u = m.group(1) or m.group(0)
        h = (urlparse(urljoin(r.url, u)).hostname or "").lower()
        if not h or h.endswith(own) or re.search(r"google|gstatic|facebook|twitter|linkedin|youtube|"
                                                 r"doubleclick|cloudflare|jsdelivr|jquery|fonts|typekit|"
                                                 r"hotjar|hubspot|cookie|onetrust|w3\.org|schema\.org|"
                                                 r"wp\.com|gravatar|vimeo|instagram|addtoany|recaptcha", h):
            continue
        hosts[h] = hosts.get(h, 0) + 1
    return (f"{url} -> {r.url} [{r.status_code}] {_title(r.text)[:60]!r}\n      " +
            ", ".join(f"{h}({n})" for h, n in sorted(hosts.items(), key=lambda x: -x[1])[:14]))


_PEEK = ["phApp.ddo", "eagerLoadRefineSearch", "totalHits", "refNum", "successfactors", "career?company=",
         "jobs.sap.com", "hr.cloud.sap", "rmk", "data-row", "jobTitle-link", "/job/", "searchResultsShell",
         "jobs-list", "JobPosting", "sitemap"]


def peek(s, url: str) -> str:
    """For testing: status, final address and the text around system markers.
    Extra markers can follow the address: URL##marker1,marker2"""
    url, _, extra = url.partition("##")
    marks = [m for m in extra.split(",") if m] or _PEEK
    try:
        r = s.get(url, headers=UA, timeout=T, allow_redirects=True)
    except Exception as e:
        return f"{url}: {e.__class__.__name__}"
    t = r.text or ""
    out = [f"{url} -> {r.url} [{r.status_code}] {len(t)} chars {_title(t)[:60]!r}"]
    for m in marks:
        i = t.find(m)
        if i >= 0:
            snip = re.sub(r"\s+", " ", t[max(0, i - 120): i + 260])
            out.append(f"      [{m}] x{t.count(m)}: {snip}")
    return "\n".join(out)


# ── Readers ──────────────────────────────────────────────────────────────────
# Each returns (roles, ok). ok=False means the system didn't answer.

SEARCH_TERMS = ["statistic", "biostat", "programmer", "data scien", "machine learning",
                "bioinformatic", "epidemiolog", "real world", "quantitative", "contract", "consultant"]


def _role(name, system, title, url, loc="", etype="", remote=False, posted="", body=""):
    title = rw.clean(str(title or ""))
    loc = str(loc or "")
    return rw.Role(title=title, company=name, location=loc,
                   contract=rw.is_contract(title, str(etype or ""), body),
                   remote=bool(remote) or "remote" in f"{title} {loc}".lower(),
                   source=f"{name} · {system}", url=str(url), posted=str(posted or "")[:10], body=body)


def read_bamboohr(s, name, tok):
    r = _get(s, f"https://{tok}.bamboohr.com/careers/list", True)
    if r is None:
        return [], False
    try:
        rows = (r.json() or {}).get("result") or []
    except ValueError:
        return [], False
    out = []
    for j in rows:
        loc = j.get("location") or {}
        loc = ", ".join(v for v in [loc.get("city"), loc.get("state")] if v) if isinstance(loc, dict) else str(loc)
        out.append(_role(name, "BambooHR", j.get("jobOpeningName"),
                         f"https://{tok}.bamboohr.com/careers/{j.get('id')}", loc,
                         j.get("employmentStatusLabel"), j.get("isRemote")))
    return out, True


def read_breezy(s, name, tok):
    r = _get(s, f"https://{tok}.breezy.hr/json", True)
    if r is None:
        return [], False
    try:
        rows = r.json() or []
    except ValueError:
        return [], False
    out = []
    for j in rows if isinstance(rows, list) else []:
        loc = j.get("location") or {}
        out.append(_role(name, "Breezy", j.get("name"), j.get("url"),
                         loc.get("name", "") if isinstance(loc, dict) else loc,
                         (j.get("type") or {}).get("name", "") if isinstance(j.get("type"), dict) else j.get("type"),
                         isinstance(loc, dict) and loc.get("is_remote"), j.get("published_date")))
    return out, True


def read_pinpoint(s, name, tok):
    r = _get(s, f"https://{tok}.pinpointhq.com/postings.json", True)
    if r is None:
        return [], False
    try:
        rows = (r.json() or {}).get("data") or []
    except ValueError:
        return [], False
    out = []
    for j in rows:
        loc = j.get("location") or {}
        out.append(_role(name, "Pinpoint", j.get("title"), j.get("url"),
                         loc.get("name", "") if isinstance(loc, dict) else loc,
                         j.get("employment_type_text") or j.get("employment_type"),
                         "remote" in str(j.get("workplace_type", "")).lower()))
    return out, True


def _rss(s, name, system, url):
    r = _get(s, url)
    if r is None:
        return [], False
    out = []
    for item in re.findall(r"<item>(.*?)</item>", r.text, re.S):
        t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
        l = re.search(r"<link>(.*?)</link>", item, re.S)
        if t and l:
            out.append(_role(name, system, _html.unescape(t.group(1)), l.group(1).strip()))
    return out, True


def read_teamtailor(s, name, host):
    return _rss(s, name, "Teamtailor", f"https://{host}/jobs.rss")


def _anchors(s, name, system, url, pat):
    r = _get(s, url)
    if r is None:
        return [], False
    out, seen = [], set()
    for u, label in _links(r.text, r.url):
        if re.search(pat, u) and u not in seen and label:
            seen.add(u)
            out.append(_role(name, system, label, u))
    return out, True


def read_jazzhr(s, name, tok):
    return _anchors(s, name, "JazzHR", f"https://{tok}.applytojob.com/apply/jobs", r"applytojob\.com/apply/[A-Za-z0-9]+/")


def read_jobvite(s, name, tok):
    return _anchors(s, name, "Jobvite", f"https://jobs.jobvite.com/{tok}/jobs", rf"jobvite\.com/{re.escape(tok)}/job/")


def read_rippling(s, name, tok):
    r = _get(s, f"https://ats.rippling.com/api/v2/board/{tok}/jobs", True)
    if r is not None:
        try:
            d = r.json()
            rows = d.get("items") or d.get("jobs") or (d if isinstance(d, list) else [])
            out = []
            for j in rows:
                locs = j.get("locations") or j.get("workLocations") or []
                loc = ", ".join(str((x or {}).get("name", x)) for x in locs) if isinstance(locs, list) else str(locs)
                out.append(_role(name, "Rippling", j.get("name") or j.get("title"),
                                 j.get("url") or f"https://ats.rippling.com/{tok}/jobs/{j.get('id')}", loc,
                                 j.get("employmentType") or ""))
            if out:
                return out, True
        except ValueError:
            pass
    return _anchors(s, name, "Rippling", f"https://ats.rippling.com/{tok}/jobs", rf"rippling\.com/{re.escape(tok)}/jobs/[0-9a-f-]+")


def read_icims(s, name, host):
    out, seen, ok = [], set(), False
    for term in SEARCH_TERMS[:7]:
        r = _get(s, f"https://{host}/jobs/search?ss=1&searchKeyword={quote(term)}&in_iframe=1")
        if r is None:
            continue
        ok = True
        for m in re.finditer(r'href="(https?://[^"]+/jobs/(\d+)/([^/"]+)/job[^"]*)"[^>]*?(?:title="([^"]*)")?', r.text):
            url, jid, slug, title = m.group(1), m.group(2), m.group(3), m.group(4) or ""
            if jid in seen:
                continue
            seen.add(jid)
            title = re.sub(r"^\s*\d+\s*-\s*", "", _html.unescape(title)) or slug.replace("-", " ").title()
            out.append(_role(name, "iCIMS", title, url.split("?")[0]))
    return out, ok


def read_ukg(s, name, tok):
    parts = tok.split("|")
    host, tenant, guid = (["recruiting.ultipro.com"] + parts)[-3:]
    url = f"https://{host}/{tenant}/JobBoard/{guid}/JobBoardView/LoadSearchResults"
    body = {"opportunitySearch": {"Top": 200, "Skip": 0, "QueryString": "", "OrderBy": [
        {"Value": "postedDateDesc", "PropertyName": "PostedDate", "Ascending": False}], "Filters": []},
        "matchCriteria": {"PreferredJobs": [], "Educations": [], "LicenseAndCertifications": [],
                          "Skills": [], "hasNoLicenses": False, "SkippedSkills": []}}
    try:
        r = s.post(url, json=body, headers=JSON_H, timeout=T)
    except Exception:
        return [], False
    if not r.ok:
        return [], False
    try:
        rows = (r.json() or {}).get("opportunities") or []
    except ValueError:
        return [], False
    out = []
    for j in rows:
        locs = j.get("Locations") or []
        loc = ""
        if locs:
            a = (locs[0] or {}).get("Address") or {}
            loc = ", ".join(v for v in [a.get("City"), ((a.get("State") or {}).get("Code") if isinstance(a.get("State"), dict) else a.get("State"))] if v)
        out.append(_role(name, "UKG", j.get("Title"),
                         f"https://{host}/{tenant}/JobBoard/{guid}/OpportunityDetail?opportunityId={j.get('Id')}",
                         loc, "full time" if j.get("FullTime") else "", posted=j.get("PostedDate")))
    return out, True


def read_oracle(s, name, tok):
    host, site = tok.split("|")
    out, seen, ok = [], set(), False
    for term in SEARCH_TERMS[:7]:
        u = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions?onlyData=true"
             f"&expand=requisitionList.secondaryLocations&finder=findReqs;siteNumber={site},"
             f"facetsList=NONE,limit=50,keyword=%22{quote(term)}%22")
        r = _get(s, u, True)
        if r is None:
            continue
        ok = True
        try:
            items = (r.json() or {}).get("items") or []
        except ValueError:
            continue
        for it in items:
            for j in it.get("requisitionList") or []:
                if j.get("Id") in seen:
                    continue
                seen.add(j.get("Id"))
                out.append(_role(name, "Oracle", j.get("Title"),
                                 f"https://{host}/hcmUI/CandidateExperience/en/sites/{site}/job/{j.get('Id')}",
                                 j.get("PrimaryLocation"), j.get("JobSchedule") or j.get("ContractType") or "",
                                 "remote" in str(j.get("WorkplaceType", "")).lower(), j.get("PostedDate")))
    return out, ok


def read_eightfold(s, name, tok):
    host, dom = (tok.split("|") + [""])[:2]
    out, seen, ok = [], set(), False
    for term in SEARCH_TERMS[:7]:
        u = f"https://{host}/api/apply/v2/jobs?domain={quote(dom)}&start=0&num=50&query={quote(term)}"
        r = _get(s, u, True)
        if r is None:
            continue
        ok = True
        try:
            rows = (r.json() or {}).get("positions") or []
        except ValueError:
            continue
        for j in rows:
            if j.get("id") in seen:
                continue
            seen.add(j.get("id"))
            out.append(_role(name, "Eightfold", j.get("name"),
                             j.get("canonicalPositionUrl") or f"https://{host}/careers/job/{j.get('id')}",
                             j.get("location"), j.get("type") or "",
                             "remote" in str(j.get("work_location_option", "")).lower()))
    return out, ok


def read_adp(s, name, cid):
    u = (f"https://workforcenow.adp.com/mascsr/default/careercenter/public/events/staffing/v1/"
         f"job-requisitions?cid={cid}&lang=en_US&locale=en_US&$top=200")
    r = _get(s, u, True)
    if r is None:
        return [], False
    try:
        rows = (r.json() or {}).get("jobRequisitions") or []
    except ValueError:
        return [], False
    out = []
    for j in rows:
        locs = j.get("requisitionLocations") or []
        loc = ""
        if locs:
            a = ((locs[0] or {}).get("address") or {})
            loc = ", ".join(v for v in [a.get("cityName"), ((a.get("countrySubdivisionLevel1") or {}).get("codeValue"))] if v)
        etype = " ".join(str((x or {}).get("nameCode", {}).get("shortName", "")) for x in (j.get("workLevelCode") and [j.get("workLevelCode")] or []))
        out.append(_role(name, "ADP", j.get("requisitionTitle"),
                         f"https://workforcenow.adp.com/mascsr/default/mdf/recruitment/recruitment.html?cid={cid}&jobId={j.get('itemID')}",
                         loc, etype, posted=j.get("postDate")))
    return out, True


def read_radancy(s, name, host):
    """Radancy (TalentBrew) careers sites: /search-jobs/<term> lists /job/ links."""
    out, seen, ok = [], set(), False
    for term in SEARCH_TERMS[:7]:
        r = _get(s, f"https://{host}/search-jobs/{quote(term)}")
        if r is None:
            continue
        ok = True
        for u, label in _links(r.text, r.url):
            if "/job/" in u and u not in seen and label and len(label) < 140:
                seen.add(u)
                out.append(_role(name, "careers site", label, u))
    return out, ok


def read_personio(s, name, tok):
    r = _get(s, f"https://{tok}.jobs.personio.de/xml")
    if r is None:
        return [], False
    out = []
    for pos in re.findall(r"<position>(.*?)</position>", r.text, re.S):
        g = lambda k: (re.search(rf"<{k}>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</{k}>", pos, re.S) or [None, ""])[1]
        jid = g("id")
        out.append(_role(name, "Personio", _html.unescape(g("name")), f"https://{tok}.jobs.personio.de/job/{jid}",
                         g("office"), f"{g('employmentType')} {g('schedule')}"))
    return out, True


def read_isolved(s, name, tok):
    return _anchors(s, name, "iSolved", f"https://{tok}.isolvedhire.com/jobs/", r"isolvedhire\.com/jobs/\d+")


def read_trinet(s, name, tok):
    return _anchors(s, name, "TriNet Hire", f"https://app.trinethire.com/companies/{tok}/jobs",
                    r"trinethire\.com/companies/[^/]+/jobs/\d+")


def read_jobscore(s, name, tok):
    return _anchors(s, name, "JobScore", f"https://careers.jobscore.com/careers/{tok}",
                    rf"jobscore\.com/careers/{re.escape(tok)}/jobs/")


_ROLE_NOUN = re.compile(r"scientist|statistician|biostatistician|programmer|analyst|director|manager|"
                        r"\blead\b|\bhead\b|engineer|associate|specialist|consultant|officer|\bvp\b|"
                        r"vice president|epidemiologist|modeler|pharmacometrician|fellow", re.I)


_NOT_JOB = re.compile(r"board of directors|analyst coverage|leadership|management team|our team|"
                      r"\bgrants?\b|culture|values|benefits|investor|press|news|events|contact|"
                      r"privacy|cookie|careers?\b|join us|why work|life at|meet the|learn more", re.I)


def read_page(s, name, url):
    """Jobs listed on a careers page with no careers system behind it: link
    and heading texts that read like job titles."""
    r = _get(s, url)
    if r is None:
        return [], False
    out, seen = [], set()
    texts = [(u, label) for u, label in _links(r.text, r.url)]
    texts += [(r.url, re.sub(r"<[^>]+>", " ", m.group(1)))
              for m in re.finditer(r"<(?:h[2-5]|strong|b)\b[^>]*>(.*?)</(?:h[2-5]|strong|b)>", r.text, re.I | re.S)]
    for u, label in texts:
        t = _html.unescape(" ".join(label.split()))
        if (not (8 <= len(t) <= 90) or ". " in t or t.endswith(".") or t.lower() in seen
                or not _ROLE_NOUN.search(t) or _NOT_JOB.search(t) or t[:1].islower()):
            continue
        seen.add(t.lower())
        out.append(_role(name, "careers page", t, u))
    return out, True


def _jsonld_jobs(page: str):
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page or "", re.S | re.I):
        try:
            d = json.loads(m.group(1).strip())
        except ValueError:
            continue
        stack = [d]
        while stack:
            x = stack.pop()
            if isinstance(x, list):
                stack.extend(x)
            elif isinstance(x, dict):
                if x.get("@type") == "JobPosting":
                    yield x
                for k in ("@graph", "itemListElement", "item"):
                    if k in x:
                        stack.append(x[k])


def read_jsonld(s, name, url):
    r = _get(s, url)
    if r is None:
        return [], False
    out = []
    for j in _jsonld_jobs(r.text):
        loc = j.get("jobLocation") or {}
        if isinstance(loc, list):
            loc = loc[0] if loc else {}
        a = (loc.get("address") or {}) if isinstance(loc, dict) else {}
        loc = ", ".join(v for v in [a.get("addressLocality"), a.get("addressRegion")] if isinstance(v, str) and v)
        et = j.get("employmentType") or ""
        out.append(_role(name, "careers page", j.get("title"), j.get("url") or r.url, loc,
                         " ".join(et) if isinstance(et, list) else str(et),
                         j.get("jobLocationType") == "TELECOMMUTE", j.get("datePosted")))
    return out, True


# Phenom (Roche, Thermo Fisher, GSK, UCB, GE HealthCare ...): each search page
# carries its first ten results as data. Its keyword search matches whole
# words; a word it doesn't know returns every job, so those pages stop at one.
PH_TERMS = ["biostatistics", "biostatistician", "statistician", "statistical programmer",
            "statistics", "data scientist", "bioinformatics", "epidemiology",
            "real world evidence", "pharmacometrics", "machine learning", "artificial intelligence",
            "contract"]


def _ph_ddo(text: str):
    i = text.find("phApp.ddo")
    j = text.find("{", i) if i >= 0 else -1
    if j < 0:
        return None
    try:
        return json.JSONDecoder().raw_decode(text, j)[0]
    except ValueError:
        return None


def read_phenom(s, name, tok):
    host, loc = (tok.split("|") + ["global/en"])[:2]
    base = f"https://{host}/{loc.strip('/')}"
    out, seen, ok = [], set(), False
    for term in PH_TERMS:
        for start in (0, 10, 20):
            r = _get(s, f"{base}/search-results?keywords={quote(term)}&from={start}")
            d = _ph_ddo(r.text) if r is not None else None
            res = (d or {}).get("eagerLoadRefineSearch") or {}
            if not res:
                break
            ok = True
            jobs = (res.get("data") or {}).get("jobs") or []
            for j in jobs:
                jid = str(j.get("jobId") or j.get("jobSeqNo") or "")
                if not jid or jid in seen:
                    continue
                seen.add(jid)
                title = rw.clean(str(j.get("title") or ""))
                slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-")
                where = j.get("location") or ", ".join(
                    str(v) for v in [j.get("city"), j.get("state"), j.get("country")] if v)
                teaser = (j.get("ml_job_parser") or {}).get("descriptionTeaser") or j.get("descriptionTeaser") or ""
                etype = " ".join(str(j.get(k) or "") for k in ("type", "jobType", "timeType", "employmentType"))
                remote = re.search(r"remote|virtual|home", f"{where} {j.get('workplaceType') or ''} "
                                   f"{j.get('locationType') or ''}", re.I)
                out.append(_role(name, "careers site", title, f"{base}/job/{quote(jid)}/{slug}", where, etype,
                                 bool(remote), j.get("postedDate"), teaser))
            total = int(res.get("totalHits") or 0)
            if total > 300 or start + 10 >= total or len(jobs) < 10:
                break                              # an unknown word lists every job: one page is enough
    return out, ok


# SuccessFactors career sites (BioNTech, Perrigo, Lupin, Karl Storz ...): each
# keyword search has an RSS feed with the full description.
SF_TERMS = ["biostatistics", "statistician", "statistical", "statistics", "data scientist",
            "bioinformatics", "epidemiology", "real world", "programmer", "machine learning",
            "artificial intelligence", "contract", "consultant"]


def read_successfactors(s, name, host):
    out, seen, ok = [], set(), False
    for term in SF_TERMS:
        r = _get(s, f"https://{host}/services/rss/job/?locale=en_US&keywords=({quote(term)})")
        if r is None or "<rss" not in r.text[:500].lower():
            continue
        ok = True
        for item in re.findall(r"<item>(.*?)</item>", r.text, re.S):
            t = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", item, re.S)
            l = re.search(r"<link>(.*?)</link>", item, re.S)
            if not (t and l):
                continue
            url = _html.unescape(l.group(1).strip()).split("?")[0]
            if url in seen or "/job/" not in url:
                continue                           # "No jobs currently available" placeholder
            seen.add(url)
            title = _html.unescape(t.group(1)).strip()
            where = ""
            m = re.match(r"(.*\S)\s*\(([^()]*,[^()]*)\)\s*$", title)   # "Title (City, ST, US, 02139)"
            if m:
                title, where = m.group(1), m.group(2)
            d = re.search(r"<description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</description>", item, re.S)
            body = rw.clean(re.sub(r"<[^>]+>", " ", _html.unescape(d.group(1)))) [:1800] if d else ""
            p = re.search(r"<pubDate>(.*?)</pubDate>", item)
            out.append(_role(name, "careers site", title, url, where, "", False,
                             _rss_date(p.group(1)) if p else "", body))
    return out, ok


def _rss_date(v: str) -> str:
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(v).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def read_sfxml(s, name, tok):
    """Older SuccessFactors career pages (career5.successfactors.eu/career?company=X):
    one XML file lists every job."""
    host, cid = (tok.split("|") + [""])[:2]
    r = _get(s, f"https://{host}/career?company={quote(cid)}&career_ns=job_listing_summary&resultType=XML",
             timeout=(6, 40))
    if r is None or "<JobTitle" not in r.text:
        return [], False
    out = []
    for chunk in re.split(r"<JobTitle>", r.text)[1:]:
        tm = re.match(r"(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</JobTitle>", chunk, re.S)
        if not tm:
            continue
        req = re.search(r"<(?:ReqId|Job-Req-Id|JobReqId|Req-Id)>(?:<!\[CDATA\[)?\s*(\d+)", chunk)
        where = re.search(r"<(?:Location|City|mfield\d)>(?:<!\[CDATA\[)?([^<\]]{2,80})", chunk)
        d = re.search(r"<Job-Description>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</Job-Description>", chunk, re.S)
        url = (f"https://{host}/career?company={cid}&career_ns=job_listing&career_job_req_id={req.group(1)}"
               if req else f"https://{host}/career?company={cid}")
        body = rw.clean(re.sub(r"<[^>]+>", " ", _html.unescape(d.group(1))))[:1800] if d else ""
        out.append(_role(name, "careers site", _html.unescape(tm.group(1)).strip(), url,
                         where.group(1).strip() if where else "", "", False, "", body))
    return out, True


READERS = {"bamboohr": read_bamboohr, "breezy": read_breezy, "pinpoint": read_pinpoint,
           "teamtailor": read_teamtailor, "jazzhr": read_jazzhr, "jobvite": read_jobvite,
           "rippling": read_rippling, "icims": read_icims, "ukg": read_ukg, "oracle": read_oracle,
           "eightfold": read_eightfold, "adp": read_adp, "jsonld": read_jsonld,
           "radancy": read_radancy, "personio": read_personio, "isolved": read_isolved,
           "trinet": read_trinet, "jobscore": read_jobscore, "page": read_page,
           "phenom": read_phenom, "successfactors": read_successfactors, "sfxml": read_sfxml}
