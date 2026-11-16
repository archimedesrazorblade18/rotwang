#!/usr/bin/env python3
"""
rotwang.py — one command, four sources, ranked by what you can actually sell
============================================================================
Replaces direct_biostat_poller.py and rotwang_sweep.py. Run it, get one CSV
and one HTML report.

WHY IT IS ORGANISED THIS WAY
    Earlier versions sorted by industry. That was wrong. Industry is a fact
    about the employer; your skill is the constant. Survival analysis is
    survival analysis whether it is time-to-progression in oncology,
    time-to-failure on a turbine, or time-to-churn in SaaS. Searching
    "data scientist energy" finds almost nothing; searching
    "reliability statistician" finds the same job wearing different clothes.

    So queries are built from METHODS (what you sell) crossed with ENGAGEMENT
    (how you get paid), and results are grouped by what you would DO about
    them, not by which sector the employer happens to sit in:

        CONTRACT   — posted as contract, contractor, consultant, 1099.
                     Action: apply.
        LEAD       — permanent posting at a company with a proven, budgeted,
                     unfilled need. Action: pitch them fractional help.

    Industry is still recorded, as a column you can filter. It is a facet,
    not the spine.

SOURCES
    boards      Greenhouse / Lever / Ashby public APIs. Deep, exact, but only
                for companies on your list. No key.
    adzuna      A large aggregating index. Wide reach across all sectors and
                countries. Free developer key, asked for once.
    remotive    Remote-only board, free, no key. Listings are delayed ~24h and
                their terms ask that Remotive be credited as the source, so the
                output carries the source and links back to their URL.
    remotejobs  Remote-only board, free, no key.

RUN
    pip install requests
    python rotwang.py                    everything, default depth
    python rotwang.py --setup            re-enter the Adzuna key
    python rotwang.py --depth deep       many more queries
    python rotwang.py --leads            include permanent roles as leads
    python rotwang.py --new-only         only what appeared since last run
    python rotwang.py --min-fit 65       hide weak matches
    python rotwang.py --countries us,gb  more than one market
    python rotwang.py --discover "Tempus" "GRAIL"    resolve board addresses
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field as dcfield
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter

try:
    from urllib3.util.retry import Retry
except Exception:                                    # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

HERE = Path(__file__).resolve().parent

# ══════════════════════════ CONFIG — EDIT HERE ═════════════════════════════════

# ══════════════════════════ PROFILES — ADD YOUR OWN ════════════════════════════
# A profile is the whole definition of a discipline:
#   methods  what the person sells; crossed with ENGAGEMENT to build the queries
#   strong   vocabulary that lifts a role's fit hard
#   good     vocabulary that lifts it a little
#   weak     vocabulary meaning this is the wrong kind of job
#   gate     title words that make a listing worth looking at at all
# Copy a block, change the words, and the whole engine retargets. The app can
# also write rotwang_profile.json straight from your CV, which beats all of these.

PROFILES: dict[str, dict] = {
    "biostatistics": {
        "label": "Biostatistics & clinical research",
        "methods": ["biostatistician", "statistical programmer", "SAS programmer",
                    "survival analysis", "clinical trial statistician", "regulatory statistician",
                    "Bayesian statistician", "adaptive trial design", "real world evidence",
                    "pharmacoepidemiologist", "causal inference", "conformal prediction",
                    "uncertainty quantification", "causal machine learning", "CDISC SDTM ADaM",
                    "clinical data manager", "biometrics lead",
                    "health economics outcomes research", "statistical modeler",
                    "reliability engineer statistics", "quantitative scientist"],
        "strong": ["biostatistic", "statistician", "statistical program", "sas", "oncol",
                   "solid tumor", "rare disease", "regulatory submission", "fda", "ema",
                   "cdisc", "sdtm", "adam dataset", "phase 1", "phase 2", "phase 3",
                   "clinical trial", "biometric", "real world evidence", "real-world evidence",
                   "pharmacoepidemiolog", "survival analysis", "time-to-event", "bayesian",
                   "adaptive design"],
        "good": ["data scientist", "machine learning", "epidemiolog", "python", "sql",
                 "longitudinal", "mixed model", "causal", "forecasting", "reliability",
                 "heor", "clinical data", "protocol", "r programming", "medical affairs",
                 "conformal", "uncertainty quantif", "uplift", "treatment effect",
                 "propensity", "double machine learning", "estimand", "llm evaluation"],
        "weak": ["sales", "account executive", "customer success", "front-end", "devops",
                 "helpdesk", "technician", "phlebotom", "warehouse", "driver", "nurse"],
        "gate": ["statistic", "biostat", "epidemiolog", "data scien", "quantitative",
                 "clinical data", "biometric", "sas ", "bioinformat", "research scientist"],
    },
    "datascience": {
        "label": "Data science & machine learning",
        "methods": ["data scientist", "machine learning engineer", "ML scientist",
                    "applied scientist", "NLP engineer", "computer vision engineer",
                    "analytics engineer", "experimentation scientist", "causal inference",
                    "forecasting scientist", "recommender systems", "MLOps engineer",
                    "research scientist AI", "LLM engineer"],
        "strong": ["machine learning", "deep learning", "data scientist", "nlp",
                   "large language model", "llm", "pytorch", "tensorflow", "causal",
                   "experimentation", "a/b test", "recommender", "forecasting",
                   "computer vision", "mlops", "principal", "staff"],
        "good": ["python", "sql", "spark", "airflow", "statistics", "bayesian", "analytics",
                 "dbt", "snowflake", "kubernetes", "aws", "gcp"],
        "weak": ["sales", "account executive", "recruiter", "customer support",
                 "data entry", "helpdesk", "bookkeeping"],
        "gate": ["data scien", "machine learning", "ml engineer", "ai engineer", "nlp",
                 "analytics", "research scientist", "applied scientist", "statistic"],
    },
    "aistats": {
        "label": "Statistical AI & causal ML",
        "methods": ["causal inference", "experimentation scientist", "conformal prediction",
                    "uncertainty quantification", "causal machine learning",
                    "applied scientist experimentation", "incrementality measurement",
                    "uplift modeling", "Bayesian machine learning", "LLM evaluation scientist",
                    "model evaluation statistician", "research scientist causal",
                    "quasi-experimental design", "reinforcement learning evaluation",
                    "AI research statistician", "geospatial causal inference"],
        "strong": ["causal inference", "causal machine learning", "conformal prediction",
                   "uncertainty quantification", "experimentation", "incrementality",
                   "uplift model", "quasi-experimental", "treatment effect",
                   "instrumental variable", "propensity score", "a/b test",
                   "bayesian", "llm evaluation", "model evaluation", "calibration",
                   "double machine learning", "heterogeneous treatment", "estimand",
                   "principal", "staff", "research scientist", "applied scientist"],
        "good": ["machine learning", "deep learning", "python", "r program", "sql",
                 "statistics", "statistical", "inference", "forecasting", "uncertainty",
                 "reinforcement learning", "large language model", "llm", "pytorch",
                 "uplift", "observational", "quasi experimental", "econometric"],
        "weak": ["sales", "account executive", "recruiter", "customer support",
                 "data entry", "helpdesk", "bookkeeping", "intern", "phlebotom",
                 "front-end", "devops", "warehouse", "driver"],
        # Gate is deliberately METHOD-specific, not "machine learning" broad — that
        # keeps the AI floodgates shut and only lets statistically deep roles through.
        "gate": ["causal", "conformal", "uncertainty quantif", "experimentation",
                 "incrementality", "uplift", "quasi-experiment", "treatment effect",
                 "propensity", "instrumental variable", "double machine learning",
                 "llm eval", "model evaluation", "bayesian", "estimand",
                 "research scientist", "applied scientist", "statistic"],
    },
    "rwd": {
        "label": "Real-world data, biostatistics & health AI",
        "methods": ["real world evidence consultant", "real world data scientist",
                    "biostatistician", "biostatistics consultant", "statistical programmer",
                    "SAS programmer", "statistical programming", "clinical data scientist",
                    "health data scientist", "AI scientist healthcare", "biometrics lead",
                    "computational biologist", "bioinformatics scientist",
                    "pharmacoepidemiologist", "HEOR scientist", "causal inference healthcare",
                    "epidemiologist real world", "principal biostatistician"],
        "strong": ["biostatistic", "statistician", "statistical program", "sas program",
                   "real world evidence", "real-world evidence", "real world data", "rwe",
                   "biometric", "computational biolog", "bioinformatic", "pharmacoepidemiolog",
                   "clinical trial", "oncol", "rare disease", "regulatory submission",
                   "cdisc", "sdtm", "adam", "heor", "survival analysis", "causal inference",
                   "observational study", "external comparator", "phase 2", "phase 3",
                   "genomic", "single cell", "estimand"],
        "good": ["data scientist", "machine learning", "epidemiolog", "python", "r program",
                 "sql", "bayesian", "longitudinal", "mixed model", "conformal",
                 "uncertainty quantif", "clinical data", "medical affairs", "protocol",
                 "claims data", "ehr", "electronic health", "health econom"],
        "weak": ["sales", "account executive", "customer success", "solutions consultant",
                 "business consultant", "recruiting", "recruiter", "talent", "marketing",
                 "front-end", "ios ", "android", "devops", "helpdesk", "phlebotom",
                 "warehouse", "driver", "teleoperation", "annotation", "fraud",
                 "toxicolog", "process engineer", "field application", "sales engineer",
                 "post-sales", "post sales", "implementation consultant", "support specialist"],
        # TWO-PART GATE. A bare "data scientist" or "AI scientist" passes ONLY if a
        # health / life-science signal is also present — that is what strips the
        # fintech, adtech and enterprise-SaaS versions while keeping health roles.
        "gate": ["biostat", "statistical program", "sas program", "biometric",
                 "computational biolog", "bioinformatic", "pharmacoepidemiolog",
                 "real world evidence", "real-world evidence", "real world data",
                 "clinical data scien", "health data scien"],
        "gate_any": ["data scien", "machine learning", "ai scientist", "ai/ml",
                     "research scientist", "quantitative scien", "causal inference",
                     "statistician", "epidemiolog", "biostatistic"],
        "gate_ctx": ["pharma", "biotech", "clinical", "health", "patient", "medical",
                     "drug", "therap", "oncol", "disease", "genom", "biolog", "life scien",
                     "rwe", "real world", "real-world", "epidemiolog", "hospital", "care",
                     "diagnostic", "clinical trial", "fda", "regulatory", "ehr", "claims",
                     "medic", "vaccine", "immun", "cell", "molecul", "biomarker",
                     "cdisc", "sdtm", "adam", "sas", "gxp", "pharmacovig", "trial"],
    },
    "marketing": {
        "label": "Marketing & communications",
        "methods": ["marketing consultant", "brand strategist", "content strategist",
                    "communications consultant", "demand generation manager",
                    "growth marketer", "SEO consultant", "lifecycle marketing",
                    "product marketing manager", "public relations consultant", "copywriter",
                    "campaign manager", "marketing analytics", "social media strategist",
                    "email marketing consultant"],
        "strong": ["brand strategy", "product marketing", "demand generation",
                   "content strategy", "communications", "campaign", "go-to-market",
                   "positioning", "messaging", "public relations", "copywriter",
                   "growth marketing", "lifecycle", "director", "head of marketing"],
        "good": ["seo", "sem", "paid media", "hubspot", "marketo", "salesforce",
                 "google analytics", "email marketing", "social media", "crm", "editorial",
                 "creative direction", "media relations"],
        "weak": ["engineer", "developer", "nurse", "warehouse", "driver", "accountant",
                 "sales development representative", "cold calling"],
        "gate": ["marketing", "communicat", "brand", "content", "copywrit", "campaign",
                 "public relations", "growth", "seo", "social media", "editorial"],
    },
    "design": {
        "label": "Product & UX design",
        "methods": ["product designer", "UX designer", "UX researcher", "design systems lead",
                    "brand designer", "service designer", "interaction designer",
                    "design director", "content designer"],
        "strong": ["product design", "ux design", "user research", "design system",
                   "interaction design", "figma", "prototyping", "service design",
                   "accessibility", "usability", "design director", "principal designer"],
        "good": ["wireframe", "user flow", "information architecture", "design token",
                 "brand identity", "motion design", "front-end", "html", "css"],
        "weak": ["sales", "recruiter", "warehouse", "driver", "nurse", "accountant"],
        "gate": ["design", "ux ", "ui ", "user research", "creative"],
    },
    "engineering": {
        "label": "Software engineering",
        "methods": ["software engineer contractor", "backend engineer", "full stack engineer",
                    "platform engineer", "site reliability engineer", "data engineer",
                    "cloud architect", "security engineer", "mobile engineer",
                    "engineering manager"],
        "strong": ["software engineer", "backend", "full stack", "distributed systems",
                   "platform engineer", "site reliability", "data engineer", "architect",
                   "kubernetes", "microservices", "staff engineer", "principal engineer"],
        "good": ["python", "typescript", "golang", "rust", "java", "aws", "gcp", "azure",
                 "terraform", "postgres", "react", "api", "ci/cd"],
        "weak": ["sales", "recruiter", "support agent", "data entry", "manual tester"],
        "gate": ["engineer", "developer", "architect", "programmer", "sre ", "devops"],
    },
    "finance": {
        "label": "Finance, accounting & actuarial",
        "methods": ["financial consultant", "FP&A consultant", "controller contract",
                    "fractional CFO", "actuary contract", "valuation analyst",
                    "financial modeler", "quantitative analyst", "internal auditor",
                    "revenue operations analyst"],
        "strong": ["fp&a", "financial model", "controller", "cfo", "actuar", "valuation",
                   "quantitative analyst", "treasury", "audit", "ifrs", "us gaap",
                   "revenue recognition", "director of finance"],
        "good": ["excel", "netsuite", "sap", "quickbooks", "sql", "forecasting",
                 "budgeting", "variance analysis", "due diligence"],
        "weak": ["cashier", "teller", "sales", "recruiter", "warehouse", "driver"],
        "gate": ["financ", "account", "actuar", "controller", "audit", "fp&a", "treasury",
                 "quantitative"],
    },
    "legal": {
        "label": "Legal & regulatory",
        "methods": ["contract attorney", "regulatory counsel", "compliance consultant",
                    "privacy counsel", "paralegal contract", "commercial counsel",
                    "fractional general counsel", "regulatory affairs consultant"],
        "strong": ["counsel", "attorney", "regulatory affairs", "compliance", "privacy",
                   "gdpr", "hipaa", "contract negotiation", "litigation", "general counsel"],
        "good": ["paralegal", "due diligence", "policy", "governance", "licensing",
                 "intellectual property", "employment law"],
        "weak": ["sales", "recruiter", "warehouse", "driver", "engineer"],
        "gate": ["legal", "counsel", "attorney", "compliance", "regulatory", "paralegal",
                 "privacy"],
    },
    "operations": {
        "label": "Operations & programme management",
        "methods": ["operations consultant", "program manager contract",
                    "project manager contract", "chief of staff fractional",
                    "supply chain consultant", "process improvement consultant",
                    "business analyst contract", "PMO lead"],
        "strong": ["program manager", "project manager", "operations", "chief of staff",
                   "supply chain", "process improvement", "pmo", "change management",
                   "director of operations", "business analyst"],
        "good": ["agile", "scrum", "lean", "six sigma", "jira", "stakeholder",
                 "vendor management", "roadmap", "kpi"],
        "weak": ["warehouse associate", "driver", "cashier", "recruiter"],
        "gate": ["operations", "program manager", "project manager", "business analyst",
                 "supply chain", "chief of staff", "pmo"],
    },
}
DEFAULT_PROFILE = "biostatistics"

# How you get paid. Crossed with the profile's methods to build the query matrix.
ENGAGEMENT = ["contract", "contractor", "consultant", "freelance", "fractional",
              "1099", "interim"]

# Only used at --depth deep, and only as a suffix. Never the organising principle.
CONTEXTS = ["pharmaceutical", "biotech", "medical device", "health tech", "energy",
            "utilities", "insurance", "government", "technology", "nonprofit"]

# Companies whose own boards get read directly. Grows itself: --discover
# resolves new ones, and companies.json overrides this list entirely.
BOARDS: list[tuple[str, str, str]] = [
    ("greenhouse", "Syndax Pharmaceuticals", "syndaxpharmaceuticals"),
    ("greenhouse", "Natera", "natera"),
    ("greenhouse", "Freenome", "freenome"),
    ("greenhouse", "Komodo Health", "komodohealth"),
    ("greenhouse", "Cohere Health", "coherehealth"),
    ("greenhouse", "Formation Bio", "formationbio"),
    ("greenhouse", "Xaira Therapeutics", "xairatherapeutics"),
    ("greenhouse", "Truveta", "truveta"),
    ("greenhouse", "PathAI", "pathai"),
    ("greenhouse", "Flatiron Health", "flatironhealth"),
    ("greenhouse", "Kymera Therapeutics", "kymeratherapeutics"),
    ("greenhouse", "Qventus", "qventus"),
    ("greenhouse", "HealthVerity", "healthverity"),
    ("greenhouse", "Iterative Health", "iterativehealth"),
    ("greenhouse", "Isomorphic Labs", "isomorphiclabs"),
    ("greenhouse", "Devoted Health", "devotedhealth"),
    ("greenhouse", "Oscar Health", "oscarhealth"),
    ("greenhouse", "Hinge Health", "hingehealth"),
    ("greenhouse", "Omada Health", "omadahealth"),
    ("greenhouse", "Point72", "point72"),
    ("greenhouse", "Two Sigma", "twosigma"),
    ("ashby", "Edison Scientific", "Edison Scientific"),
    ("ashby", "Hippocratic AI", "Hippocratic AI"),
    ("ashby", "Nabla", "Nabla"),
    ("ashby", "OpenEvidence", "OpenEvidence"),
    ("ashby", "Triomics", "Triomics"),
    ("ashby", "Regard", "Regard"),
    ("lever", "Layer Health", "layerhealth"),
]

# Agency, recruiter and CRO names. Matched against the COMPANY NAME only —
# never the URL, because ATS hostnames legitimately contain "recruit".
AGENCIES = [
    "orbis", "advanced clinical", "black diamond", "icon plc", "icon clinical",
    "icon strategic", "iqvia", "parexel", "syneos", "fortrea", "medpace",
    "veristat", "cytel", "actalent", "insight global", "planet pharma",
    "epm scientific", "proclinical", "barrington james", "hobson prior",
    "real staffing", "randstad", "robert half", "kforce", "aerotek", "teksystems",
    "caidya", "novotech", "premier research", "allucent", "worldwide clinical",
    "talentburst", "russell tobin", "the planet group", "jobot", "cybercoders",
    "apex systems", "collabera", "mindlance", "diverse lynx", "manpower",
    "adecco", "hays ", "michael page", "motion recruit", "staffing", "recruiter",
    "recruitment", "recruiting", "talent solutions", "talent group",
    "consulting group", "search partners", "executive search",
    # Present in real output and previously unmatched:
    "clinchoice", "beaufort", "edetek", "atrium works", "system one", "blue matter",
    "united biosource", "sci.bio", "scibio", "medix", "gforce", "penfield", "artech",
    "yoh ", "rangam", "spectraforce", "amerit", "eteam", "judge group", "kelly services",
    "lancesoft", "integrated resources", "axelon", "nesco", "pyramid consulting",
    "acro service", "tandym", "solomon page", "career center", "job board",
]

PENALTY = ["intern", "internship", "entry level", "entry-level", "junior",
           "graduate scheme", "apprentice", "0-2 years", "1-3 years"]

# Roles worth surfacing even when nothing else matches.
EXCLUDE_TITLE = ["intern", "internship", "co-op"]

CONTRACT_WORDS = ["contract", "contractor", "consultant", "consulting", "freelance",
                  "fractional", "temporary", "interim", "1099", "locum", "temp "]
# Phrases where "contract" describes the WORK, not the engagement. Previously
# applied to body text only, so "Contracts Manager", "Deals Contract Management"
# and "AD, Market Access Contracting" all registered as contract roles.
CONTRACT_FALSE_FRIENDS = re.compile(
    r"contract research organi|contract manufactur|contract negotiat|contract review|"
    r"contract lifecycle|contracting officer|contract law|contracts? manage|"
    r"market access contracting|contract recruit|contract special|contract administrat|"
    r"contract analyst|deals contract|contract coordinator|contract compliance", re.I)
CONTRACT_BODY_RE = re.compile(
    r"\bthis (?:is|role is|position is) an? (?:\d+[\s-]*(?:month|week)[\s-]*)?"
    r"(?:contract|contractor|temporary|freelance|fractional)\b"
    r"|\bcontract[- ]to[- ]hire\b|\bcontract position\b"
    r"|\b(?:1099|w-?2)\s+contractor\b|\b\d+[\s-]*(?:month|week)[\s-]*contract\b", re.I)

# ═══════════════════════════════ PLUMBING ══════════════════════════════════════

KEYS = HERE / "rotwang_keys.json"
STATE = Path(os.getenv("ROTWANG_STATE", HERE / "rotwang_seen.json"))
CSV_OUT = Path(os.getenv("ROTWANG_CSV", HERE / "rotwang_roles.csv"))
HTML_OUT = Path(os.getenv("ROTWANG_HTML", HERE / "rotwang_report.html"))
BOARDS_OUT = HERE / "rotwang_boards.json"      # resolved boards + ruled-out names
TARGETS_FILE = HERE / "rotwang_targets.json"  # company and agency NAMES to resolve
GROW_PER_RUN = int(os.getenv("ROTWANG_GROW", "40"))   # names probed each run
RETRY_MISSED_DAYS = 30                        # re-probe a miss after this long
BOARD_STRIKES = 3                             # consecutive failures before retiring
APPS_CSV = HERE / "rotwang_applications.csv"
CACHE_DIR = HERE / ".rotwang_cache"
CACHE_TTL = int(os.getenv("ROTWANG_CACHE_TTL", "1800"))     # seconds
TIMEOUT, WORKERS = 25, 16

EMAIL_TO = os.getenv("ROTWANG_EMAIL_TO", "")
SMTP_HOST = os.getenv("ROTWANG_SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("ROTWANG_SMTP_PORT", "587"))
SMTP_USER = os.getenv("ROTWANG_SMTP_USER", "")
SMTP_PASS = os.getenv("ROTWANG_SMTP_PASS", "")

_cj = HERE / "companies.json"
if _cj.exists():
    try:
        BOARDS = [tuple(x) if len(x) == 3 else (x["ats"], x["name"], x["token"])
                  for x in json.loads(_cj.read_text())]
    except Exception as e:
        print(f"  companies.json ignored ({e})")


def active_profile(name: str) -> dict:
    """rotwang_profile.json is written by the app from your actual CV. When it
    exists it wins, because a profile derived from the real document beats any
    built-in guess. That is how this scales past one person's discipline."""
    f = HERE / "rotwang_profile.json"
    if f.exists():
        try:
            p = json.loads(f.read_text())
            if p.get("methods") and p.get("strong"):
                p.setdefault("label", "From your CV")
                for k in ("good", "weak", "gate"):
                    p.setdefault(k, [])
                if not p["gate"]:
                    p["gate"] = [m.split()[0].lower() for m in p["methods"][:10]]
                return p
        except Exception as e:
            print(f"  rotwang_profile.json ignored ({e})")
    return PROFILES.get(name, PROFILES[DEFAULT_PROFILE])


@dataclass
class Role:
    title: str = ""
    company: str = ""
    location: str = ""
    pay: str = ""
    contract: bool = False
    agency: bool = False
    remote: bool = False
    method: str = ""
    industry: str = ""
    source: str = ""
    posted: str = ""
    url: str = ""
    body: str = ""
    fit: int = 0
    hits: list[str] = dcfield(default_factory=list)

    @property
    def key(self) -> str:
        return (self.url or f"{self.company}|{self.title}").split("?")[0].rstrip("/")

    @property
    def dkey(self) -> str:
        """Content identity. Aggregators hand out a fresh tracking URL per query,
        so URL-only de-duplication let one Labcorp posting through fourteen times."""
        n = lambda x: re.sub(r"[^a-z0-9]+", " ", (x or "").lower()).strip()
        return f"{n(self.company)}|{n(self.title)}"

    @property
    def channel(self) -> str:
        return "AGENCY" if self.agency else "DIRECT"

    @property
    def kind(self) -> str:
        return "contract" if self.contract else "lead"


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "rotwang/3.0", "Accept": "application/json"})
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=2, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504]),
        pool_maxsize=WORKERS * 2))
    return s


def cached_json(s, url, params=None, fresh=False):
    """A board's contents don't change minute to minute, so a re-run inside the
    TTL costs nothing. Returns None on any failure — callers read that as
    'source unreachable', never as an exception."""
    tag = hashlib.sha1((url + json.dumps(params or {}, sort_keys=True)).encode()).hexdigest()[:20]
    f = CACHE_DIR / f"{tag}.json"
    if not fresh and f.exists() and time.time() - f.stat().st_mtime < CACHE_TTL:
        try:
            return json.loads(f.read_text())
        except Exception:
            pass
    try:
        r = s.get(url, params=params, timeout=TIMEOUT)
        if r.status_code == 429:
            time.sleep(5)
            r = s.get(url, params=params, timeout=TIMEOUT)
        if not r.ok:
            return None
        d = r.json()
    except (requests.RequestException, ValueError):
        return None
    try:
        CACHE_DIR.mkdir(exist_ok=True)
        f.write_text(json.dumps(d))
    except Exception:
        pass
    return d


clean = lambda h: re.sub(r"\s+", " ", re.sub(r"&[a-z#0-9]+;", " ",
                         re.sub(r"<[^>]+>", " ", h or ""))).strip()


def is_agency(company: str) -> bool:
    c = (company or "").lower()
    return any(a in c for a in AGENCIES)


def is_contract(title: str, etype: str = "", body: str = "",
                trust_etype: bool = True) -> bool:
    """trust_etype=False for aggregators. Adzuna's contract_type said "contract"
    on 230 of 230 rows in a real run, including Director and Manager postings at
    Regeneron and Madrigal. Its label is not evidence on its own."""
    t = CONTRACT_FALSE_FRIENDS.sub(" ", (title or "").lower())
    if any(w in t for w in CONTRACT_WORDS):
        return True
    if trust_etype:
        e = CONTRACT_FALSE_FRIENDS.sub(" ", (etype or "").lower())
        if any(w in e for w in CONTRACT_WORDS):
            return True
    return bool(CONTRACT_BODY_RE.search(CONTRACT_FALSE_FRIENDS.sub(" ", body or "")))


def wanted(title: str) -> bool:
    # whole words: "intern" must not reject "International" or "Internal Medicine"
    return not re.search(r"\b(intern|interns|internship|co-op)\b", title or "", re.I)


def guess_industry(text: str) -> str:
    t = (text or "").lower()
    for label, keys in [
        ("Pharma & biotech", ["pharma", "biotech", "therapeutic", "clinical trial",
                              "oncolog", "drug develop", "fda", "gene therapy"]),
        ("Diagnostics & devices", ["diagnostic", "medical device", "genomic",
                                   "assay", "in vitro", "imaging"]),
        ("Health tech & payers", ["health system", "payer", "provider", "ehr",
                                  "electronic health", "telehealth", "hospital",
                                  "health plan", "clinical ai"]),
        ("Energy & industry", ["energy", "utility", "grid", "turbine", "oil",
                               "renewable", "power plant", "reliability", "manufactur"]),
        ("Finance & insurance", ["hedge fund", "insurance", "actuar", "trading",
                                 "asset management", "underwrit"]),
        ("Public & academic", ["university", "institute", "government", "public health",
                               "nonprofit", "ministry", "national lab"]),
    ]:
        if any(k in t for k in keys):
            return label
    return "Other"


PAY_RE = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?\s?[kK]?\s?(?:[-–—]|to)\s?\$?\s?\d[\d,]*(?:\.\d+)?\s?[kK]?"
    r"[^.\n]{0,18}|\$\s?\d[\d,]{2,}(?:\.\d+)?\s?[kK]?\s?(?:/\s?(?:hr|hour|yr|year)"
    r"|per\s(?:hour|year))", re.I)


def extract_pay(text: str) -> str:
    m = PAY_RE.search(text or "")
    return re.sub(r"\s+", " ", m.group(0)).strip() if m else ""


def pay_value(p: str) -> int:
    """Rough annual-equivalent, for ranking. -1 when nothing is stated."""
    if not p:
        return -1
    s = p.lower().replace(",", "")
    nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", s) if float(n) > 5]
    if not nums:
        return -1
    # Midpoint, not ceiling: a wide band should not outrank a higher firm number.
    v = (min(nums) + max(nums)) / 2 if len(nums) >= 2 else nums[0]
    if re.search(r"\d\s?k\b", s):
        v *= 1000
    if re.search(r"/\s?h|per hour|hourly|\bhr\b|an hour", s) or v < 500:
        v *= 2080
    return int(v)


def score(r: Role, prof: dict) -> tuple[int, list[str]]:
    blob = f"{r.title} {r.company} {r.location} {r.body[:1500]}".lower()
    s, hits = 50, []
    for k in prof["strong"]:
        if k in blob:
            s += 5
            if len(hits) < 6:
                hits.append(k)
    for k in prof.get("good", []):
        if k in blob:
            s += 3
    for k in prof.get("weak", []):
        if k in blob:
            s -= 6
    for k in PENALTY:
        if k in blob:
            s -= 15
    if r.contract:
        s += 8
    if r.remote:
        s += 3
    # Adzuna marks modelled salaries "est.". 113 of 230 rows in a real run were
    # estimates, and they were scoring as though the employer had published a rate.
    if r.pay and "est." not in r.pay:
        s += 4
        if pay_value(r.pay) > 200_000:
            s += 5
    if r.agency:
        s -= 6
    return max(5, min(99, s)), hits


def finish(r: Role, prof: dict) -> Role:
    r.title, r.company = clean(r.title), clean(r.company)
    r.body = clean(r.body)[:4000]
    if not r.pay:
        r.pay = extract_pay(r.body)
    r.industry = guess_industry(f"{r.company} {r.title} {r.body[:900]}")
    r.agency = is_agency(r.company)
    r.fit, r.hits = score(r, prof)
    return r


# ─────────────────────────────── SOURCES ──────────────────────────────────────

def build_queries(prof: dict, depth: str) -> list[str]:
    """The query matrix. Coverage scales from two short lists you can maintain,
    instead of a flat list of phrases someone typed once."""
    q: list[str] = []
    methods = prof["methods"]
    core = methods[:12]
    for m in core:                                   # tier 1 — always
        q += [f"{e} {m}" for e in ENGAGEMENT[:3]]
    if depth in ("normal", "deep"):                  # tier 2
        q += methods
        q += [f"{e} {m}" for e in ENGAGEMENT[3:] for m in core[:6]]
    if depth == "deep":                              # tier 3
        q += [f"{m} {c}" for m in core[:8] for c in CONTEXTS]
    seen, out = set(), []
    for x in q:
        k = x.lower().strip()
        if k not in seen:
            seen.add(k); out.append(x.strip())
    return out


# Every unit of work returns (label, roles, ok) and goes into ONE flat thread
# pool. v3 ran boards, then the index, then the free boards, in three sequential
# phases with a sleep between every call — that was where the minutes went.

def task_board(s, entry, fresh):
    ats, name, token = entry
    if ats in ("smartrecruiters", "workable", "recruitee"):
        return task_board_extra(s, entry, fresh)
    if ats == "greenhouse":
        d = cached_json(s, f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs",
                        {"content": "true"}, fresh)
        if d is None:
            return ("board:" + name, [], False)
        out = []
        for j in d.get("jobs", []):
            if not wanted(j.get("title", "")):
                continue
            body = clean(j.get("content"))
            loc = (j.get("location") or {}).get("name", "")
            out.append(Role(title=j.get("title", ""), company=name, location=loc,
                            contract=is_contract(j.get("title", ""), "", body),
                            remote="remote" in loc.lower(), source=f"{name} · Greenhouse",
                            url=j.get("absolute_url", ""), body=body,
                            posted=(j.get("updated_at") or "")[:10]))
        return ("board:" + name, out, True)

    if ats == "lever":
        d = cached_json(s, f"https://api.lever.co/v0/postings/{token}", {"mode": "json"}, fresh)
        if d is None or not isinstance(d, list):
            return ("board:" + name, [], False)
        out = []
        for j in d:
            if not wanted(j.get("text", "")):
                continue
            c = j.get("categories") or {}
            out.append(Role(title=j.get("text", ""), company=name, location=c.get("location", ""),
                            contract=is_contract(j.get("text", ""), c.get("commitment", ""),
                                                 j.get("descriptionPlain", "")),
                            remote="remote" in (c.get("location", "") or "").lower(),
                            source=f"{name} · Lever", url=j.get("hostedUrl", ""),
                            body=j.get("descriptionPlain", "")))
        return ("board:" + name, out, True)

    if ats == "ashby":
        d = cached_json(s, f"https://api.ashbyhq.com/posting-api/job-board/{requests.utils.quote(token)}",
                        {"includeCompensation": "true"}, fresh)
        if d is None:
            return ("board:" + name, [], False)
        out = []
        for j in d.get("jobs", []):
            if not wanted(j.get("title", "")):
                continue
            body = clean(j.get("descriptionHtml") or j.get("descriptionPlain") or "")
            out.append(Role(title=j.get("title", ""), company=name, location=j.get("location", ""),
                            pay=(j.get("compensation") or {}).get("compensationTierSummary", "") or "",
                            contract=is_contract(j.get("title", ""), j.get("employmentType", ""), body),
                            remote=bool(j.get("isRemote")), source=f"{name} · Ashby",
                            url=j.get("jobUrl") or j.get("applyUrl", ""), body=body))
        return ("board:" + name, out, True)

    return ("board:" + name, [], False)


def task_index(s, app_id, app_key, country, query, page, days, contract_flag, fresh):
    p = {"app_id": app_id, "app_key": app_key, "results_per_page": 50, "what": query,
         "content-type": "application/json", "sort_by": "date"}
    if days:
        p["max_days_old"] = days
    if contract_flag:
        p["contract"] = 1
    d = cached_json(s, f"https://api.adzuna.com/v1/api/jobs/{country}/search/{page}", p, fresh)
    if d is None:
        return (f"index:{country}", [], False)
    out = []
    for j in d.get("results", []):
        t = clean(j.get("title", ""))
        if not wanted(t):
            continue
        body = clean(j.get("description", ""))
        lo, hi = j.get("salary_min"), j.get("salary_max")
        pay = ""
        if lo or hi:
            f = lambda v: f"${int(v):,}"
            pay = f(lo) if (not hi or lo == hi) else f"{f(lo)}–{f(hi)}"
            if j.get("salary_is_predicted") in (1, "1", True):
                pay += " est."
        loc = (j.get("location") or {}).get("display_name", "")
        out.append(Role(title=t, company=(j.get("company") or {}).get("display_name", ""),
                        location=loc, pay=pay,
                        contract=is_contract(t, j.get("contract_type", "") or "", body,
                                             trust_etype=False),
                        remote=bool(re.search(r"remote|anywhere|work from home", f"{t} {loc} {body}", re.I)),
                        source=f"Adzuna {country.upper()}", posted=(j.get("created", "") or "")[:10],
                        url=j.get("redirect_url", ""), body=body))
    return (f"index:{country}", out, True)


def task_remotive(s, fresh):
    """Free, no key. Remotive's terms ask to be credited and linked back to,
    which the source column and the url field both do."""
    d = cached_json(s, "https://remotive.com/api/remote-jobs", None, fresh)
    if d is None:
        return ("remotive", [], False)
    out = []
    for j in d.get("jobs", []):
        t = clean(j.get("title", ""))
        if not wanted(t):
            continue
        body = clean(j.get("description", ""))
        out.append(Role(title=t, company=clean(j.get("company_name", "")),
                        location=j.get("candidate_required_location", "") or "Remote",
                        pay=j.get("salary", "") or "",
                        contract=is_contract(t, j.get("job_type", "") or "", body),
                        remote=True, source="Remotive", url=j.get("url", ""), body=body,
                        posted=(j.get("publication_date", "") or "")[:10]))
    return ("remotive", out, True)


def task_remotejobs(s, page, fresh):
    d = cached_json(s, "https://remotejobs.org/api/v1/jobs", {"limit": 100, "page": page}, fresh)
    if d is None:
        return ("remotejobs", [], False)
    out = []
    for j in d.get("data", []):
        t = clean(j.get("title", ""))
        if not wanted(t):
            continue
        body = clean(j.get("description", ""))
        out.append(Role(title=t, company=clean((j.get("company") or {}).get("name", "")),
                        location=j.get("location", "Remote"), pay=j.get("salary_text", "") or "",
                        contract=is_contract(t, j.get("type", "") or "", body), remote=True,
                        source="RemoteJobs.org", url=j.get("apply_url") or j.get("url", ""),
                        body=body, posted=(j.get("posted_at", "") or "")[:10]))
    return ("remotejobs", out, True)


def task_board_extra(s, entry, fresh):
    """SmartRecruiters, Workable and Recruitee. Added because recruiters and
    mid-size pharma sit on these far more often than on Greenhouse."""
    ats, name, token = entry
    if ats == "smartrecruiters":
        d = cached_json(s, f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
                        {"limit": 100}, fresh)
        if d is None:
            return ("board:" + name, [], False)
        out = []
        for j in d.get("content", []):
            t = clean(j.get("name", ""))
            if not wanted(t):
                continue
            lo = j.get("location") or {}
            loc = ", ".join(x for x in [lo.get("city"), lo.get("region"), lo.get("country")] if x)
            out.append(Role(title=t, company=name, location=loc,
                            contract=is_contract(t, (j.get("typeOfEmployment") or {}).get("label", "")),
                            remote=bool(lo.get("remote")), source=f"{name} · SmartRecruiters",
                            url=f"https://jobs.smartrecruiters.com/{token}/{j.get('id','')}",
                            posted=(j.get("releasedDate") or "")[:10]))
        return ("board:" + name, out, True)

    if ats == "workable":
        d = cached_json(s, f"https://apply.workable.com/api/v1/widget/accounts/{token}",
                        {"details": "true"}, fresh)
        if d is None:
            return ("board:" + name, [], False)
        out = []
        for j in d.get("jobs", []):
            t = clean(j.get("title", ""))
            if not wanted(t):
                continue
            body = clean(j.get("description", ""))
            loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x)
            out.append(Role(title=t, company=name, location=loc,
                            contract=is_contract(t, j.get("employment_type", ""), body),
                            remote="remote" in f"{loc} {t}".lower(),
                            source=f"{name} · Workable", body=body,
                            url=j.get("shortlink") or j.get("url", "")))
        return ("board:" + name, out, True)

    if ats == "recruitee":
        d = cached_json(s, f"https://{token}.recruitee.com/api/offers/", None, fresh)
        if d is None:
            return ("board:" + name, [], False)
        out = []
        for j in d.get("offers", []):
            t = clean(j.get("title", ""))
            if not wanted(t):
                continue
            body = clean(j.get("description", ""))
            loc = j.get("location", "") or ""
            out.append(Role(title=t, company=name, location=loc,
                            contract=is_contract(t, j.get("employment_type_code", ""), body),
                            remote="remote" in f"{loc} {t}".lower(),
                            source=f"{name} · Recruitee", body=body,
                            url=j.get("careers_url") or j.get("url", "")))
        return ("board:" + name, out, True)

    return ("board:" + name, [], False)


def gate(r: Role, prof: dict) -> bool:
    """Discipline gate, now run on EVERY source. Two ways to pass:

      1. the title contains an unambiguous term (gate) — e.g. "biostatistician",
         "statistical programmer", "computational biologist"; or
      2. the title contains a broad term (gate_any) that only survives when a
         health/life-science context word (gate_ctx) is also present anywhere in
         the title, company, or body.

    Rule 2 is what keeps a health data-scientist or health causal-inference role
    while dropping the identical title at a bank, ad network, or SaaS company.
    A weak-word title (Solutions Consultant, Recruiter, iOS Engineer) is rejected
    outright, which is what stops company-board leakage.
    """
    t = r.title.lower()
    weak = prof.get("weak", [])
    if any(w in t for w in weak):
        # allow only if the title ALSO carries a strong discipline term
        if not any(k in t for k in prof.get("gate", [])):
            return False
    if any(k in t for k in prof.get("gate", [])):
        return True
    any_terms = prof.get("gate_any")
    if any_terms and any(k in t for k in any_terms):
        ctx = prof.get("gate_ctx", [])
        if not ctx:
            return True
        blob = f"{r.title} {r.company} {r.industry} {r.body[:600]}".lower()
        return any(c in blob for c in ctx)
    # profiles without a gate_any keep the old single-list behaviour
    return False if any_terms else any(k in t for k in prof.get("gate", []))


# ────────────────────────────── DISCOVER ──────────────────────────────────────

# Words that describe what a company IS rather than what it is called. Board
# addresses usually drop them: "Day One Biopharmaceuticals" answers on dayonebio,
# "Penfield Search Partners" on penfieldsearch.
_FILLER = {"pharmaceuticals", "pharmaceutical", "pharma", "therapeutics", "therapeutic",
           "biosciences", "bioscience", "biopharmaceuticals", "biopharma", "biotherapeutics",
           "sciences", "science", "medicines", "medicine", "technologies", "technology",
           "holdings", "group", "partners", "associates", "consulting", "consultants",
           "search", "staffing", "recruitment", "recruiting", "solutions", "services",
           "international", "global", "worldwide", "inc", "llc", "ltd", "plc", "corp",
           "corporation", "company", "labs", "laboratories", "laboratory", "systems",
           "healthcare", "resources", "institute", "foundation", "center", "centre"}


def slugs(name: str) -> list[str]:
    """Plausible board addresses for a company, most likely first. Capped, because
    every extra candidate costs one request per ATS across the whole target list."""
    raw = (name or "").strip()
    clean_name = raw.lower().replace("&", " and ")
    words = [w for w in re.split(r"[^a-z0-9]+", clean_name) if w]
    if not words:
        return []
    kept = [w for w in words if w not in _FILLER] or words

    join = lambda ws: "".join(ws)
    hyph = lambda ws: "-".join(ws)
    cands = [
        raw,                       # Ashby boards are usually the display name
        join(words),
        join(kept),
        hyph(words),
        hyph(kept),
        join(words[:2]) if len(words) > 2 else "",
        kept[0] if len(kept[0]) >= 4 else "",
        join(kept) + "bio" if len(kept) == 1 else "",
    ]
    out = []
    for c in cands:
        c = c.strip()
        if c and len(c) >= 3 and c not in out:
            out.append(c)
    return out[:6]


def _probe_urls(token: str) -> list[tuple[str, str]]:
    q = requests.utils.quote(token)
    return [
        ("ashby", f"https://api.ashbyhq.com/posting-api/job-board/{q}"),
        ("greenhouse", f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"),
        ("lever", f"https://api.lever.co/v0/postings/{token}?mode=json"),
        ("smartrecruiters", f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=1"),
        ("workable", f"https://apply.workable.com/api/v1/widget/accounts/{token}"),
        ("recruitee", f"https://{token}.recruitee.com/api/offers/"),
    ]


def probe_name(s, name: str):
    """Try every plausible address for one company across six ATSes.
    Returns the first that answers with at least one open posting."""
    for token in slugs(name):
        if not token or len(token) < 2:
            continue
        for ats, url in _probe_urls(token):
            if ats == "recruitee" and not re.fullmatch(r"[a-z0-9-]+", token):
                continue
            try:
                r = s.get(url, timeout=12)
            except requests.RequestException:
                continue
            if r.status_code != 200:
                continue
            try:
                d = r.json()
            except ValueError:
                continue
            if isinstance(d, list):
                n = len(d)
            elif isinstance(d, dict):
                n = len(d.get("jobs") or d.get("content") or d.get("offers") or [])
            else:
                n = 0
            if n:
                return {"ats": ats, "name": name, "token": token, "open": n}
    return None


# ── the board store: what has been resolved, what has been ruled out ─────────

def load_store() -> dict:
    if not BOARDS_OUT.exists():
        return {"resolved": [], "missed": {}}
    try:
        d = json.loads(BOARDS_OUT.read_text(encoding="utf-8"))
    except Exception:
        return {"resolved": [], "missed": {}}
    if isinstance(d, list):                       # v4 wrote a plain list
        return {"resolved": d, "missed": {}}
    d.setdefault("resolved", []); d.setdefault("missed", {})
    return d


def save_store(store: dict) -> None:
    BOARDS_OUT.write_text(json.dumps(store, indent=1), encoding="utf-8")


def all_boards(store: dict) -> list[tuple[str, str, str]]:
    """Built-in seeds plus everything discovery has resolved, de-duplicated."""
    out, seen = [], set()
    for ats, name, token in BOARDS:
        k = f"{ats}|{token.lower()}"
        if k not in seen:
            seen.add(k); out.append((ats, name, token))
    for b in store.get("resolved", []):
        if b.get("retired"):
            continue
        k = f"{b['ats']}|{str(b['token']).lower()}"
        if k not in seen:
            seen.add(k); out.append((b["ats"], b["name"], b["token"]))
    return out


def load_targets() -> list[str]:
    if not TARGETS_FILE.exists():
        return []
    try:
        d = json.loads(TARGETS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  rotwang_targets.json ignored ({e})")
        return []
    groups = d.get("targets", d) if isinstance(d, dict) else {}
    names = []
    for v in groups.values():
        if isinstance(v, list):
            names += [str(x) for x in v]
    return names


def grow_boards(store: dict, budget: int, quiet: bool = False) -> int:
    """Resolve a slice of the target list. Runs every time, so the board list
    grows on its own instead of waiting for someone to run --discover."""
    targets = load_targets()
    if not targets or budget <= 0:
        return 0
    known = {b["name"] for b in store.get("resolved", [])} | {n for _, n, _ in BOARDS}
    missed = store.get("missed", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETRY_MISSED_DAYS)).strftime("%Y-%m-%d")
    todo = [n for n in targets
            if n not in known and missed.get(n, "0000-00-00") < cutoff][:budget]
    if not todo:
        return 0
    if not quiet:
        print(f"  growing the board list: probing {len(todo)} of "
              f"{len([n for n in targets if n not in known])} unresolved names")
    s = session()
    hits = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for name, res in zip(todo, ex.map(lambda n: probe_name(s, n), todo)):
            if res:
                res["found"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                res["fails"] = 0
                store["resolved"].append(res)
                missed.pop(name, None)
                hits += 1
                if not quiet:
                    print(f"    + {res['name'][:30]:30} {res['ats']:15} "
                          f"{str(res['token'])[:26]:26} {res['open']} open")
            else:
                missed[name] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store["missed"] = missed
    save_store(store)
    if not quiet:
        print(f"  {hits} new board(s) resolved, {len(todo) - hits} without a public board\n")
    return hits


def discover(names: list[str]) -> int:
    s = session()
    store = load_store()
    found = []
    print(f"\nProbing board addresses for {len(names)} companies\n")
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for name, res in zip(names, ex.map(lambda n: probe_name(s, n), names)):
            if res:
                print(f"  found  {res['name']:28} {res['ats']:15} {str(res['token']):26} {res['open']} open")
                found.append([res["ats"], res["name"], res["token"]])
                res["found"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                res["fails"] = 0
                if res["name"] not in {b["name"] for b in store["resolved"]}:
                    store["resolved"].append(res)
            else:
                print(f"  none   {name}")
    save_store(store)
    print(f"\n{len(found)} resolved. {BOARDS_OUT.name} now holds "
          f"{len(store['resolved'])} board(s); they are polled from the next run on.")
    return 0


# ─────────────────────────────── OUTPUT ───────────────────────────────────────

def adzuna_keys(force=False) -> tuple[str, str]:
    if not force:
        a, b = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
        if a and b:
            return a, b
        if KEYS.exists():
            try:
                d = json.loads(KEYS.read_text())
                if d.get("app_id"):
                    return d["app_id"], d["app_key"]
            except Exception:
                pass
    # Scheduled runs have no keyboard. Never block waiting for one.
    if not sys.stdin or not sys.stdin.isatty():
        print("  No Adzuna key in the environment and nobody to ask — running the")
        print("  boards and free sources only. Set ADZUNA_APP_ID / ADZUNA_APP_KEY.")
        return "", ""
    print("\n  The wide index needs a free Adzuna key (about a minute to get).")
    print("  https://developer.adzuna.com/signup — confirm your email, open the")
    print("  dashboard, copy the two values. Press Enter twice to skip and use")
    print("  the free sources only.\n")
    try:
        a = input("  App ID  : ").strip()
        b = input("  App Key : ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n  Skipped.\n")
        return "", ""
    if not (a and b):
        print("  Skipping the index. Boards and free remote sources still run.\n")
        return "", ""
    KEYS.write_text(json.dumps({"app_id": a, "app_key": b}, indent=2))
    print(f"  Saved to {KEYS.name}.\n")
    return a, b


def write_csv(roles: list[Role]) -> None:
    # utf-8-sig, not utf-8: without the byte-order mark both Excel and Windows
    # PowerShell decode the file as Windows-1252 and every "·" arrives as "Â·".
    with CSV_OUT.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["fit", "action", "channel", "title", "company", "pay", "location",
                    "remote", "industry", "posted", "source", "why", "url"])
        for r in roles:
            w.writerow([r.fit, "APPLY" if r.contract else "PITCH", r.channel, r.title,
                        r.company, r.pay, r.location, "yes" if r.remote else "",
                        r.industry, r.posted, r.source, "; ".join(r.hits), r.url])
    print(f"  {CSV_OUT.name}   {len(roles)} role(s)")


REPORT_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ROTWANG — __META_LABEL__</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Big+Shoulders+Display:wght@400;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;450;500;600&display=swap');

:root{
  --steel:#111820; --steel-2:#1A242E; --hair:#3A4A5A;
  --bone:#EAE4D8; --bone-2:#DED6C6; --bone-3:#CFC5B1;
  --ink:#1C232A; --ink-soft:#4E5862;
  --brass:#C08A2E; --brass-lite:#E0B45E; --brass-ink:#7A5310; --brass-graph:#9A6E1E;
  --rust:#A8442B;
  --display:'Big Shoulders Display','Arial Narrow',sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,Consolas,monospace;
  --sans:'IBM Plex Sans',system-ui,-apple-system,Segoe UI,sans-serif;
}
*{box-sizing:border-box;}
html,body{margin:0;padding:0;}
body{background:var(--bone);color:var(--ink);font-family:var(--sans);font-size:14px;
  line-height:1.5;-webkit-font-smoothing:antialiased;}
:focus-visible{outline:2px solid var(--brass);outline-offset:2px;border-radius:2px;}

/* ---------- control band ---------- */
.band{background:var(--steel);color:var(--bone);padding:18px 0 0;
  box-shadow:inset 0 -1px 0 rgba(192,138,46,.35);position:sticky;top:0;z-index:10;}
.inner{max-width:1340px;margin:0 auto;padding:0 22px;}
.top{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;}
.mark{font-family:var(--display);font-weight:700;font-size:38px;line-height:.9;
  letter-spacing:.055em;color:var(--brass);}
.sub{font-family:var(--mono);font-size:10.5px;letter-spacing:.2em;text-transform:uppercase;color:#8A94A0;}
.tally{margin-left:auto;font-family:var(--mono);font-size:11.5px;letter-spacing:.08em;color:#A9B3BF;}
.tally b{color:var(--bone);font-weight:600;}

.controls{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:14px;}
.ctl{font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
  color:#B4BEC9;background:var(--steel-2);border:1px solid var(--hair);border-radius:3px;
  padding:7px 11px;cursor:pointer;transition:.13s;white-space:nowrap;}
.ctl:hover{color:var(--bone);border-color:var(--brass);}
.ctl[aria-pressed="true"]{color:var(--steel);background:var(--brass);border-color:var(--brass);font-weight:600;}
.ctl.primary{color:var(--steel);background:var(--brass-lite);border-color:var(--brass-lite);font-weight:600;}
.ctl.primary:hover{background:var(--brass);}
.find{flex:1 1 190px;min-width:160px;background:var(--steel-2);color:var(--bone);
  border:1px solid var(--hair);border-radius:3px;padding:8px 11px;font-size:13.5px;font-family:var(--sans);}
.find::placeholder{color:#6E7986;}
.find:focus{border-color:var(--brass);outline:none;}
.sel{background:var(--steel-2);color:var(--bone);border:1px solid var(--hair);border-radius:3px;
  padding:7px 9px;font-size:12px;font-family:var(--mono);max-width:190px;}
.lbl{font-family:var(--mono);font-size:10px;letter-spacing:.16em;text-transform:uppercase;color:#8A94A0;}
.sep{width:1px;height:22px;background:var(--hair);}
input[type=range]{accent-color:var(--brass);width:92px;}

/* ---------- sortable header ---------- */
.head{display:grid;border-bottom:1px solid rgba(192,138,46,.35);}
.h{display:flex;align-items:center;gap:5px;padding:9px 12px;position:relative;cursor:pointer;
  font-family:var(--display);font-weight:600;font-size:15px;letter-spacing:.13em;
  text-transform:uppercase;color:#8A94A0;background:none;border:0;transition:color .13s;text-align:left;}
.h:hover{color:var(--bone);}
.h.on{color:var(--brass-lite);}
.h.on::after{content:"";position:absolute;left:10px;right:10px;bottom:-1px;height:2px;background:var(--brass);}
.h.r{justify-content:flex-end;padding-right:20px;}
.h.c{justify-content:center;}
.caret{flex-shrink:0;}

/* ---------- sheet ---------- */
.sheet{max-width:1340px;margin:0 auto;padding:0 22px 70px;}
.grp{display:flex;align-items:center;gap:10px;width:100%;padding:20px 10px 7px;cursor:pointer;
  font-family:var(--mono);font-size:10.5px;letter-spacing:.18em;text-transform:uppercase;
  color:var(--ink-soft);background:none;border:0;text-align:left;}
.grp b{color:var(--ink);font-weight:600;}
.grp .rule{flex:1;height:1px;background:var(--bone-3);}

.row{display:grid;align-items:center;border-bottom:1px solid var(--bone-2);cursor:pointer;transition:background .12s;}
.row:hover,.row.open{background:#E3DCCC;}
.row.done{opacity:.5;}
.cell{padding:11px 12px;min-width:0;}
.cell.r{text-align:right;} .cell.c{text-align:center;}
.fitwrap{position:relative;display:inline-flex;align-items:center;justify-content:center;}
.fitwrap b{position:absolute;font-family:var(--display);font-weight:700;font-size:16px;
  line-height:1;color:var(--brass-ink);}
.dial-track{fill:none;stroke:var(--bone-3);stroke-width:3;}
.dial-arc{fill:none;stroke:var(--brass-graph);stroke-width:3;stroke-linecap:round;}
.title{font-weight:500;font-size:14.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.loc{font-size:11.5px;color:var(--ink-soft);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.co{font-size:13.5px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;display:block;}
.rate{font-family:var(--mono);font-size:12.5px;font-weight:500;white-space:nowrap;}
.rate.none{color:var(--bone-3);}
.tag{font-family:var(--mono);font-size:10px;letter-spacing:.1em;text-transform:uppercase;
  padding:2.5px 7px;border:1px solid var(--bone-3);border-radius:2px;white-space:nowrap;}
.tag.apply{border-color:var(--brass-graph);color:var(--brass-ink);background:rgba(192,138,46,.12);}
.tag.ag{border-color:var(--rust);color:var(--rust);background:rgba(168,68,43,.09);}
.src{font-family:var(--mono);font-size:11px;color:var(--ink-soft);overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap;display:block;}
.when{font-family:var(--mono);font-size:11.5px;color:var(--ink-soft);}
.chev{color:var(--ink-soft);padding:11px 8px;background:none;border:0;cursor:pointer;}

.detail{border-bottom:1px solid var(--bone-2);background:#E6DFCE;padding:4px 12px 16px 82px;}
.detail .why{font-family:var(--mono);font-size:11.5px;color:var(--ink-soft);margin:0 0 12px;}
.detail .why span{display:inline-block;border:1px solid var(--bone-3);border-radius:2px;
  padding:1px 7px;margin:0 5px 5px 0;}
.acts{display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.btn{font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
  padding:7px 13px;border:1px solid var(--ink);border-radius:2px;color:var(--ink);background:none;
  text-decoration:none;display:inline-flex;align-items:center;gap:6px;cursor:pointer;transition:.13s;}
.btn:hover{background:var(--ink);color:var(--bone);}
.btn.solid{background:var(--steel);color:var(--bone);border-color:var(--steel);}
.btn.solid:hover{background:var(--brass);border-color:var(--brass);color:var(--steel);}
.btn[data-state="saved"]{background:var(--brass);border-color:var(--brass);color:var(--steel);}
.btn[data-state="applied"]{background:var(--ink);color:var(--bone);}
.meta{font-family:var(--mono);font-size:10.5px;color:var(--ink-soft);margin-left:auto;}


/* ---------- launch pad ---------- */
.pad{max-width:1340px;margin:0 auto;padding:16px 22px 0;}
.pad-h{font-family:var(--mono);font-size:10px;letter-spacing:.18em;text-transform:uppercase;
  color:var(--ink-soft);margin:0 0 8px;display:flex;align-items:center;gap:8px;}
.pad-h .rule{flex:1;height:1px;background:var(--bone-3);}
.pad-row{display:flex;gap:7px;flex-wrap:wrap;align-items:center;margin-bottom:11px;}
.pad-kw{flex:0 0 260px;max-width:260px;background:#fff;color:var(--ink);border:1px solid var(--bone-3);
  border-radius:3px;padding:8px 11px;font-size:13.5px;font-family:var(--sans);}
.pad-kw:focus{border-color:var(--brass-graph);outline:none;}
.jump{font-family:var(--mono);font-size:11px;letter-spacing:.05em;text-decoration:none;
  color:var(--ink);background:#fff;border:1px solid var(--bone-3);border-radius:3px;
  padding:7px 12px;transition:.13s;white-space:nowrap;display:inline-flex;align-items:center;gap:6px;}
.jump:hover{border-color:var(--brass);background:rgba(192,138,46,.08);}
.jump.rec{color:var(--brass-ink);border-color:rgba(192,138,46,.4);}
.jump .arr{opacity:.5;font-size:10px;}
.pad-note{font-size:11.5px;color:var(--ink-soft);margin:2px 0 0;max-width:80ch;}

/* ---------- states ---------- */
.empty{text-align:center;padding:80px 20px;color:var(--ink-soft);}
.empty h2{font-family:var(--display);font-weight:600;font-size:30px;letter-spacing:.09em;
  text-transform:uppercase;color:var(--ink);margin:0 0 10px;}
.empty p{max-width:46ch;margin:0 auto 8px;}
.empty code{font-family:var(--mono);font-size:12.5px;background:var(--bone-2);padding:2px 6px;border-radius:2px;}
.note{display:flex;gap:10px;align-items:flex-start;font-size:12.5px;color:var(--ink-soft);
  padding:10px 12px;border-left:2px solid var(--brass-graph);background:rgba(192,138,46,.09);
  max-width:1340px;margin:18px auto 0;}
.note.bad{border-left-color:var(--rust);background:rgba(168,68,43,.09);color:#7E3220;}
.foot{max-width:1340px;margin:0 auto;padding:16px 22px 60px;font-size:12px;color:var(--ink-soft);}
#drop{position:fixed;inset:0;z-index:60;background:rgba(17,24,32,.88);color:var(--bone);
  display:none;place-items:center;font-family:var(--display);font-size:32px;letter-spacing:.1em;
  text-transform:uppercase;pointer-events:none;}
#drop.on{display:grid;}
#file{display:none;}
.mob{display:none;}

@media (max-width:900px){
  .head{display:none;}
  .mob{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px;}
  .row{display:flex;flex-wrap:wrap;align-items:center;padding:7px 0;}
  .cell{padding:2px 8px;}
  .c-fit{flex:0 0 52px;order:1;}
  .c-title{flex:1 1 calc(100% - 98px);order:2;}
  .c-chev{flex:0 0 34px;order:3;margin-left:auto;}
  .c-company{flex:0 0 auto;order:4;padding-left:60px;}
  .c-rate{flex:0 0 auto;order:5;text-align:left;}
  .c-action{flex:0 0 auto;order:6;}
  .c-channel{flex:0 0 auto;order:7;}
  .c-source,.c-posted{display:none;}
  .title{white-space:normal;}
  .co{font-size:12px;color:var(--ink-soft);}
  .detail{padding-left:16px;}
  .mark{font-size:30px;}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important;}}
</style>
</head>
<body>

<header class="band">
  <div class="inner">
    <div class="top">
      <span class="mark">ROTWANG</span>
      <span class="sub">contract ledger</span>
      <span class="tally" id="tally">__META_STAMP__</span>
    </div>

    <div class="controls">
      <button class="ctl primary" id="pick">Open rotwang_roles.csv</button>
      <input type="file" id="file" accept=".csv,.json,text/csv">
      <input class="find" id="find" placeholder="Filter by role, company, method, place…" aria-label="Filter rows">
      <select class="sel" id="fAction" aria-label="Filter by action"><option value="">All actions</option></select>
      <select class="sel" id="fChannel" aria-label="Filter by channel">
        <option value="">Direct + agency</option>
        <option value="DIRECT">Direct only</option>
        <option value="AGENCY">Agency only</option>
      </select>
      <select class="sel" id="fIndustry" aria-label="Filter by industry"><option value="">All industries</option></select>
      <button class="ctl" id="tRemote" aria-pressed="false">Remote</button>
      <button class="ctl" id="tRate" aria-pressed="false">Rate shown</button>
      <button class="ctl" id="tDone" aria-pressed="false">Hide applied</button>
      <span class="sep"></span>
      <label class="lbl" for="minfit">Min fit <span id="minfitv">0</span></label>
      <input type="range" id="minfit" min="0" max="95" step="5" value="0">
      <span class="sep"></span>
      <label class="lbl" for="group">Group</label>
      <select class="sel" id="group">
        <option value="none">Off</option>
        <option value="action">Action</option>
        <option value="band">Fit band</option>
        <option value="industry">Industry</option>
        <option value="company">Company</option>
        <option value="source">Source</option>
      </select>
      <button class="ctl" id="export">Export view</button>
    </div>

    <div class="controls mob">
      <label class="lbl" for="msort">Sort</label>
      <select class="sel" id="msort"></select>
      <button class="ctl" id="mdir">Descending</button>
    </div>

    <div class="head" id="head"></div>
  </div>
</header>


<section class="pad" id="pad">
  <div class="pad-h">Launch pad — sites ROTWANG can't pull, one click as a human <span class="rule"></span></div>
  <div class="pad-row">
    <input class="pad-kw" id="padkw" placeholder="tune these links — oncology, RWE, survival…" aria-label="Launch keyword">
    <a class="jump" id="j_li" target="_blank" rel="noreferrer">LinkedIn <span class="arr">&#8599;</span></a>
    <a class="jump" id="j_in" target="_blank" rel="noreferrer">Indeed <span class="arr">&#8599;</span></a>
    <a class="jump" id="j_gj" target="_blank" rel="noreferrer">Google Jobs <span class="arr">&#8599;</span></a>
    <a class="jump" id="j_di" target="_blank" rel="noreferrer">Dice <span class="arr">&#8599;</span></a>
    <a class="jump" id="j_bs" target="_blank" rel="noreferrer">BioSpace <span class="arr">&#8599;</span></a>
    <a class="jump" id="j_wf" target="_blank" rel="noreferrer">Wellfound <span class="arr">&#8599;</span></a>
  </div>
  <div class="pad-h">Recruiter desks — pharma &amp; biotech contract headhunters <span class="rule"></span></div>
  <div class="pad-row" id="recrow"></div>
  <p class="pad-note">These open a live, pre-filtered search on each site in a new tab. ROTWANG never
  logs into them or scrapes them — that would risk your own accounts. The results table below is the
  part it pulls directly from company and recruiter boards.</p>
</section>

<div id="msg"></div>
<main class="sheet" id="sheet"></main>
<footer class="foot" id="foot"></footer>
<div id="drop">Drop rotwang_roles.csv</div>

<script>
window.__ROTWANG__ = __ROTWANG_DATA__;
</script>
<script>
"use strict";

/* ==========================================================================
   ROTWANG ledger — standalone. Reads the CSV rotwang.py writes.
   Columns expected: fit, action, title, company, pay, location, remote,
   industry, posted, source, why, url. Missing ones are simply skipped.
   ========================================================================== */

/* ---------- storage: works from file:// where it can, memory where it can't */
var store = (function () {
  var mem = {}, ok = false;
  try { localStorage.setItem("__probe", "1"); localStorage.removeItem("__probe"); ok = true; } catch (e) { ok = false; }
  return {
    get: function (k, d) {
      try { if (ok) { var r = localStorage.getItem(k); return r ? JSON.parse(r) : d; } } catch (e) {}
      return k in mem ? mem[k] : d;
    },
    set: function (k, v) {
      mem[k] = v;
      try { if (ok) localStorage.setItem(k, JSON.stringify(v)); } catch (e) {}
    }
  };
})();

/* ---------- CSV: quotes, escaped quotes, commas and newlines inside fields */
function parseCsv(text) {
  text = text.replace(/^\uFEFF/, "");
  var rows = [], row = [], val = "", q = false, i = 0, c, n;
  for (; i < text.length; i++) {
    c = text[i];
    if (q) {
      if (c === '"') {
        if (text[i + 1] === '"') { val += '"'; i++; }
        else q = false;
      } else val += c;
    } else if (c === '"') q = true;
    else if (c === ",") { row.push(val); val = ""; }
    else if (c === "\n" || c === "\r") {
      if (c === "\r" && text[i + 1] === "\n") i++;
      row.push(val); val = "";
      if (row.length > 1 || row[0] !== "") rows.push(row);
      row = [];
    } else val += c;
  }
  if (val !== "" || row.length) { row.push(val); rows.push(row); }
  if (!rows.length) throw new Error("That file has no rows in it.");
  var head = rows.shift().map(function (h) { return h.trim().toLowerCase(); });
  return rows.filter(function (r) { return r.length > 1; }).map(function (r) {
    var o = {};
    for (n = 0; n < head.length; n++) o[head[n]] = (r[n] || "").trim();
    return o;
  });
}

/* Mojibake repair. The CSV is UTF-8; if it was ever written or re-saved as
   Windows-1252 the middle dot in "Company · Greenhouse" arrives as "Â·". */
function fixText(s) {
  if (!s || !/Ã|Â|â€/.test(s)) return s;
  try { return decodeURIComponent(escape(s)); } catch (e) { return s; }
}

/* ---------- money: ranges sort on their midpoint, not their ceiling ------- */
var RANGE = /(\d+(?:\.\d+)?)\s*k?\s*(?:[-–—]|to)\s*[$£€]?\s*(\d+(?:\.\d+)?)\s*k?/;
function parseRate(raw) {
  var s = String(raw || "").toLowerCase().replace(/,/g, "");
  if (!s) return null;
  var m = s.match(RANGE), v;
  if (m) v = (Number(m[1]) + Number(m[2])) / 2;
  else { var f = s.match(/\d+(?:\.\d+)?/); if (!f) return null; v = Number(f[0]); }
  if (!(v > 0)) return null;
  if (/\d\s*k\b/.test(s)) v *= 1000;
  if (/\/\s*day|\/\s*d\b|per day|\bdaily\b|day rate|\bp\/d\b/.test(s))
    return { hourly: Math.round(v / 8), annual: Math.round(v * 230) };
  if (/\/\s*h|per hour|hourly|\bhr\b|an hour|\bp\/h\b/.test(s) || v <= 900)
    return { hourly: Math.round(v), annual: Math.round(v * 2080) };
  return { hourly: Math.round(v / 2080), annual: Math.round(v) };
}

var isUrl = function (u) { return /^https?:\/\/\S+\.\S+/i.test(u || ""); };

function normalise(r, i) {
  var url = r.url || r.link || "";
  var title = fixText(r.title || r.role || "");
  if (!title && !url) return null;
  var j = {
    id: isUrl(url) ? url.split("?")[0] : "row-" + i + "-" + title.slice(0, 40),
    fit: Math.round(Number(r.fit) || 0),
    action: (fixText(r.action) || "—").toUpperCase(),
    channel: (fixText(r.channel) || "").toUpperCase(),
    title: title || "Untitled role",
    company: fixText(r.company) || "—",
    rate: fixText(r.pay || r.rate || r.salary || ""),
    location: fixText(r.location || ""),
    industry: fixText(r.industry || ""),
    source: fixText(r.source || "—"),
    why: fixText(r.why || ""),
    url: url,
    postedRaw: r.posted || ""
  };
  j.rateVal = parseRate(j.rate);
  var t = Date.parse(j.postedRaw);
  j.posted = isNaN(t) ? null : t;
  var rem = (r.remote || "").toLowerCase();
  j.remote = rem === "yes" || rem === "true" || rem === "1" || rem === "remote" ||
    /remote|anywhere/i.test(j.location);
  return j;
}

function load(text, label) {
  var rows;
  try {
    var t = text.replace(/^\uFEFF/, "").trim();
    rows = (t[0] === "[" || t[0] === "{")
      ? (function () { var d = JSON.parse(t); return Array.isArray(d) ? d : (d.jobs || d.results || d.rows || []); })()
      : parseCsv(t);
    var out = [], seen = {}, k;
    for (var i = 0; i < rows.length; i++) {
      var j = normalise(rows[i], i);
      if (!j) continue;
      k = isUrl(j.url) ? j.url.split("?")[0] : (j.company + "|" + j.title).toLowerCase();
      if (seen[k]) continue;
      seen[k] = 1; out.push(j);
    }
    if (!out.length) throw new Error("Read the file, but found no rows with a title or a link. Is this the right CSV?");
    S.jobs = out; S.open = null; S.collapsed = {};
    buildFilters();
    say("Loaded " + out.length + " roles" + (label ? " from " + label : "") + ".", false);
    render();
  } catch (e) {
    say(e.message, true);
  }
}

function say(text, bad) {
  document.getElementById("msg").innerHTML =
    '<div class="note' + (bad ? " bad" : "") + '">' + esc(text) + "</div>";
}
function esc(s) {
  return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
    .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* ---------- columns ---------- */
var COLS = [
  { k: "fit", label: "Fit", align: "c", w: "70px" },
  { k: "title", label: "Role", align: "l", w: "minmax(0,1fr)" },
  { k: "company", label: "Company", align: "l", w: "minmax(0,200px)" },
  { k: "rate", label: "Rate", align: "r", w: "160px" },
  { k: "action", label: "Action", align: "l", w: "96px" },
  { k: "channel", label: "Channel", align: "l", w: "104px" },
  { k: "source", label: "Source", align: "l", w: "140px" },
  { k: "posted", label: "Posted", align: "r", w: "94px" }
];
var GRID = COLS.map(function (c) { return c.w; }).join(" ") + " 34px";

function valueOf(j, k) {
  if (k === "fit") return j.fit;
  if (k === "rate") return j.rateVal ? j.rateVal.annual : null;
  if (k === "posted") return j.posted;
  return (j[k] || "").toLowerCase();
}
function cmp(k, dir) {
  var sign = dir === "asc" ? 1 : -1;
  return function (a, b) {
    var x = valueOf(a, k), y = valueOf(b, k);
    var xn = x === null || x === undefined || x === "";
    var yn = y === null || y === undefined || y === "";
    if (xn && yn) return b.fit - a.fit;
    if (xn) return 1;                 /* blanks always last, both directions */
    if (yn) return -1;
    var d = typeof x === "string" ? x.localeCompare(y) : x - y;
    if (d === 0) return b.fit - a.fit || a.company.localeCompare(b.company);
    return d * sign;
  };
}
function ago(ts) {
  if (!ts) return "—";
  var d = Math.floor((Date.now() - ts) / 86400000);
  if (d <= 0) return "today";
  if (d === 1) return "1d";
  if (d < 60) return d + "d";
  return Math.round(d / 30) + "mo";
}
function dial(v) {
  var size = 34, r = size / 2 - 3.5, c = size / 2, pct = Math.max(0, Math.min(100, v || 0)) / 100;
  function pt(deg) { var a = deg * Math.PI / 180; return [(c + r * Math.cos(a)).toFixed(2), (c + r * Math.sin(a)).toFixed(2)]; }
  function arc(f, t) { var p = pt(f), q = pt(t); return "M " + p[0] + " " + p[1] + " A " + r + " " + r + " 0 " + (t - f > 180 ? 1 : 0) + " 1 " + q[0] + " " + q[1]; }
  return '<svg width="' + size + '" height="' + size + '" viewBox="0 0 ' + size + ' ' + size + '" aria-hidden="true">' +
    '<path class="dial-track" d="' + arc(135, 405) + '"/>' +
    (pct > 0.01 ? '<path class="dial-arc" d="' + arc(135, 135 + 270 * pct) + '"/>' : "") + "</svg>";
}

/* ---------- state ---------- */
var EMBEDDED = (window.__ROTWANG__ && window.__ROTWANG__.roles) || null;
var SEED_KW = (window.__ROTWANG__ && window.__ROTWANG__.keyword) || "contract biostatistician";
var S = {
  jobs: [], sortKey: "fit", sortDir: "desc", group: "none", find: "",
  action: "", channel: "", industry: "", remote: false, rateOnly: false, hideDone: false,
  minFit: 0, open: null, collapsed: {}, status: store.get("rotwang_status", {})
};

function buildFilters() {
  ["action", "industry"].forEach(function (field) {
    var sel = document.getElementById(field === "action" ? "fAction" : "fIndustry");
    var vals = {}, list = [];
    S.jobs.forEach(function (j) { if (j[field] && j[field] !== "—") vals[j[field]] = 1; });
    for (var v in vals) list.push(v);
    list.sort();
    var all = field === "action" ? "All actions" : "All industries";
    sel.innerHTML = '<option value="">' + all + "</option>" +
      list.map(function (v) { return '<option value="' + esc(v) + '">' + esc(v) + "</option>"; }).join("");
  });
}

function visible() {
  var q = S.find.trim().toLowerCase();
  return S.jobs.filter(function (j) {
    if (j.fit < S.minFit) return false;
    if (S.action && j.action !== S.action) return false;
    if (S.channel && j.channel !== S.channel) return false;
    if (S.industry && j.industry !== S.industry) return false;
    if (S.remote && !j.remote) return false;
    if (S.rateOnly && !j.rateVal) return false;
    if (S.hideDone && S.status[j.id] === "applied") return false;
    if (q && (j.title + " " + j.company + " " + j.location + " " + j.source + " " + j.why)
      .toLowerCase().indexOf(q) === -1) return false;
    return true;
  }).sort(cmp(S.sortKey, S.sortDir));
}

function groupRows(rows) {
  if (S.group === "none") return [{ label: null, rows: rows }];
  var map = {}, order = [];
  rows.forEach(function (j) {
    var k = S.group === "band"
      ? (j.fit >= 78 ? "Act on these" : j.fit >= 60 ? "Worth a look" : "Lower fit")
      : (j[S.group] || "—");
    if (!map[k]) { map[k] = []; order.push(k); }
    map[k].push(j);
  });
  if (S.group === "band") {
    var rank = { "Act on these": 0, "Worth a look": 1, "Lower fit": 2 };
    order.sort(function (a, b) { return rank[a] - rank[b]; });
  } else {
    order.sort(function (a, b) { return map[b].length - map[a].length || a.localeCompare(b); });
  }
  return order.map(function (k) { return { label: k, rows: map[k] }; });
}

/* ---------- render ---------- */
function renderHead() {
  var h = document.getElementById("head");
  h.style.gridTemplateColumns = GRID;
  h.innerHTML = COLS.map(function (c) {
    var on = S.sortKey === c.k;
    var caret = on ? '<svg class="caret" width="9" height="9" viewBox="0 0 9 9"><path d="' +
      (S.sortDir === "asc" ? "M4.5 1.5 L8 7 L1 7 Z" : "M4.5 7.5 L1 2 L8 2 Z") + '" fill="currentColor"/></svg>' : "";
    return '<button class="h ' + (c.align === "r" ? "r " : c.align === "c" ? "c " : "") + (on ? "on" : "") +
      '" data-sort="' + c.k + '" aria-sort="' + (on ? (S.sortDir === "asc" ? "ascending" : "descending") : "none") +
      '" title="Sort by ' + c.label.toLowerCase() + '">' + c.label + caret + "</button>";
  }).join("") + "<span></span>";
}

function render() {
  renderHead();
  var rows = visible(), sheet = document.getElementById("sheet");

  document.getElementById("tally").innerHTML = S.jobs.length
    ? "<b>" + rows.length + "</b> shown · <b>" +
      rows.filter(function (j) { return j.channel !== "AGENCY"; }).length +
      "</b> direct · " + S.jobs.length + " loaded"
    : "no file loaded";

  if (!S.jobs.length) {
    sheet.innerHTML = '<div class="empty"><h2>Open your results</h2>' +
      "<p>This run returned no roles. Widen the search, or drag a <code>rotwang_roles.csv</code> " +
      "onto the window to view an earlier run.</p></div>";
    document.getElementById("foot").textContent = "";
    return;
  }
  if (!rows.length) {
    sheet.innerHTML = '<div class="empty"><h2>Nothing matches</h2>' +
      "<p>Drop the min-fit slider back to 0, or clear a filter.</p></div>";
    return;
  }

  var html = "";
  groupRows(rows).forEach(function (g, gi) {
    if (g.label) {
      var shut = S.collapsed[g.label];
      html += '<button class="grp" data-group="' + esc(g.label) + '" aria-expanded="' + (!shut) + '">' +
        chev(!shut) + " <b>" + esc(g.label) + "</b> " + g.rows.length + '<span class="rule"></span></button>';
      if (shut) return;
    }
    g.rows.forEach(function (j) {
      var open = S.open === j.id, st = S.status[j.id] || "";
      html += '<div class="row' + (open ? " open" : "") + (st === "applied" ? " done" : "") +
        '" data-id="' + esc(j.id) + '" style="grid-template-columns:' + GRID + '">' +
        '<div class="cell c c-fit"><span class="fitwrap">' + dial(j.fit) + "<b>" + j.fit + "</b></span></div>" +
        '<div class="cell c-title"><div class="title" title="' + esc(j.title) + '">' + esc(j.title) + "</div>" +
          (j.location ? '<div class="loc">' + esc(j.location) + "</div>" : "") + "</div>" +
        '<div class="cell c-company"><span class="co" title="' + esc(j.company) + '">' + esc(j.company) + "</span></div>" +
        '<div class="cell r c-rate"><span class="rate' + (j.rate ? "" : " none") + '">' + esc(j.rate || "—") + "</span></div>" +
        '<div class="cell c-action"><span class="tag' + (j.action === "APPLY" ? " apply" : "") + '">' + esc(j.action) + "</span></div>" +
        '<div class="cell c-channel">' + (j.channel ? '<span class="tag' + (j.channel === "AGENCY" ? " ag" : "") + '">' + esc(j.channel) + "</span>" : "") + "</div>" +
        '<div class="cell c-source"><span class="src" title="' + esc(j.source) + '">' + esc(j.source) + "</span></div>" +
        '<div class="cell r c-posted"><span class="when">' + ago(j.posted) + "</span></div>" +
        '<button class="chev c-chev" aria-expanded="' + open + '" aria-label="Details">' + chev(open) + "</button>" +
        "</div>";
      if (open) {
        html += '<div class="detail">' +
          (j.why ? '<div class="why">' + j.why.split(/;\s*/).filter(Boolean).map(function (w) {
            return "<span>" + esc(w) + "</span>"; }).join("") + "</div>" : "") +
          '<div class="acts">' +
          (isUrl(j.url) ? '<a class="btn solid" href="' + esc(j.url) + '" target="_blank" rel="noreferrer">Open posting</a>' : "") +
          '<button class="btn mark" data-mark="' + esc(j.id) + '" data-state="' + st + '">' +
            (st === "applied" ? "Applied" : st === "saved" ? "Saved" : "Save") + "</button>" +
          '<button class="btn" data-copy="' + esc(j.id) + '">Copy row</button>' +
          '<span class="meta">' + (j.rateVal ? "≈ $" + j.rateVal.hourly + "/hr · $" +
            Math.round(j.rateVal.annual / 1000) + "k yr · " : "") +
            (j.industry ? esc(j.industry) + " · " : "") + "posted " + esc(j.postedRaw || "unknown") + "</span>" +
          "</div></div>";
      }
    });
  });
  sheet.innerHTML = html;

  var lbl = COLS.filter(function (c) { return c.k === S.sortKey; })[0].label.toLowerCase();
  document.getElementById("foot").textContent =
    "Sorted by " + lbl + ", " + (S.sortDir === "asc" ? "ascending" : "descending") + "." +
    (S.group !== "none" ? " Grouping sits on top; the sort still holds inside each group." : "") +
    " Rates compare on an annualised basis, so hourly and salaried rows sort against each other." +
    " Rows with no rate listed always sort last." +
    " Saved and applied marks persist on this computer.";
}
function chev(open) {
  return '<svg width="11" height="11" viewBox="0 0 11 11" style="transform:rotate(' + (open ? 90 : 0) +
    'deg);transition:transform .16s"><path d="M3.5 1 L8 5.5 L3.5 10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
}

/* ---------- events ---------- */
function sortBy(k) {
  if (S.sortKey === k) S.sortDir = S.sortDir === "asc" ? "desc" : "asc";
  else { S.sortKey = k; S.sortDir = (k === "fit" || k === "rate" || k === "posted") ? "desc" : "asc"; }
  document.getElementById("msort").value = k;
  document.getElementById("mdir").textContent = S.sortDir === "asc" ? "Ascending" : "Descending";
  render();
}

document.addEventListener("click", function (e) {
  var t = e.target.closest ? e.target.closest("[data-sort],[data-group],[data-mark],[data-copy],.chev,.row,a") : null;
  if (!t) return;
  if (t.tagName === "A") return;
  if (t.dataset.sort) { sortBy(t.dataset.sort); return; }
  if (t.dataset.group) { S.collapsed[t.dataset.group] = !S.collapsed[t.dataset.group]; render(); return; }
  if (t.dataset.mark) {
    var id = t.dataset.mark, cur = S.status[id];
    if (!cur) S.status[id] = "saved";
    else if (cur === "saved") S.status[id] = "applied";
    else delete S.status[id];
    store.set("rotwang_status", S.status); render(); return;
  }
  if (t.dataset.copy) {
    var j = S.jobs.filter(function (x) { return x.id === t.dataset.copy; })[0];
    if (j) { try { navigator.clipboard.writeText(j.title + " — " + j.company + (j.rate ? " — " + j.rate : "") + "\n" + j.url); } catch (err) {} }
    return;
  }
  var row = e.target.closest(".row");
  if (row) { S.open = S.open === row.dataset.id ? null : row.dataset.id; render(); }
});

document.getElementById("pick").onclick = function () { document.getElementById("file").click(); };
document.getElementById("file").onchange = function (e) {
  var f = e.target.files[0]; if (!f) return;
  var r = new FileReader();
  r.onload = function () { load(String(r.result), f.name); };
  r.onerror = function () { say("Could not read that file.", true); };
  r.readAsText(f);
};
document.getElementById("find").oninput = function (e) { S.find = e.target.value; render(); };
document.getElementById("fAction").onchange = function (e) { S.action = e.target.value; render(); };
document.getElementById("fChannel").onchange = function (e) { S.channel = e.target.value; render(); };
document.getElementById("fIndustry").onchange = function (e) { S.industry = e.target.value; render(); };
document.getElementById("group").onchange = function (e) { S.group = e.target.value; render(); };
document.getElementById("minfit").oninput = function (e) {
  S.minFit = Number(e.target.value);
  document.getElementById("minfitv").textContent = S.minFit; render();
};
["tRemote:remote", "tRate:rateOnly", "tDone:hideDone"].forEach(function (pair) {
  var p = pair.split(":"), el = document.getElementById(p[0]);
  el.onclick = function () { S[p[1]] = !S[p[1]]; el.setAttribute("aria-pressed", S[p[1]]); render(); };
});
document.getElementById("mdir").onclick = function () {
  S.sortDir = S.sortDir === "asc" ? "desc" : "asc";
  this.textContent = S.sortDir === "asc" ? "Ascending" : "Descending"; render();
};
document.getElementById("msort").innerHTML = COLS.map(function (c) {
  return '<option value="' + c.k + '">' + c.label + "</option>";
}).join("");
document.getElementById("msort").onchange = function (e) { S.sortKey = e.target.value; render(); };

document.getElementById("export").onclick = function () {
  var rows = visible();
  var head = ["fit", "action", "title", "company", "pay", "location", "industry", "posted", "source", "status", "url"];
  var q = function (v) { return '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"'; };
  var csv = head.join(",") + "\n" + rows.map(function (j) {
    return [j.fit, j.action, j.title, j.company, j.rate, j.location, j.industry,
      j.postedRaw, j.source, S.status[j.id] || "", j.url].map(q).join(",");
  }).join("\n");
  try {
    var b = new Blob(["\uFEFF" + csv], { type: "text/csv;charset=utf-8" });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(b); a.download = "rotwang_view.csv";
    document.body.appendChild(a); a.click(); a.remove();
  } catch (err) { say("Could not save the file. Copy the rows instead.", true); }
};

/* drag and drop anywhere */
var depth = 0, dropEl = document.getElementById("drop");
window.addEventListener("dragover", function (e) { e.preventDefault(); });
window.addEventListener("dragenter", function (e) { e.preventDefault(); depth++; dropEl.classList.add("on"); });
window.addEventListener("dragleave", function () { depth = Math.max(0, depth - 1); if (!depth) dropEl.classList.remove("on"); });
window.addEventListener("drop", function (e) {
  e.preventDefault(); depth = 0; dropEl.classList.remove("on");
  var f = e.dataTransfer && e.dataTransfer.files[0]; if (!f) return;
  var r = new FileReader();
  r.onload = function () { load(String(r.result), f.name); };
  r.onerror = function () { say("Could not read that file.", true); };
  r.readAsText(f);
});


/* ---------- launch pad ---------- */
var RECRUITERS = ["Penfield Search Partners","Planet Pharma","Sci.bio","GForce Life Sciences",
  "EPM Scientific","Barrington James","Hobson Prior","ProClinical","Klein Hersh","Medix",
  "i-Pharm Consulting","RBW Consulting"];
function padLinks() {
  var kw = (document.getElementById("padkw").value.trim() || SEED_KW);
  var q = encodeURIComponent(kw);
  var qc = encodeURIComponent(kw + " contract");
  var set = function (id, url) { document.getElementById(id).href = url; };
  set("j_li", "https://www.linkedin.com/jobs/search/?keywords=" + q + "&f_JT=C%2CT&f_WT=2");
  set("j_in", "https://www.indeed.com/jobs?q=" + qc + "&sc=0kf%3Aattr(PAXZC)%3B");
  set("j_gj", "https://www.google.com/search?ibp=htl;jobs&q=" + qc);
  set("j_di", "https://www.dice.com/jobs?q=" + q + "&filters.employmentType=CONTRACTS&filters.workplaceTypes=Remote");
  set("j_bs", "https://www.biospace.com/jobs?query=" + q);
  set("j_wf", "https://wellfound.com/role/r/biostatistician");
}
(function () {
  var row = document.getElementById("recrow");
  row.innerHTML = RECRUITERS.map(function (name) {
    return '<a class="jump rec" target="_blank" rel="noreferrer" data-rec="' + esc(name) +
      '">' + esc(name) + ' <span class="arr">&#8599;</span></a>';
  }).join("");
  function recLinks() {
    var kw = (document.getElementById("padkw").value.trim() || SEED_KW);
    [].forEach.call(row.querySelectorAll("[data-rec]"), function (a) {
      a.href = "https://www.google.com/search?q=" +
        encodeURIComponent('"' + a.dataset.rec + '" ' + kw + " jobs");
    });
  }
  document.getElementById("padkw").addEventListener("input", function () { padLinks(); recLinks(); });
  padLinks(); recLinks();
})();

/* ---------- auto-load embedded results ---------- */
if (EMBEDDED && EMBEDDED.length) {
  try {
    load(JSON.stringify(EMBEDDED), null);
    document.getElementById("msg").innerHTML = "";
  } catch (e) { say("Embedded data failed to parse: " + e.message, true); }
}

render();
</script>
</body>
</html>
'''


def write_html(roles: list[Role], prof_label: str = "") -> None:
    """Bake the results into a self-contained interactive report: sortable,
    filterable, DIRECT/AGENCY channel, and a launch pad into the sites ROTWANG
    can't pull (LinkedIn, Indeed, Dice, BioSpace) plus the named recruiters."""
    data = [{
        "fit": r.fit,
        "action": "APPLY" if r.contract else "PITCH",
        "channel": r.channel,
        "title": r.title,
        "company": r.company,
        "pay": r.pay,
        "location": r.location,
        "remote": "yes" if r.remote else "",
        "industry": r.industry,
        "posted": r.posted,
        "source": r.source,
        "why": "; ".join(r.hits),
        "url": r.url,
    } for r in roles]

    # Seed the launch-pad keyword from the active profile so the first click is
    # already on-target even before the user tunes it.
    seed = (prof_label or "contract biostatistician").lower()
    if "biostat" not in seed and "data" not in seed:
        seed = "contract " + seed
    payload = json.dumps({"roles": data, "keyword": seed}, ensure_ascii=False)
    stamp = f"generated {datetime.now(timezone.utc):%d %b %Y · %H:%M UTC} · {len(roles)} roles"

    out = (REPORT_TEMPLATE
           .replace("__ROTWANG_DATA__", payload)
           .replace("__META_LABEL__", html.escape(prof_label or "Contract Ledger"))
           .replace("__META_STAMP__", html.escape(stamp)))
    HTML_OUT.write_text(out, encoding="utf-8")
    print(f"  {HTML_OUT.name}   open this — the full interface, data baked in")


def digest(roles: list[Role]) -> str:
    L = [f"{len(roles)} direct-employer role(s) — {datetime.now(timezone.utc):%Y-%m-%d %H:%M UTC}"]
    for label, block in [("CONTRACT — APPLY", [r for r in roles if r.contract]),
                         ("LEADS — PITCH", [r for r in roles if not r.contract])]:
        if not block:
            continue
        L.append(f"\n{label}  ({len(block)})")
        for r in block[:40]:
            pay = f"  {r.pay}" if r.pay else ""
            L.append(f"  [{r.fit:2}] {r.company}: {r.title} ({r.location or 'n/a'}){pay}"
                     f"\n       {r.industry} · {r.source}\n       {r.url}")
    return "\n".join(L)


def send_email(roles: list[Role]) -> None:
    if not (EMAIL_TO and SMTP_USER and SMTP_PASS):
        print("  Email off — set ROTWANG_EMAIL_TO / ROTWANG_SMTP_USER / ROTWANG_SMTP_PASS.")
        return
    m = EmailMessage()
    m["Subject"] = f"[ROTWANG] {len(roles)} role(s), {sum(r.contract for r in roles)} contract"
    m["From"], m["To"] = SMTP_USER, EMAIL_TO
    m.set_content(digest(roles))
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
        srv.starttls(); srv.login(SMTP_USER, SMTP_PASS); srv.send_message(m)
    print(f"  Emailed to {EMAIL_TO}")


# ──────────────────────────────── MAIN ────────────────────────────────────────

def log_application(role_url: str, title: str, company: str) -> None:
    """The app writes here too, so both halves share one record of what has
    been sent and when."""
    fresh = not APPS_CSV.exists()
    with APPS_CSV.open("a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if fresh:
            w.writerow(["date", "company", "title", "url", "status"])
        w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%d"), company, title,
                    role_url, "applied"])


def main() -> int:
    p = argparse.ArgumentParser(description="Find direct contract work and consulting leads.")
    p.add_argument("--profile", default=DEFAULT_PROFILE, help="discipline to search for")
    p.add_argument("--list-profiles", action="store_true", help="show the built-in disciplines")
    p.add_argument("--depth", choices=["quick", "normal", "deep"], default="normal")
    p.add_argument("--leads", action="store_true", help="include permanent roles to pitch")
    p.add_argument("--no-agency", action="store_true",
                   help="drop recruiter and CRO listings instead of tagging them")
    p.add_argument("--countries", default="us", help="comma list, e.g. us,gb,ca")
    p.add_argument("--days", type=int, default=0, help="only postings newer than N days")
    p.add_argument("--pages", type=int, default=1, help="index pages per query, 50 each")
    p.add_argument("--min-fit", type=int, default=0)
    p.add_argument("--new-only", action="store_true")
    p.add_argument("--no-index", action="store_true", help="skip the keyed index")
    p.add_argument("--fresh", action="store_true", help="ignore the 30-minute cache")
    p.add_argument("--setup", action="store_true", help="re-enter the Adzuna key")
    p.add_argument("--discover", nargs="*", help="resolve board addresses for companies")
    p.add_argument("--grow", type=int, default=None,
                   help=f"names to resolve this run (default {GROW_PER_RUN}; use 0 or --no-grow to skip)")
    p.add_argument("--no-grow", action="store_true", help="do not extend the board list")
    p.add_argument("--boards", action="store_true", help="show board coverage and exit")
    a = p.parse_args()

    if a.list_profiles:
        print("\nBuilt-in disciplines:\n")
        for k, v in PROFILES.items():
            print(f"  {k:14} {v['label']}")
        print("\n  rotwang_profile.json — written by the app from your own CV —")
        print("  overrides all of these. That is the one worth using.\n")
        return 0
    if a.setup:
        adzuna_keys(force=True); return 0
    if a.boards:
        st = load_store()
        live = all_boards(st)
        tg = load_targets()
        known = {b["name"] for b in st["resolved"]} | {n for _, n, _ in BOARDS}
        by = {}
        for ats, _, _ in live:
            by[ats] = by.get(ats, 0) + 1
        print(f"\n  {len(live)} boards live" +
              (f"  ({', '.join(f'{v} {k}' for k, v in sorted(by.items()))})" if by else ""))
        print(f"  {len([b for b in st['resolved'] if b.get('retired')])} retired")
        print(f"  {len(tg)} target names loaded from {TARGETS_FILE.name}")
        print(f"  {len([n for n in tg if n not in known])} still unresolved")
        print(f"  {len(st['missed'])} ruled out (re-probed after {RETRY_MISSED_DAYS} days)")
        rem = len([n for n in tg if n not in known])
        if rem:
            print(f"\n  At {GROW_PER_RUN} per run that is ~{-(-rem // max(1, GROW_PER_RUN))} more runs,")
            print(f"  or resolve them now: python rotwang.py --grow {rem} --no-index\n")
        else:
            print("\n  Every target name has been resolved or ruled out.\n")
        return 0

    if a.discover is not None:
        names = a.discover
        f = HERE / "discover.txt"
        if not names and f.exists():
            names = [l.strip() for l in f.read_text().splitlines() if l.strip()]
        if not names:
            names = [n for n in load_targets()
                     if n not in {b["name"] for b in load_store()["resolved"]}]
            print(f"No names given — falling back to the {len(names)} unresolved "
                  f"targets in {TARGETS_FILE.name}")
        if not names:
            print('Usage: python rotwang.py --discover "Tempus" "GRAIL"')
            print('   or: one company name per line in discover.txt, then --discover')
            return 1
        return discover(names)

    if a.profile not in PROFILES and not (HERE / "rotwang_profile.json").exists():
        print(f"Unknown profile '{a.profile}'. Run --list-profiles to see them.")
        return 1

    prof = active_profile(a.profile)
    queries = build_queries(prof, a.depth)
    countries = [c.strip().lower() for c in a.countries.split(",") if c.strip()]
    app_id, app_key = ("", "")
    if not a.no_index:
        app_id, app_key = adzuna_keys()

    t0 = time.time()
    store = load_store()
    if not a.no_grow:
        grow_boards(store, a.grow if a.grow is not None else GROW_PER_RUN)
    boards = all_boards(store)

    print(f"\nROTWANG · {prof['label']} · {a.depth} · {len(queries)} queries · "
          f"{len(boards)} boards · {', '.join(c.upper() for c in countries)} · "
          f"{'contract + leads' if a.leads else 'contract only'}\n")

    s = session()
    raw, failed_boards = [], []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(task_board, s, b, a.fresh): "board" for b in boards}
        futs[ex.submit(task_remotive, s, a.fresh)] = "free"
        for pg in (1, 2, 3):
            futs[ex.submit(task_remotejobs, s, pg, a.fresh)] = "free"
        if app_id:
            for c in countries:
                for q in queries:
                    for pg in range(1, a.pages + 1):
                        futs[ex.submit(task_index, s, app_id, app_key, c, q, pg,
                                       a.days, not a.leads, a.fresh)] = "index"
        total, done = len(futs), 0
        for fut in as_completed(futs):
            label, roles, ok = fut.result()
            done += 1
            if not ok and label.startswith("board:"):
                failed_boards.append(label[6:])
            # The discipline gate now runs on EVERY source — boards included.
            # Board fetchers use wanted() (a title exclude list) which does not
            # check discipline, so company boards were leaking "Solutions
            # Consultant", "Recruiting Coordinator", "Business Consultant", etc.
            roles = [r for r in roles if gate(r, prof)]
            raw.extend(roles)
            if done % 20 == 0 or done == total:
                print(f"  {done}/{total} sources", end="\r", flush=True)
    print(" " * 30, end="\r")

    print(f"  {len(boards) - len(failed_boards)} of {len(boards)} boards answered")
    # A board that fails BOARD_STRIKES runs in a row is retired, so a list of a
    # thousand does not spend most of its time on addresses that no longer exist.
    bad = set(failed_boards)
    retired = 0
    for b in store.get("resolved", []):
        if b.get("retired"):
            continue
        if b["name"] in bad:
            b["fails"] = b.get("fails", 0) + 1
            if b["fails"] >= BOARD_STRIKES:
                b["retired"] = True; retired += 1
        elif b.get("fails"):
            b["fails"] = 0
    save_store(store)
    if failed_boards:
        (HERE / "dead_boards.txt").write_text("\n".join(sorted(failed_boards)))
        print(f"  {len(failed_boards)} unreachable"
              + (f", {retired} retired after {BOARD_STRIKES} strikes" if retired else ""))

    seen, dseen, uniq = set(), set(), []
    for r in raw:
        if not r.url or r.key in seen or r.dkey in dseen:
            continue
        seen.add(r.key); dseen.add(r.dkey); uniq.append(finish(r, prof))

    # Agencies are TAGGED, not deleted. A recruiter advertising a contract in your
    # therapeutic area is proof a sponsor needs one right now, whether or not you
    # go through the recruiter. --no-agency drops them if you want the old behaviour.
    kept = [r for r in uniq if not (a.no_agency and r.agency)]
    agencies = sum(r.agency for r in kept)
    if not a.leads:
        kept = [r for r in kept if r.contract]
    if a.min_fit:
        kept = [r for r in kept if r.fit >= a.min_fit]
    kept.sort(key=lambda r: (not r.contract, r.agency, -r.fit, -pay_value(r.pay)))

    print(f"  {len(raw)} raw · {len(uniq)} unique · {len(kept)} kept "
          f"({len(kept) - agencies} direct, {agencies} agency) · {time.time() - t0:.1f}s\n")

    prev = set(json.loads(STATE.read_text())) if STATE.exists() else set()
    STATE.write_text(json.dumps(sorted(prev | {r.key for r in kept})))
    if a.new_only:
        kept = [r for r in kept if r.key not in prev]
        print(f"  {len(kept)} new since last run\n")

    if not kept:
        print("  Nothing matched. Try --leads, --depth deep, or a lower --min-fit.\n")
        return 0

    write_csv(kept)
    write_html(kept, prof["label"])

    print(digest(kept)[:4000])
    print()
    send_email(kept)
    return 0


if __name__ == "__main__":
    sys.exit(main())

# ══════════════════════════════════════════════════════════════════════════════
# WEEKLY
#   Windows Task Scheduler — Program: py
#     Args: "C:\\Users\\YOU\\rotwang\\rotwang.py" --new-only --days 14 --leads
#     Start in: C:\\Users\\YOU\\rotwang
#
#   GitHub Actions — .github/workflows/rotwang.yml
#     name: weekly
#     on: { schedule: [{ cron: "0 13 * * 1" }], workflow_dispatch: {} }
#     jobs:
#       run:
#         runs-on: ubuntu-latest
#         steps:
#           - uses: actions/checkout@v4
#           - uses: actions/setup-python@v5
#             with: { python-version: "3.12" }
#           - run: pip install requests
#           - run: python rotwang.py --new-only --days 14 --leads
#             env:
#               ADZUNA_APP_ID:     ${{ secrets.ADZUNA_APP_ID }}
#               ADZUNA_APP_KEY:    ${{ secrets.ADZUNA_APP_KEY }}
#               ROTWANG_EMAIL_TO:  ${{ secrets.ROTWANG_EMAIL_TO }}
#               ROTWANG_SMTP_USER: ${{ secrets.ROTWANG_SMTP_USER }}
#               ROTWANG_SMTP_PASS: ${{ secrets.ROTWANG_SMTP_PASS }}
# ══════════════════════════════════════════════════════════════════════════════
