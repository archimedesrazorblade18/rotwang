#!/usr/bin/env python3
"""
radar.py — ROTWANG RADAR
========================
Start from the companies, not the job boards.

  1. UNIVERSE   Build a list of biotech & pharma companies from public sources:
                SEC EDGAR (every listed drug / biologics / R&D filer) and
                ClinicalTrials.gov (every company sponsoring an active US trial —
                the ones that need a statistician right now), plus the curated
                names already in rotwang_targets.json.
  2. RESOLVE    Find where each company posts jobs by probing six applicant-
                tracking systems. Workday careers sites are added by URL.
  3. SWEEP      Read every resolved board, keep only biostatistics, statistical
                programming, RWE, biometrics and computational-biology roles
                (rotwang's "rwd" gate), and compare against what you've seen.
  4. ALERT      New roles are written to radar_alert.md, emailed if SMTP is
                configured, and baked into radar.html — the radar display.

Needs rotwang.py in the same folder.  pip install requests

  python radar.py --build                 build the company universe (once, ~5 min)
  python radar.py --resolve 500           find careers boards for 500 companies
  python radar.py                         sweep: resolve a slice, scan, alert
  python radar.py --status                coverage report
  python radar.py --add "Moderna" URL     map a company to its careers URL by hand
  python radar.py --open                  open radar.html after the sweep
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import smtplib
import sys
import tempfile
import time
import webbrowser
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("radar.py needs the requests package:  pip install requests")

HERE = Path(__file__).resolve().parent
if not (HERE / "rotwang.py").exists():
    sys.exit("radar.py must sit in the same folder as rotwang.py")
sys.path.insert(0, str(HERE))
with contextlib.redirect_stdout(io.StringIO()):
    import rotwang as rw          # board readers, the rwd gate, scoring
import careers                    # careers pages: website -> careers system -> roles

# ── files ────────────────────────────────────────────────────────────────────
COMPANIES = HERE / "radar_companies.json"   # the universe + each company's board
SEEN      = HERE / "radar_seen.json"        # every role ever shown, with first-seen date
CONFIG    = HERE / "radar_config.json"      # SEC contact email
HTML_OUT  = HERE / "radar.html"
ALERT_OUT = HERE / "radar_alert.md"
GOOGLE_SAVE = HERE / "radar_google.json"     # the last Google Jobs results, reused by reruns that week
LOG       = HERE / "radar_log.txt"          # every run and every error, readable from Drive
APP_DATA  = HERE / "data.json"              # what the installed app reads
META      = HERE / "radar_meta.json"        # when the universe was last rebuilt

# ── knobs ────────────────────────────────────────────────────────────────────
PROFILE        = os.getenv("RADAR_PROFILE", "rwd")
# SCOPE "wide" (default): every contract, consulting, freelance, fractional or
# interim role on every watched board, plus every role in your lane (biostat,
# stat programming, RWE, biometrics, comp bio, health data science / AI) whether
# contract or permanent. Ranking does the sorting: your lane first, best fit first.
# SCOPE "narrow": your lane only — the original behaviour.  --narrow switches.
SCOPE          = os.getenv("RADAR_SCOPE", "quant")
# "quant" (default): your lane in any employment type, plus contract roles only
# when the title is quantitative — statistics, data science, programming,
# AI/ML, bioinformatics, analytics. "wide": every professional contract role.
# "narrow": your lane only.  --wide / --narrow switch.
AGENCY_FEED_PER_RUN = int(os.getenv("RADAR_FEED", "40"))   # recruiters asked of Adzuna per run
# Claude reads each candidate job and decides — instead of keyword lists.
CLASSIFY_MODEL = os.getenv("ROTWANG_CLASSIFY_MODEL", "claude-haiku-5-5")
CLASS_CACHE    = HERE / "radar_class.json"      # each job is judged once, then remembered
CLASS_BATCH    = 20
SHOW_PERM      = False                          # --perm: also list permanent roles in your field
CONTRACT_KINDS = {"contract", "contract_to_hire", "freelance", "part_time_contract", "hourly"}
# Contract-only aggregator sweep: your roles, by name, contract listings only.
# Leadership titles, searched across the indexes regardless of which careers
# system a company uses (Ipsen, Lilly, BioCryst, nonprofits…).
LEADERSHIP_QUERIES = ["senior director biostatistics", "executive director biostatistics",
                      "head of biostatistics", "vice president biostatistics",
                      "director biostatistics", "associate director biostatistics",
                      "director biometrics", "head of biometrics", "director statistical programming",
                      "head of statistical programming", "director statistics", "head of data science"]
CONTRACT_QUERIES = ["biostatistician", "statistician", "statistical programmer", "SAS programmer",
                    "clinical programmer", "R programmer", "data scientist", "machine learning engineer",
                    "AI engineer", "bioinformatics", "epidemiologist", "real world evidence",
                    "biometrics", "clinical data scientist", "statistical analyst",
                    "healthcare AI engineer", "clinical AI", "AI scientist", "LLM engineer",
                    "generative AI", "AI data scientist", "machine learning scientist healthcare"]
# Energy: the same disciplines, at oil & gas, pipeline, utility, solar / wind /
# storage, grid-software and trading companies.
ENERGY_CONTRACT_QUERIES = ["data scientist energy", "data scientist utility", "data scientist renewable",
                           "data scientist oil gas", "statistician energy", "machine learning energy",
                           "quantitative analyst energy", "quantitative analyst power", "load forecasting",
                           "energy forecasting analyst", "energy analytics", "power market analyst",
                           "energy trading quantitative", "energy data analyst", "energy modeling"]
ENERGY_LEADERSHIP_QUERIES = ["director data science energy", "head of data science energy",
                             "director analytics utility", "director forecasting energy",
                             "director quantitative analytics energy", "head of analytics renewable"]
TECH_CONTRACT_QUERIES = ["statistician contract", "data scientist contract", "applied scientist contract",
                         "data scientist experimentation", "causal inference data scientist",
                         "statistician semiconductor", "data scientist semiconductor", "yield data scientist",
                         "reliability statistician", "data scientist photonics", "data scientist optics",
                         "machine learning scientist contract", "quantitative researcher contract",
                         "statistical consultant", "survey statistician", "economist data scientist",
                         "AI engineer contract", "machine learning engineer contract", "LLM engineer contract",
                         "generative AI engineer contract", "NLP engineer contract",
                         "computer vision engineer contract", "AI data scientist contract",
                         "MLOps engineer contract", "AI consultant contract", "AI architect contract",
                         "AI research scientist contract"]
TECH_LEADERSHIP_QUERIES = ["director data science technology", "head of data science",
                           "director statistics", "principal statistician",
                           "head of AI", "director machine learning", "director of AI healthcare"]
HIGH_RATE_HOURLY    = 100      # $/hr — the "High rate" section starts here
HIGH_RATE_ANNUAL    = 200_000
RESOLVE_BUDGET = int(os.getenv("RADAR_RESOLVE", "400"))   # companies probed per sweep
RETRY_NONE     = 21        # days before re-probing a company with no board
STRIKES        = 3         # consecutive failed reads before a board is re-resolved
WORKERS        = 12
# SEC industry codes -> (label, sector). Device and diagnostics codes added so
# the universe covers surgical, orthopedic, electromedical, imaging, dental and
# ophthalmic makers, lab-instrument companies and medical laboratories.
SEC_SICS = {
    "2833": ("Medicinal chemicals & botanical products", "pharma"),
    "2834": ("Pharmaceutical preparations", "pharma"),
    "2835": ("In vitro diagnostics", "diagnostics"),
    "2836": ("Biological products", "biotech"),
    "3826": ("Laboratory analytical instruments", "diagnostics"),
    "3841": ("Surgical & medical instruments", "device"),
    "3842": ("Orthopedic, prosthetic & surgical appliances", "device"),
    "3843": ("Dental equipment & supplies", "device"),
    "3844": ("X-ray & imaging apparatus", "device"),
    "3845": ("Electromedical & electrotherapeutic apparatus", "device"),
    "3851": ("Ophthalmic goods", "device"),
    "8071": ("Medical laboratories", "diagnostics"),
    "8731": ("Commercial physical & biological research", "biotech"),
    # energy
    "1311": ("Crude petroleum & natural gas", "energy"),
    "1381": ("Drilling oil & gas wells", "energy"),
    "1382": ("Oil & gas field exploration services", "energy"),
    "1389": ("Oil & gas field services", "energy"),
    "2911": ("Petroleum refining", "energy"),
    "4610": ("Pipelines, except natural gas", "energy"),
    "4911": ("Electric services", "energy"),
    "4922": ("Natural gas transmission", "energy"),
    "4923": ("Natural gas transmission & distribution", "energy"),
    "4924": ("Natural gas distribution", "energy"),
    "4931": ("Electric & other services combined", "energy"),
    "4932": ("Gas & other services combined", "energy"),
    "4991": ("Cogeneration & small power producers", "energy"),
    "3621": ("Motors & generators", "energy"),
    "3533": ("Oil & gas field machinery", "energy"),
    "5172": ("Petroleum products wholesale", "energy"),
    # optics, electronics, semiconductors, instruments, software
    "3827": ("Optical instruments & lenses", "tech"),
    "3674": ("Semiconductors & related devices", "tech"),
    "3559": ("Special industry machinery (chip equipment)", "tech"),
    "3571": ("Electronic computers", "tech"),
    "3572": ("Computer storage devices", "tech"),
    "3576": ("Computer communications equipment", "tech"),
    "3577": ("Computer peripheral equipment", "tech"),
    "3661": ("Telephone & telegraph apparatus", "tech"),
    "3663": ("Radio & TV broadcasting & communications equipment", "tech"),
    "3669": ("Communications equipment", "tech"),
    "3672": ("Printed circuit boards", "tech"),
    "3678": ("Electronic connectors", "tech"),
    "3679": ("Electronic components", "tech"),
    "3812": ("Search, detection, navigation & guidance instruments", "tech"),
    "3823": ("Industrial instruments for measurement", "tech"),
    "3825": ("Instruments for measuring electricity", "tech"),
    "3829": ("Measuring & controlling devices", "tech"),
    "3861": ("Photographic equipment & supplies", "tech"),
    "7370": ("Computer programming & data processing", "tech"),
    "7371": ("Computer programming services", "tech"),
    "7372": ("Prepackaged software", "tech"),
    "7373": ("Computer integrated systems design", "tech"),
    "7374": ("Computer processing & data preparation", "tech"),
}
# rotwang_targets.json groups read into the universe -> sector
CURATED_GROUPS = {
    "biotech": "biotech", "pharma": "pharma", "healthai": "healthai", "cro": "cro",
    "device": "device", "surgical": "device", "diagnostics": "diagnostics",
    "device_cro": "cro", "recruiter": "agency", "staffing_medtech": "agency",
    "public": "research", "nonprofit_research": "research",
    "energy_oilgas": "energy", "energy_midstream": "energy", "energy_utility": "energy",
    "energy_renewables": "energy", "energy_tech": "energy", "energy_services": "energy",
    "energy_consulting": "energy", "energy_staffing": "energy",
    "tech_optics": "tech", "tech_semis": "tech", "tech_hardware": "tech", "tech_software": "tech",
    "tech_consulting": "tech", "tech_staffing": "tech",
}
HEALTH_SECTORS = {"biotech", "pharma", "healthai", "cro", "device", "diagnostics", "research",
                  "biopharma"}


def company_sector(c: dict) -> str:
    """'health', 'energy', 'tech', or '' when the registry can't say (recruiters, feed-only names).
    A company on two lists goes to the first that applies, in that order."""
    secs = set((c or {}).get("sectors") or [])
    if secs & HEALTH_SECTORS:
        return "health"
    if "energy" in secs:
        return "energy"
    if "tech" in secs:
        return "tech"
    return ""
FDA_YEARS  = 3            # device clearances/approvals this recent mark a live company
SBIR_YEARS = 3            # NIH small-business grant years read
# FDA review panels whose devices are diagnostics rather than therapeutic hardware
FDA_DX_PANELS = {"chemistry", "clinical chemistry", "hematology", "immunology",
                 "microbiology", "pathology", "toxicology", "clinical toxicology",
                 "molecular genetics", "immunology and microbiology"}
WORKDAY_TERMS = ["biostatistician", "statistician", "statistical programmer", "biometrics",
                 "real world evidence", "epidemiologist", "bioinformatics",
                 "clinical evidence", "data scientist", "machine learning", "artificial intelligence",
                 "contractor"]
WORKDAY_ENERGY_TERMS = ["data scientist", "statistician", "quantitative", "forecasting",
                        "machine learning", "contractor"]
WORKDAY_TECH_TERMS = ["statistician", "data scientist", "applied scientist", "quantitative",
                      "machine learning", "artificial intelligence", "contractor"]
# Wide scope also asks Workday boards for contract work by name
WORKDAY_WIDE_TERMS = ["contract", "contractor", "consultant", "temporary", "freelance"]
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")

# Contract roles kept in wide scope unless the title is clearly not professional
# work for you: sales, admin, trades, retail, hourly patient care. Deliberately
# short — the point of wide scope is to see the market, and fit ranking keeps
# your lane on top.
_WIDE_EXCLUDE = re.compile(
    r"\b(sales|account executive|account manager|business development|bdr|sdr|"
    r"recruit\w*|talent acquisition|receptionist|administrative assistant|"
    r"executive assistant|office (manager|assistant|coordinator)|facilit\w*|"
    r"janitor|custodian|housekeep\w*|warehouse|forklift|driver|courier|delivery|"
    r"cashier|retail|store|barista|cook|chef|server|security (guard|officer)|"
    r"customer (service|support|success)|call center|"
    r"phlebotom\w*|patient care|caregiver|nursing assistant|cna|home health aide|"
    r"electrician|plumber|hvac|mechanic|welder|maintenance technician|"
    r"intern|internship|co-op|apprentice)\b", re.I)


_NEVER = re.compile(r"\b(intern|internship|co-op|coop|apprentice|summer student|student worker|"
                    r"post-?doc\w*|residency|resident|fellowship|graduate program|early career program)\b", re.I)
# Fixed-term training posts (postdocs, residencies, fellowships) are not contracts.

_TITLE_TAIL = re.compile(r"\s*(?:[:|\u2013-]\s*)?(?:view the role|tell me more|apply now|learn more|"
                         r"read more|view job|see details|more info)\s*$", re.I)
_TITLE_LOC = re.compile(r"\s+Location:\s.*$", re.I)


def tidy_title(t: str) -> str:
    """What a careers-page scrape glued onto a title: button text, a location
    line, or UTF-8 read as Latin-1."""
    t = t or ""
    if "\u00e2" in t or "\u00c3" in t:
        try:
            t = t.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    t = _TITLE_LOC.sub("", t)
    t = _TITLE_TAIL.sub("", t)
    return re.sub(r"\s+", " ", t).strip(" :|-")


# The quantitative market: what a contract role's TITLE must mention to count
# in quant scope. Deliberately about the work, never the sector.
_QUANT = re.compile(
    r"statistic|biostat|biometric|data scien|data analy|analytics|machine learning|"
    r"\bml\b|\bai\b|artificial intelligence|deep learning|\bllm|\bnlp\b|\bml ?ops\b|computer vision|"
    r"generative|neural|natural language|"
    r"programm|\bsas\b|\br\b programm|python|bioinformat|computational|"
    r"quantitative|data engineer|epidemiolog|real[- ]world|\brwe\b|\bheor\b|"
    r"clinical data|data manag|model(l)?(ing|er)|forecast|simulation|"
    r"econometric|psychometric|actuar|mathematic|algorithm", re.I)


# "Contract" as the SUBJECT of a job (pricing, contracting analytics, contract
# strategy) is not an engagement type. Only an explicit engagement marker wins.
_CONTRACT_NOUN = re.compile(
    r"contract(ing|s)?\s+(analytics|analyst|strategy|manag|operations|specialist|"
    r"administrat|negotiat|pricing|compliance|lifecycle)|pricing and contracting|"
    r"contracting analytics|contract analytics|market access", re.I)
_ENGAGEMENT = re.compile(
    r"\(contract|contractor|\bcontract\b\s*[-–—)]|[-–—(]\s*contract\b|temporary|temp\b|"
    r"freelance|fractional|interim|consultant|1099|fixed[- ]term|locum|\bcontract to hire", re.I)

_US = re.compile(r"\b(remote|anywhere|united states|usa|u\.s\.|us\b|[A-Z]{2}\b, ?(us|usa)|"
                 r"california|new york|boston|massachusetts|new jersey|north carolina|texas|"
                 r"pennsylvania|illinois|washington|maryland|connecticut|florida|georgia)", re.I)


_US_STATE = re.compile(
    r"(^|[,\s(])(AL|AK|AZ|AR|CA|CO|CT|DE|DC|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|"
    r"NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY)\b")


def us_or_remote(location: str) -> bool:
    loc = location or ""
    return bool(_US.search(loc) or _US_STATE.search(loc)) or not loc.strip()


# Senior statistical leadership: permanent on paper, but the need — someone to
# own the trials, the SAP, the submission — is work a senior consultant can
# propose to do fractionally or on contract.
_LEAD_LEVEL = re.compile(
    r"\b(vp|vice president|head of|head,|chief|executive director|senior director|"
    r"sr\.? director|director|associate director|assoc\.? director|principal|lead)\b", re.I)
_LEAD_FIELD = re.compile(
    r"biostat|biometric|statistic|statistical program|clinical program|data scien|"
    r"quantitative|pharmacometric|epidemiolog|real[- ]world|clinical data science|"
    r"machine learning|\bai\b|bioinformat", re.I)
LEAD_PAY_ANNUAL = 200_000       # a stated salary this high marks a role worth pitching
_CORE_LEAD = re.compile(r"biostat|biometric|statistic|statistical program|clinical program|data scien|"
                        r"pharmacometric|epidemiolog|bioinformat|machine learning", re.I)


# Without Claude reading each posting, a role must say your discipline in its
# title and nothing that marks it as another job. Strict on purpose: a missed
# role costs less than a list full of noise.
_STRICT_FIELD = re.compile(
    r"statistic|biostat|biometric|data scien|machine learning|\bml\b|deep learning|bioinformat|"
    r"computational (bio|chem|scien)|epidemiolog|real[- ]world|\brwe\b|\bheor\b|pharmacometric|"
    r"quantitative|forecast|\bsas\b|clinical (data scien|programm(er|ing))|statistical program|\br programm|"
    r"data analy|analytics|\bai (research|scien)|(applied|research) scientist|econometric|"
    r"operations research|optimi[sz]ation|\bquant\b|decision scien|experimentation|causal|"
    r"economist|yield (analy|data|scien)|reliability (analy|statistic|data|scien)|"
    r"measurement scien|six sigma|design of experiments|\bdoe\b|process (analytics|data scien)|"
    r"(image|signal) (analysis|processing) (scien|algorithm)|computer vision (scien|research)|"
    r"algorithm (scien|develop|research)|metrology (data|algorithm)|survey (statistic|methodolog)|"
    r"psychometric|sampling", re.I)
_JUNK = re.compile(
    r"annotat|\btrainer\b|tutor|artist|designer|\bhris\b|people (data|analytics)|\bhr\b|"
    r"human resources|payroll|\bcnc\b|machinist|attorney|legal|lawyer|paralegal|counsel|"
    r"\bproduct\b|business development|\bsales\b|marketing|growth|recruit|account (manager|executive)|"
    r"nurse|\brn\b|physician|software engineer|front[- ]?end|back[- ]?end|full[- ]stack|devops|"
    r"architect|founder|customer|technical support|it support|writer|translator|interpreter|real estate|crypto|"
    r"blockchain|web3|pricing|payer|contracting|procurement|supply chain|facilities|strategy|"
    r"transformation|platform|infrastructure|security|help ?desk|video|finance|"
    r"evangelist|advocate|solutions? (engineer|consultant)|partner|program manag|project manag|"
    r"technical program|site reliability|\bqa\b|test engineer|\bgrants?\b|tooling", re.I)
_HEALTH_TITLE = re.compile(r"biostat|clinical|pharma|oncolog|real[- ]world|\brwe\b|\bheor\b|"
                           r"epidemiolog|bioinformat|biometric|pharmacometric|health|medical|patient", re.I)


_TECH_PITCH = re.compile(
    r"statistic|biostat|((director|head|vp|vice president|chief)\b.*(data scien|decision scien|"
    r"quantitative|analytics|experimentation|economi|machine learning|\bml\b|\bai\b|"
    r"artificial intelligence))", re.I)


# AI and machine-learning work (engineering, research, data science). These
# titles often say engineer, architect or platform, words that mark other jobs
# elsewhere, so they have their own, shorter list of what disqualifies them.
_AI_TITLE = re.compile(
    r"machine learning|\bml\b|\bml ?ops\b|\bai\b|a\.i\.|artificial intelligence|deep learning|"
    r"\bllms?\b|large language|generative|\bgen ?ai\b|\bnlp\b|natural language|computer vision|"
    r"\bai/ml\b|\bml/ai\b|foundation model|\brag\b|agentic|ai agents?|prompt engineer|"
    r"reinforcement learning|neural", re.I)
_AI_ROLE = re.compile(r"engineer|scientist|research|developer|architect|specialist|consultant|lead|"
                      r"head|director|manager|principal|analyst|modeler|expert|statistician|programmer", re.I)
_AI_NOT = re.compile(
    r"annotat|\btrainer\b|\btraining (data|tasks?)\b|tutor|rater|evaluator|label|\bsales\b|marketing|"
    r"recruit|account (manager|executive)|customer|writer|translator|\bproduct (manager|owner|marketing)|"
    r"program manag|project manag|business development|partnerships?|legal|counsel|policy|"
    r"\bteacher\b|instructor|professor|faculty|postdoc|intern\b|internship|student", re.I)


def ai_title(title: str) -> bool:
    t = title or ""
    return bool(_AI_TITLE.search(t) and _AI_ROLE.search(t)) and not _AI_NOT.search(t)


def strict_title(title: str) -> bool:
    t = title or ""
    return (bool(_STRICT_FIELD.search(t)) and not _JUNK.search(t)) or ai_title(t)


_LEAD_NOT = re.compile(
    r"account|sales|brand|marketing|governance|privacy|policy|legal|compliance|deployment|partner|"
    r"business development|customer|recruit|talent|communications|commercial|go[- ]to[- ]market|\bgtm\b|"
    r"alliance|solutions|\bfield\b|regional|enablement|platform engineering|data platform|infrastructure|"
    r"product (manag|lead|director)|program manag|procurement|finance|security", re.I)


def leadership_title(title: str) -> bool:
    t = title or ""
    return bool(_LEAD_LEVEL.search(t) and _LEAD_FIELD.search(t)) and not _LEAD_NOT.search(t)


_ENERGY_QUANT = re.compile(
    r"operations research|optimi[sz]ation|\bquant\b|risk (analy|model|quant)|meteorolog|"
    r"load research|market (analy|model|fundamentals)|fundamentals analy|structur(ing|er)\b|"
    r"resource assessment|performance analy|evaluation|measurement (and|&) verification|"
    r"\bem&v\b|pricing analy|portfolio analy|energy analy|power analy", re.I)
# Engineering and field work that shares words with the pattern above
_ENERGY_NOT = re.compile(
    r"reservoir|drilling|petroleum engineer|process engineer|electrical engineer|mechanical engineer|"
    r"civil engineer|protection engineer|substation|lineman|technician|operator\b|field service|"
    r"installer|electrician|land(man| agent)|permitting|interconnection engineer|"
    r"project manager|construction|scada engineer|plc\b|controls engineer|site manager|"
    r"safety|environmental (specialist|scientist)|geolog|geophysic|welding|pipefitter", re.I)


def quant_title(title: str, energy: bool = False) -> bool:
    t = title or ""
    if energy:
        return bool((_QUANT.search(t) or _ENERGY_QUANT.search(t)) and not _ENERGY_NOT.search(t))
    return bool(_QUANT.search(t))


def hourly_rate(pay: str):
    """$/hr implied by a stated pay figure, or None. Modelled estimates ('est.')
    are not real rates and return None."""
    if not pay or "est." in pay:
        return None
    v = rw.pay_value(pay)
    h = round(v / 2080) if v and v > 0 else None
    return h if h and h <= 400 else None


_HARD_ENGAGEMENT = re.compile(r"contract|temporary|\btemp\b|freelance|fractional|interim|1099|"
                              r"fixed[- ]term|hourly|part[- ]time", re.I)
_CORE_TITLE = re.compile(r"statistic|biostat|programmer|data scien|machine learning|bioinformat|"
                         r"epidemiolog|real[- ]world|\brwe\b|quantitative|forecast|\bai\b|"
                         r"artificial intelligence|\bllm|\bnlp\b|computer vision|deep learning", re.I)


def _SOFT_ONLY(title: str) -> bool:
    """A title whose only contract signal is the word consultant/consulting and
    which is not plainly your discipline — at software and consulting firms
    that is a permanent job title, not an engagement."""
    t = title or ""
    return bool(re.search(r"consult", t, re.I)) and not _HARD_ENGAGEMENT.search(t) and not _CORE_TITLE.search(t)


def wide_contract(r) -> bool:
    if not (r.contract or rw.is_contract(r.title)):
        return False
    return not _WIDE_EXCLUDE.search(r.title or "")


# ── small helpers ────────────────────────────────────────────────────────────
_LEGAL = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
          "llc", "plc", "sa", "ag", "nv", "se", "bv", "gmbh", "holdings", "holding",
          "group", "the", "lp", "de", "kk"}


_KEEP_UPPER_NOT = {"ELI", "THE", "AND", "OF", "ONE", "DAY", "BIO", "NEW", "SUN", "ACE", "ARC"}


def display_name(raw: str) -> str:
    """'SYNDAX PHARMACEUTICALS INC /DE/' -> 'Syndax Pharmaceuticals'."""
    s = re.sub(r"\s*/\s*[A-Z]{2,3}\s*/?\s*$", "", (raw or "").strip())
    s = re.sub(r"\s*\(.*?\)\s*", " ", s)
    words = s.replace(",", " ").split()
    while words and (words[-1].lower().strip(".") in _LEGAL or words[-1] in {"&", "and", "-"}):
        words.pop()
    s = " ".join(words).strip(" .") or (raw or "").strip()
    if s.isupper():
        out = []
        for w in s.split():
            if any(ch.isdigit() for ch in w):
                out.append(w)                                  # 4D, 2seventy
            elif len(w) <= 3 and w.isalpha() and w not in _KEEP_UPPER_NOT:
                out.append(w)                                  # UCB, GSK, AB
            else:
                out.append("-".join(p.capitalize() for p in w.split("-")))
        s = " ".join(out)
    return s


def company_key(name: str) -> str:
    words = re.split(r"[^a-z0-9]+", (name or "").lower().replace("&", " "))
    return "".join(w for w in words if w and w not in _LEGAL and w != "and")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def log(msg: str) -> None:
    """Append to radar_log.txt. Lives in the Drive folder, so a failed run can
    be diagnosed without anyone copying text out of a console window."""
    try:
        with LOG.open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {msg}\n")
    except Exception:
        pass


def save_json(path: Path, data) -> None:
    """Write via a temp file, then swap it in. Google Drive for desktop can hold
    a file open while it uploads, so the swap is retried and, as a last resort,
    the file is written in place."""
    text = json.dumps(data, indent=1, ensure_ascii=False)
    tmp = path.with_suffix(".tmp")
    for attempt in range(6):
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(path)
            return
        except OSError as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    try:
        path.write_text(text, encoding="utf-8")
        tmp.unlink(missing_ok=True)
        log(f"save {path.name}: swap failed ({last}), wrote in place")
    except OSError as e:
        log(f"save {path.name} FAILED: {e!r}")
        raise


def sec_contact() -> str:
    """The SEC refuses requests without a contact address in the User-Agent."""
    cfg = load_json(CONFIG, {})
    email = os.getenv("RADAR_SEC_EMAIL") or cfg.get("sec_email") or os.getenv("RADAR_EMAIL_TO")
    if not email:
        if not sys.stdin or not sys.stdin.isatty():
            return ""                              # unattended: the SEC source is skipped
        email = input("The SEC asks for a contact email with each request. Your email: ").strip()
        cfg["sec_email"] = email
        save_json(CONFIG, cfg)
    return email


# ═══════════════════════════════════════════════════════════ NETWORK LAYER ════
# rotwang's session retries on HTTP 429 and honours the server's Retry-After,
# with no ceiling. After ~12,000 probes an ATS answered "retry in an hour" and
# the run sat frozen at 625/1000 — twice. Here nothing ever waits on a server's
# say-so: a rate-limited host is skipped for a short cool-down, the company is
# marked for retry, and every company has a hard time budget.

from requests.adapters import HTTPAdapter
try:
    from urllib3.util.retry import Retry
except Exception:                                     # pragma: no cover
    from requests.packages.urllib3.util.retry import Retry

TIMEOUT      = (4, 9)     # connect, read — seconds
PER_COMPANY  = 45         # hard budget for finding one company's board
COOLDOWN     = 90         # seconds to leave a host alone after it says "slow down"
_cool: dict[str, float] = {}
_cool_lock = threading.Lock()


def radar_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": "rotwang-radar/1.0", "Accept": "application/json"})
    s.mount("https://", HTTPAdapter(pool_maxsize=WORKERS * 2, max_retries=Retry(
        total=1, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504],
        respect_retry_after_header=False, allowed_methods=frozenset(["GET", "POST"]))))
    return s


# Workday answers "429 Too Many Requests" to GitHub's computers when asked in a
# burst, which on the 2026-11-24 run made 204 of 206 large companies look absent.
# Every Workday request now goes through one shared pace, and a 429 is waited out.
WD_GAP = 1.5                 # seconds between Workday requests, across all threads (grows on 429)
_wd_lock = threading.Lock()
_wd_last = [0.0]
WD_STATS = {"requests": 0, "limited": 0}
WD_STOP = [float("inf")]     # when this run's Workday time is up (set by each phase)
WD_DISCOVER_MIN = int(os.getenv("RADAR_WD_DISCOVER", "5"))   # minutes for finding new sites
WD_READ_MIN = int(os.getenv("RADAR_WD_READ", "25"))           # minutes for reading known sites


def wd_request(s, method: str, url: str, **kw):
    """A Workday request at the shared pace; a 429 is retried after its wait."""
    global WD_GAP
    kw.setdefault("timeout", 25)
    for attempt in range(3):
        if time.time() > WD_STOP[0]:
            return None                        # Workday's time this run is up
        with _wd_lock:
            wait = WD_GAP - (time.time() - _wd_last[0])
            if wait > 0:
                time.sleep(wait)
            _wd_last[0] = time.time()
        WD_STATS["requests"] += 1
        try:
            r = s.request(method, url, **kw)
        except requests.RequestException:
            return None
        if r.status_code != 429:
            return r
        WD_STATS["limited"] += 1
        WD_GAP = min(4.0, WD_GAP + 0.5)        # ask more slowly from now on
        ra = r.headers.get("Retry-After", "")
        pause = min(60, int(ra)) if ra.isdigit() else 15 * (attempt + 1)
        pause = max(0.0, min(pause, WD_STOP[0] - time.time()))
        with _wd_lock:                         # everyone waits, not only this thread
            time.sleep(pause)
            _wd_last[0] = time.time()
    return None


def _host(url: str) -> str:
    m = re.match(r"https?://([^/]+)", url)
    host = m.group(1) if m else url
    return "recruitee" if host.endswith(".recruitee.com") else host


def _cooling(host: str) -> bool:
    with _cool_lock:
        return _cool.get(host, 0) > time.time()


def _cool_down(host: str) -> None:
    with _cool_lock:
        already = _cool.get(host, 0) > time.time()
        _cool[host] = time.time() + COOLDOWN
    if not already:
        log(f"rate-limited by {host} — skipping it for {COOLDOWN}s")


# Workable throttles after a handful of requests, and Recruitee (a European ATS)
# produced nothing but impostor matches for US biotech. When either was cooling
# down, every company probed in that window was marked "retry" — 143 of 150 on
# one run. They are now secondary: probed only with the company's full name, and
# a skipped or refused secondary never makes a company count as unchecked.
SKIP_ATS      = {"recruitee"}
SECONDARY_ATS = {"workable"}


def find_board(s, name: str):
    """Try each plausible address on each ATS, within PER_COMPANY seconds.
    Returns (result or None, limited) — limited means a PRIMARY ATS (Greenhouse,
    Lever, Ashby, SmartRecruiters) was skipped or refused, so 'no board found'
    can't be trusted and the company is retried next run."""
    deadline = time.time() + PER_COMPANY
    limited = False
    tokens = [t for t in rw.slugs(name) if t and len(t) >= 2]
    strong = set(tokens[:2])                     # display name and joined full name
    for token in tokens:
        for ats, url in rw._probe_urls(token):
            if time.time() > deadline:
                return None, True
            if ats in SKIP_ATS:
                continue
            secondary = ats in SECONDARY_ATS
            if secondary and token not in strong:
                continue
            h = _host(url)
            if _cooling(h):
                if not secondary:
                    limited = True
                continue
            try:
                r = s.get(url, timeout=TIMEOUT)
            except Exception:
                continue
            if r.status_code == 429:
                _cool_down(h)
                if not secondary:
                    limited = True
                continue
            if r.status_code != 200:
                continue
            try:
                d = r.json()
            except ValueError:
                continue
            n = len(d) if isinstance(d, list) else (
                len(d.get("jobs") or d.get("content") or d.get("offers") or []) if isinstance(d, dict) else 0)
            if n:
                return {"ats": ats, "name": name, "token": token, "open": n}, limited
    return None, limited


# ════════════════════════════════════════════════════════════ 1. UNIVERSE ════

def fetch_sec(email: str) -> dict[str, dict]:
    """Every currently listed company in the drug, biologics, diagnostics, device
    and R&D industry codes. The SIC listing includes decades of dead shells, so it
    is intersected with the SEC's current ticker file — listed means alive."""
    s = requests.Session()
    s.headers.update({"User-Agent": f"rotwang-radar {email}", "Accept-Encoding": "gzip"})
    print("  SEC: loading the current ticker list")
    r = s.get("https://www.sec.gov/files/company_tickers.json", timeout=30)
    r.raise_for_status()
    live = {str(v["cik_str"]): v for v in r.json().values()}

    out = {}
    for sic, (label, sector) in SEC_SICS.items():
        start, got = 0, 0
        while True:
            r = s.get("https://www.sec.gov/cgi-bin/browse-edgar",
                      params={"action": "getcompany", "SIC": sic, "owner": "include",
                              "count": 100, "start": start, "output": "atom"},
                      timeout=30)
            if not r.ok:
                print(f"    SIC {sic}: stopped at {start} (HTTP {r.status_code})")
                break
            ciks = [c.lstrip("0") for c in re.findall(r"<cik>\s*(\d+)\s*</cik>", r.text)]
            for cik in ciks:
                if cik in live and cik not in out:
                    t = live[cik]
                    out[cik] = {"name": display_name(t.get("title", "")), "ticker": t.get("ticker", ""),
                                "cik": cik, "sic": sic, "sector": sector, "country": "US"}
                    got += 1
            if len(ciks) < 100:
                break
            start += 100
            time.sleep(0.15)                       # well inside the SEC's 10 req/s
        print(f"    SIC {sic} {label}: {got} listed companies")
    return {company_key(v["name"]): v for v in out.values()}


def _fda_pages(s, endpoint: str, search: str):
    """Page through an openFDA endpoint: 1,000 records per request, 25,000 max."""
    skip = 0
    while skip <= 25000:
        try:
            r = s.get(f"https://api.fda.gov/device/{endpoint}.json",
                      params={"search": search, "limit": 1000, "skip": skip}, timeout=60)
        except requests.RequestException as e:
            print(f"    {endpoint}: stopped at {skip} ({e.__class__.__name__})")
            return
        if r.status_code == 404:                   # openFDA's way of saying "no more"
            return
        if not r.ok:
            print(f"    {endpoint}: stopped at {skip} (HTTP {r.status_code})")
            return
        rows = (r.json() or {}).get("results", []) or []
        yield from rows
        if len(rows) < 1000:
            return
        skip += 1000
        time.sleep(0.3)                            # openFDA: 240 requests/min without a key


def fetch_fda_devices() -> dict[str, dict]:
    """Every company with an FDA device decision in the last FDA_YEARS years:
    510(k) clearances and De Novo grants (one dataset) plus PMA approvals.
    The review panel says what kind of device — surgical, orthopedic,
    cardiovascular, neurology, radiology — and diagnostics panels mark IVD makers."""
    s = requests.Session()
    s.headers.update({"User-Agent": "rotwang-radar", "Accept": "application/json"})
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=365 * FDA_YEARS)
    span = f"decision_date:[{start:%Y%m%d} TO {end:%Y%m%d}]"
    out: dict[str, dict] = {}
    for endpoint, label in [("510k", "510(k) & De Novo"), ("pma", "PMA")]:
        print(f"  FDA devices: reading {label} decisions since {start:%Y-%m-%d}")
        n = 0
        for row in _fda_pages(s, endpoint, span):
            name = (row.get("applicant") or "").strip()
            if not name:
                continue
            n += 1
            k = company_key(display_name(name))
            if not k:
                continue
            panel = (row.get("advisory_committee_description") or "").strip()
            c = out.setdefault(k, {"name": display_name(name), "clearances": 0,
                                   "country": (row.get("country_code") or "").upper(),
                                   "panels": {}})
            c["clearances"] += 1
            if panel:
                c["panels"][panel] = c["panels"].get(panel, 0) + 1
        print(f"    {n} decisions")
    for c in out.values():
        top = max(c["panels"], key=c["panels"].get) if c["panels"] else ""
        c["specialty"] = top
        c["sector"] = "diagnostics" if top.lower() in FDA_DX_PANELS else "device"
        c.pop("panels", None)
    us = sum(1 for c in out.values() if c["country"] == "US")
    print(f"    {len(out)} device companies ({us} US-based)")
    return out


def fetch_sbir() -> dict[str, dict]:
    """Every company holding an NIH small-business grant (SBIR/STTR) in the last
    SBIR_YEARS fiscal years: the earliest-stage biotech, device and diagnostics
    startups — pre-revenue, lean, and the most likely to hire on contract."""
    s = requests.Session()
    s.headers.update({"User-Agent": "rotwang-radar", "Accept": "application/json",
                      "Content-Type": "application/json"})
    year = datetime.now(timezone.utc).year
    out: dict[str, dict] = {}
    for fy in range(year - SBIR_YEARS + 1, year + 1):
        offset, got = 0, 0
        while offset < 15000:                      # RePORTER caps offset at 14,999
            body = {"criteria": {"fiscal_years": [fy],
                                 "activity_codes": ["R41", "R42", "R43", "R44", "U43", "U44", "SB1"]},
                    "include_fields": ["Organization"], "offset": offset, "limit": 500}
            try:
                r = s.post("https://api.reporter.nih.gov/v2/projects/search", json=body, timeout=60)
            except requests.RequestException as e:
                print(f"    FY{fy}: stopped at {offset} ({e.__class__.__name__})")
                break
            if not r.ok:
                print(f"    FY{fy}: stopped at {offset} (HTTP {r.status_code})")
                break
            rows = (r.json() or {}).get("results", []) or []
            for row in rows:
                org = row.get("organization") or {}
                name = (org.get("org_name") or "").strip()
                if not name:
                    continue
                k = company_key(display_name(name))
                if k and k not in out:
                    out[k] = {"name": display_name(name), "sector": "startup",
                              "country": "US" if (org.get("org_country") or "").upper()
                              in ("", "UNITED STATES", "US", "USA") else org.get("org_country", "")}
                    got += 1
            if len(rows) < 500:
                break
            offset += 500
            time.sleep(0.4)
        print(f"  NIH small-business grants FY{fy}: {got} new companies")
    return out


def fetch_ctgov() -> dict[str, dict]:
    """Every company leading or co-sponsoring an active, US-sited industry trial,
    with a count of how many, tagged device / diagnostics / biopharma by what the
    trial tests. A company running trials needs statistical support now."""
    s = requests.Session()
    s.headers.update({"User-Agent": "rotwang-radar", "Accept": "application/json"})
    counts: dict[str, dict] = {}
    token, pages = None, 0
    print("  ClinicalTrials.gov: reading active industry-sponsored US trials")
    kind = {"DEVICE": "device", "DIAGNOSTIC_TEST": "diagnostics", "DRUG": "biopharma",
            "BIOLOGICAL": "biopharma", "GENETIC": "biopharma", "COMBINATION_PRODUCT": "device",
            "RADIATION": "device"}

    def note(name, types):
        name = (name or "").strip()
        if not name:
            return
        k = company_key(display_name(name))
        if not k:
            return
        c = counts.setdefault(k, {"name": display_name(name), "trials": 0, "kinds": {}})
        c["trials"] += 1
        for t in types:
            c["kinds"][t] = c["kinds"].get(t, 0) + 1

    fields = ("protocolSection.sponsorCollaboratorsModule,"
              "protocolSection.armsInterventionsModule.interventions.type")
    while True:
        params = {
            "filter.advanced": "AREA[LeadSponsorClass]INDUSTRY",
            "filter.overallStatus": "RECRUITING,NOT_YET_RECRUITING,ACTIVE_NOT_RECRUITING,ENROLLING_BY_INVITATION",
            "query.locn": "United States",
            "fields": fields,
            "pageSize": 1000,
        }
        if token:
            params["pageToken"] = token
        try:
            r = s.get("https://clinicaltrials.gov/api/v2/studies", params=params, timeout=60)
        except requests.RequestException as e:
            print(f"    stopped after {pages} page(s) ({e.__class__.__name__})")
            break
        if not r.ok and pages == 0 and "armsInterventions" in fields:
            # If the registry rejects the device-type field, keep the sponsors
            # anyway — losing 3,000 trial sponsors to tag them would be a bad trade.
            print("    device-type tagging not accepted — reading sponsors only")
            fields = "protocolSection.sponsorCollaboratorsModule"
            continue
        if not r.ok:
            print(f"    stopped after {pages} page(s) (HTTP {r.status_code})")
            break
        d = r.json()
        for st in d.get("studies", []):
            ps = st.get("protocolSection") or {}
            sp = ps.get("sponsorCollaboratorsModule") or {}
            ivs = ((ps.get("armsInterventionsModule") or {}).get("interventions") or [])
            types = {kind.get((iv.get("type") or "").upper(), "") for iv in ivs} - {""}
            note((sp.get("leadSponsor") or {}).get("name"), types)
            for col in sp.get("collaborators") or []:          # industry partners too
                if (col.get("class") or "").upper() == "INDUSTRY":
                    note(col.get("name"), types)
        pages += 1
        token = d.get("nextPageToken")
        if not token or pages >= 80:
            break
        time.sleep(0.3)
    for c in counts.values():
        ks = c.pop("kinds", {})
        c["sector"] = max(ks, key=ks.get) if ks else "biopharma"
    dev = sum(1 for c in counts.values() if c["sector"] in ("device", "diagnostics"))
    print(f"    {len(counts)} sponsoring companies across {pages} page(s) ({dev} device/diagnostics)")
    return counts


def fetch_curated() -> dict[str, dict]:
    d = load_json(HERE / "rotwang_targets.json", {})
    groups = d.get("targets", {}) if isinstance(d, dict) else {}
    out = {}
    for g, sector in CURATED_GROUPS.items():
        for n in groups.get(g, []):
            k = company_key(n)
            if k and k not in out:
                out[k] = {"name": n, "sector": sector}
    print(f"  Curated: {len(out)} names from rotwang_targets.json")
    return out


def build_universe() -> int:
    email = sec_contact()
    print("\nBuilding the company universe\n")
    reg = load_json(COMPANIES, {})
    sources = {}

    def _sec():
        if not email:
            print("  SEC: no contact email set (RADAR_SEC_EMAIL) — skipped; the other sources still run")
            return {}
        return fetch_sec(email)

    for label, fn in [("sec", _sec), ("fda", fetch_fda_devices),
                      ("ctgov", fetch_ctgov), ("sbir", fetch_sbir), ("curated", fetch_curated)]:
        try:
            sources[label] = fn()
        except Exception as e:                     # one source failing never stops the build
            print(f"  {label}: unreachable ({e.__class__.__name__}) — skipped this time")
            log(f"build: source {label} failed: {e!r}")
            sources[label] = {}

    added = 0
    new_by_source: dict[str, int] = {}
    for label, items in sources.items():
        for k, v in items.items():
            if not k:
                continue
            if k not in reg:
                reg[k] = {"name": v["name"], "sources": [], "status": "unresolved",
                          "trials": 0, "ticker": "", "added": TODAY}
                added += 1
                new_by_source[label] = new_by_source.get(label, 0) + 1
            c = reg[k]
            if label not in c["sources"]:
                c["sources"].append(label)
            sec = v.get("sector")
            if sec:
                c.setdefault("sectors", [])
                if sec not in c["sectors"]:
                    c["sectors"].append(sec)
            if v.get("country") and not c.get("country"):
                c["country"] = v["country"]
            if label == "ctgov":
                c["trials"] = v.get("trials", 0)
                c["name"] = v["name"]              # trial registry names read best
            if label == "sec":
                c["ticker"] = v.get("ticker", "")
            if label == "fda":
                c["clearances"] = v.get("clearances", 0)
                if v.get("specialty"):
                    c["specialty"] = v["specialty"]

    # Anything rotwang already resolved is reused, not re-probed.
    store = load_json(HERE / "rotwang_boards.json", {})
    for b in (store.get("resolved", []) if isinstance(store, dict) else store):
        k = company_key(b.get("name", ""))
        if k in reg and reg[k]["status"] == "unresolved" and not b.get("retired"):
            reg[k].update({"status": "watching", "ats": b["ats"], "token": b["token"],
                           "resolved": TODAY})

    save_json(COMPANIES, reg)
    save_json(META, {**load_json(META, {}), "built": TODAY})
    sectors: dict[str, int] = {}
    for c in reg.values():
        for sct in c.get("sectors", []) or ["unknown"]:
            sectors[sct] = sectors.get(sct, 0) + 1
    print(f"\n  {len(reg)} companies in the universe ({added} new this build"
          + (": " + ", ".join(f"{n} from {k}" for k, n in new_by_source.items()) if new_by_source else "")
          + ")")
    print("  By sector: " + ", ".join(f"{n} {k}" for k, n in sorted(sectors.items(), key=lambda x: -x[1])))
    print("  Next:  python radar.py --workday   then   python radar.py --resolve 9000\n")
    log(f"build: {len(reg)} companies, {added} new")
    return 0


# ═════════════════════════════════════════════════════════════ 2. RESOLVE ════

def parse_careers_url(url: str):
    """A careers URL pasted by hand -> (ats, token)."""
    u = url.strip()
    pats = [
        ("greenhouse", r"(?:job-boards|boards)(?:-api)?\.greenhouse\.io/(?:v1/boards/)?([^/?#]+)"),
        ("lever", r"jobs\.lever\.co/([^/?#]+)"),
        ("ashby", r"jobs\.ashbyhq\.com/([^/?#]+)"),
        ("smartrecruiters", r"(?:jobs|careers)\.smartrecruiters\.com/([^/?#]+)"),
        ("workable", r"apply\.workable\.com/([^/?#]+)"),
        ("recruitee", r"https?://([a-z0-9-]+)\.recruitee\.com"),
        ("paylocity", r"recruiting\.paylocity\.com/Recruiting/Jobs/All/(" + _GUID + ")"),
    ]
    for ats, p in pats:
        m = re.search(p, u, re.I)
        if m:
            return ats, requests.utils.unquote(m.group(1))
    m = re.search(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([^/?#]+)", u, re.I)
    if m:
        return "workday", f"{m.group(1)}.{m.group(2)}.myworkdayjobs.com|{m.group(1)}|{m.group(3)}"
    return None


# ── board verification ───────────────────────────────────────────────────────
# The prober falls back to shortened names ("kodiak" for Kodiak Sciences), and a
# short name often belongs to someone else: Kodiak Robotics, Novo the bank,
# Caribou the car refinancer. A board found by a shortened name must prove it
# is the right company before it goes on the radar.

_FILLER = set(getattr(rw, "_FILLER", set())) | {"ai", "bio", "health", "medical", "devices", "energy"}


def _words(text: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", (text or "").lower().replace("&", " "))
            if w and w not in _LEGAL and w != "and"]


def _is_strong(name: str, token: str) -> bool:
    """The token spells the company's full name, not a shortened form."""
    ws = _words(name)
    full = {(name or "").strip().lower(), "".join(ws), "-".join(ws)}
    return str(token).strip().lower() in full


def _core(text: str) -> list[str]:
    return [w for w in _words(text) if w not in _FILLER]


def _names_agree(board_name: str, company: str) -> bool:
    """The distinctive words of the two names must be the same set, both ways.
    'Kodiak Robotics' vs 'Kodiak Sciences' fails on 'robotics'; a board called
    just 'Axon' vs 'Axon Therapies' fails on 'therapies'. 'Suki' vs 'Suki AI'
    passes, because 'AI' is a descriptor."""
    b, c = set(_core(board_name)), set(_core(company))
    return bool(b) and b == c


_LIFE = ["clinical", "patient", "therap", "drug", "pharma", "biotech", "biolog", "oncolog",
         "disease", "medic", "trial", "fda", "genom", "protein", "molecul", "cell", "assay",
         "laborator", "diagnost", "regulatory", "gmp", "biomarker", "health", "immun", "vaccine",
         # medical device & surgical vocabulary, so a device maker's board verifies
         "device", "surgical", "surgeon", "implant", "510(k)", "510k", "iso 13485", "13485",
         "quality system", "catheter", "orthop", "cardiovascular", "imaging", "steril",
         "medtech", "ivd", "hospital", "physician", "clinic"]


def _life_science(text: str) -> bool:
    t = (text or "").lower()
    return sum(1 for w in _LIFE if w in t) >= 3


_ENERGY_WORDS = ["energy", "power", "solar", "wind", "grid", "utility", "utilities", "electric",
                 "renewable", "pipeline", "midstream", "natural gas", "oil", "lng", "refin",
                 "megawatt", " mw", "kwh", "storage", "battery", "interconnection", "ferc", "nerc",
                 "iso/rto", "ercot", "pjm", "transmission", "generation", "turbine", "photovoltaic",
                 " pv ", "drilling", "upstream", "nuclear", "geothermal", "hydrogen", "emissions",
                 "decarbon", "substation", "ppa", "offtake", "commodit", "epc"]


def _energy_work(text: str) -> bool:
    t = (text or "").lower()
    return sum(1 for w in _ENERGY_WORDS if w in t) >= 3


_TECH_WORDS = ["software", "engineer", "semiconductor", "optical", "optics", "photonic", "laser",
               "hardware", "machine learning", "cloud", "silicon", "chip", "firmware", "sensor",
               "lidar", "imaging", "algorithm", "developer", "saas", "wafer", "lithograph",
               "electronics", "data scien", "analytics", "technology", "platform", "robot",
               "aerospace", "network", "product manager", "consult", "recruit", "staffing"]


def _tech_work(text: str) -> bool:
    t = (text or "").lower()
    return sum(1 for w in _TECH_WORDS if w in t) >= 3


def _industry_ok(text: str, sector: str = "") -> bool:
    if sector == "energy":
        return _energy_work(text)
    if sector == "tech":
        return _tech_work(text)
    if sector == "health":
        return _life_science(text)
    return _life_science(text) or _energy_work(text) or _tech_work(text)


def _board_evidence(s, ats: str, token: str):
    """(the board's own company name if the system exposes one, posting text)."""
    q = requests.utils.quote(str(token))
    if ats == "greenhouse":
        bn = ""
        r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{token}", timeout=TIMEOUT)
        if r.ok:
            bn = (r.json() or {}).get("name", "") or ""
        r = s.get(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true", timeout=TIMEOUT)
        jobs = (r.json() or {}).get("jobs", []) if r.ok else []
        return bn, " ".join((j.get("title") or "") + " " + (j.get("content") or "") for j in jobs[:25])
    if ats == "smartrecruiters":
        r = s.get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings?limit=25", timeout=TIMEOUT)
        items = (r.json() or {}).get("content", []) if r.ok else []
        bn = next((((j.get("company") or {}).get("name") or "") for j in items), "")
        text = " ".join(j.get("name", "") or "" for j in items)
        for j in items[:3]:                       # list view has titles only
            try:
                d = s.get(f"https://api.smartrecruiters.com/v1/companies/{token}/postings/{j.get('id','')}",
                          timeout=TIMEOUT)
                if d.ok:
                    sec = ((d.json() or {}).get("jobAd") or {}).get("sections") or {}
                    text += " " + " ".join((v or {}).get("text", "") for v in sec.values()
                                           if isinstance(v, dict))
            except requests.RequestException:
                pass
        return bn, text
    if ats == "workable":
        r = s.get(f"https://apply.workable.com/api/v1/widget/accounts/{token}?details=true", timeout=TIMEOUT)
        d = (r.json() or {}) if r.ok else {}
        return d.get("name", "") or "", " ".join((j.get("title") or "") + " " + (j.get("description") or "")
                                             for j in d.get("jobs", [])[:25])
    if ats == "ashby":
        r = s.get(f"https://api.ashbyhq.com/posting-api/job-board/{q}", timeout=TIMEOUT)
        jobs = (r.json() or {}).get("jobs", []) if r.ok else []
        return "", " ".join((j.get("title") or "") + " " +
                            (j.get("descriptionPlain") or j.get("descriptionHtml") or "") for j in jobs[:25])
    if ats == "lever":
        r = s.get(f"https://api.lever.co/v0/postings/{token}?mode=json", timeout=TIMEOUT)
        jobs = r.json() if r.ok and isinstance(r.json(), list) else []
        return "", " ".join((j.get("text") or "") + " " + (j.get("descriptionPlain") or "") + " " +
                            (j.get("additionalPlain") or "") for j in jobs[:25])
    if ats == "recruitee":
        r = s.get(f"https://{token}.recruitee.com/api/offers/", timeout=TIMEOUT)
        offers = (r.json() or {}).get("offers", []) if r.ok else []
        bn = next((o.get("company_name") for o in offers if o.get("company_name")), "") or ""
        return bn, " ".join((o.get("title") or "") + " " + (o.get("description") or "") for o in offers[:25])
    return "", ""


def verify_board(s, name: str, ats: str, token: str, sector: str = "health") -> bool:
    """A board found by the company's full name is trusted. A board found by a
    shortened name must pass two tests:

      1. identity  - its own company name agrees with ours, or its postings
                     mention our full name ('Kodiak Sciences') or our
                     distinctive name ('Enveda'); and
      2. industry  - its postings read like life-science work (drugs, patients,
                     trials, biology). Every company on the radar is biotech,
                     pharma or health; a board that never says so belongs to
                     someone else — Kodiak Robotics, Caribou the car lender,
                     Arcus the energy company.

    The one exception to (2): postings that spell out our full name."""
    if _is_strong(name, token):
        return True
    try:
        board_name, text = _board_evidence(s, ats, token)
    except Exception:
        return False
    blob = " ".join(_words(text))
    full = " ".join(_words(name))
    core = " ".join(_core(name))
    if full and len(_words(name)) > len(_core(name)) and full in blob:
        return True                                    # "kodiak sciences" said outright
    identity = _names_agree(board_name, name) or (bool(core) and core in blob)
    return identity and _industry_ok(text, sector)


def _probe(s, name: str, sector: str = "health"):
    """One company: find a board, then verify it is really theirs. Any error is
    contained here — one odd name among thousands never stops the run."""
    try:
        res, limited = find_board(s, name)
        if res and not verify_board(s, name, res["ats"], res["token"], sector):
            return {"mismatch": res}, None
        if not res and limited:
            return None, "rate-limited or out of time — retried next run"
        return res, None
    except Exception as e:                      # noqa: BLE001 — deliberate
        return None, f"{e.__class__.__name__}: {e}"


def reverify(reg: dict, quiet=False) -> int:
    """Boards found before verification existed get checked once."""
    todo = [k for k, c in reg.items() if c["status"] == "watching" and not c.get("verified")]
    if not todo:
        return 0
    if not quiet:
        print(f"  Verifying {len(todo)} previously found boards belong to the right company…")
    s = radar_session()
    dropped = []

    def one(k):
        c = reg[k]
        try:
            return k, verify_board(s, c["name"], c["ats"], c["token"], company_sector(c) or "health")
        except Exception:
            return k, False

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for k, ok in ex.map(one, todo):
            c = reg[k]
            if ok:
                c["verified"] = TODAY
            else:
                c.update({"status": "mismatch", "wrong_board": f"{c['ats']}:{c['token']}",
                          "checked": TODAY})
                c.pop("ats", None); c.pop("token", None)
                dropped.append(c["name"])
                log(f"mismatch dropped  {c['name']} (was {c['wrong_board']})")
    save_json(COMPANIES, reg)
    if not quiet:
        print(f"  {len(todo) - len(dropped)} confirmed, {len(dropped)} were other companies' boards — removed")
        for n in dropped[:40]:
            print(f"    - {n}")
        print()
    return len(dropped)


def resolve(reg: dict, budget: int, quiet=False) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETRY_NONE)).strftime("%Y-%m-%d")
    # Hand-picked companies are worth a weekly second look: a wrong "no board"
    # verdict (e.g. reached during a rate-limit stretch) clears itself.
    cutoff_cur = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    todo = [k for k, c in reg.items()
            if c["status"] in ("unresolved", "error")
            or (c["status"] in ("none", "stale", "mismatch")
                and c.get("checked", "0") < (cutoff_cur if "curated" in c.get("sources", []) else cutoff))]
    # Hand-picked targets first — they are the likeliest to have a readable
    # board. Then trial sponsors, smaller before larger: a 3-trial biotech buys
    # contract help; a 400-trial pharma has an in-house department.
    # Priority: hand-picked targets; trial sponsors (smaller first — a 3-trial
    # company buys contract help, a 400-trial pharma has a department); US
    # companies with recent FDA device decisions; then everyone else.
    def _prio(k):
        c = reg[k]
        src = c.get("sources", [])
        return (0 if ("curated" in src and not c.get("checked")) else 1,
                0 if "curated" in src else 1,
                0 if c.get("trials") else 1,
                0 if ("fda" in src and c.get("country", "US") == "US") else 1,
                0 if c.get("country", "US") == "US" else 1,
                c.get("trials") or 0, k)
    todo.sort(key=_prio)
    todo = todo[:budget]
    if not todo:
        return 0
    if not quiet:
        print(f"  Resolving careers boards for {len(todo)} companies "
              f"(progress is saved as it goes)…")
    log(f"resolve start: {len(todo)} companies")
    s = radar_session()
    found = errors = done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = {ex.submit(_probe, s, reg[k]["name"], company_sector(reg[k])): k for k in todo}
        for fut in as_completed(futs):          # whoever finishes first, not in order
            k = futs[fut]
            res, err = fut.result()
            c = reg[k]
            c["checked"] = TODAY
            done += 1
            if err:
                c["status"] = "error"
                errors += 1
                if "rate-limited" not in err:
                    log(f"probe error  {c['name']}: {err}")
            elif res and "mismatch" in res:
                c["status"] = "mismatch"
                c["wrong_board"] = f"{res['mismatch']['ats']}:{res['mismatch']['token']}"
                log(f"mismatch  {c['name']} (board '{res['mismatch']['token']}' belongs to someone else)")
            elif res:
                c.update({"status": "watching", "ats": res["ats"], "token": res["token"],
                          "resolved": TODAY, "verified": TODAY, "fails": 0})
                found += 1
                if not quiet:
                    print(f"    + {c['name'][:34]:34} {res['ats']:15} {res['open']} open")
            else:
                c["status"] = "none"
            if done % 25 == 0:
                save_json(COMPANIES, reg)
                if not quiet:
                    print(f"  … {done}/{len(todo)} checked · {found} boards found")
    save_json(COMPANIES, reg)
    log(f"resolve done: {done} checked, {found} found, {errors} errors")
    if not quiet:
        print(f"  {found} of {len(todo)} resolved"
              + (f", {errors} errors (see radar_log.txt)" if errors else "")
              + "; the rest post on custom or unlisted sites\n")
    return found


# ═══════════════════════════════════════════════════════════════ 3. SWEEP ════

# ── Workday: the big companies ───────────────────────────────────────────────
# Most large device and pharma companies post on Workday, whose addresses can't
# be guessed from a name. These are candidate addresses; each is tested on your
# machine and kept only if it answers with open jobs. A wrong guess costs one
# request and is discarded — nothing unverified ever reaches the radar.
WORKDAY_CANDIDATES = {
    # medical device & surgical
    "Medtronic": [("medtronic", 1, "MedtronicCareers")],
    "Stryker": [("stryker", 1, "StrykerCareers")],
    "Abbott": [("abbott", 5, "abbottcareers")],
    "Boston Scientific": [("bostonscientific", 1, "BSCCareers")],
    "Becton Dickinson": [("bdx", 1, "EXTERNAL_CAREER_SITE_USA")],
    "Edwards Lifesciences": [("edwards", 5, "EdwardsCareers")],
    "Intuitive Surgical": [("intuitive", 1, "intuitive")],
    "Zimmer Biomet": [("zimmerbiomet", 1, "External")],
    "Baxter": [("baxter", 1, "baxter")],
    "Hologic": [("hologic", 1, "Hologic")],
    "Teleflex": [("teleflex", 1, "teleflex")],
    "Philips": [("philips", 3, "jobs-and-careers")],
    "GE HealthCare": [("gehc", 5, "GEHC_ExternalSite")],
    "Smith+Nephew": [("smithnephew", 5, "External")],
    "ResMed": [("resmed", 3, "ResMed_External_Careers")],
    "Dexcom": [("dexcom", 1, "Dexcom")],
    "Insulet": [("insulet", 5, "insuletcareers")],
    "Integra LifeSciences": [("integralife", 1, "Integra")],
    "Globus Medical": [("globusmedical", 1, "Globus")],
    "Masimo": [("masimo", 1, "Masimo")],
    "Penumbra": [("penumbrainc", 5, "Penumbra")],
    "Steris": [("steris", 1, "Steris")],
    "Haemonetics": [("haemonetics", 1, "Haemonetics")],
    "ICU Medical": [("icumed", 1, "ICUMedical")],
    "Conmed": [("conmed", 1, "Conmed")],
    "Merit Medical": [("merit", 1, "MeritMedical")],
    "Enovis": [("enovis", 1, "Enovis")],
    "Embecta": [("embecta", 1, "Embecta")],
    "LivaNova": [("livanova", 3, "LivaNova")],
    "Alcon": [("alcon", 1, "careers_alcon")],
    "Bausch + Lomb": [("bauschhealth", 1, "Bausch_Lomb")],
    # diagnostics & tools
    "Illumina": [("illumina", 1, "illumina-careers")],
    "Exact Sciences": [("exactsciences", 1, "Exact_Sciences")],
    "Thermo Fisher Scientific": [("thermofisher", 5, "ThermoFisherCareers")],
    "Danaher": [("danaher", 1, "DanaherJobs")],
    "Quest Diagnostics": [("questdiagnostics", 1, "External")],
    "Bio-Rad Laboratories": [("biorad", 1, "BioRadCareers")],
    "Agilent Technologies": [("agilent", 5, "Agilent_Careers")],
    "Revvity": [("revvity", 1, "Revvity")],
    "QuidelOrtho": [("quidelortho", 1, "QuidelOrtho")],
    # CROs
    "Labcorp": [("labcorp", 1, "External")],
    "IQVIA": [("iqvia", 1, "IQVIA")],
    "Fortrea": [("fortrea", 1, "Fortrea")],
    "ICON plc": [("icon", 3, "broadbean_external")],
    "Parexel": [("parexel", 1, "Parexel_External_Careers")],
    "Eli Lilly and Company": [("lilly", 5, "LLY"), ("lilly", 1, "LLY")],
    "Ipsen": [("ipsen", 3, "IPSEN_CAREERS"), ("ipsen", 103, "ipsen")],
    "BioCryst Pharmaceuticals": [("biocryst", 1, "BioCryst_Careers"), ("biocryst", 5, "External")],
    # pharma
    "Pfizer": [("pfizer", 1, "PfizerCareers")],
    "Merck": [("msd", 5, "SearchJobs")],
    "Bristol Myers Squibb": [("bristolmyerssquibb", 5, "BMS")],
    "Johnson & Johnson": [("jj", 5, "JJ")],
    "Novartis": [("novartis", 3, "Novartis_Careers")],
    "GSK": [("gsk", 5, "GSKCareers")],
    "Amgen": [("amgen", 1, "Careers")],
    "Sanofi": [("sanofi", 3, "SanofiCareers")],
    "Gilead Sciences": [("gilead", 1, "gileadcareers")],
    "AstraZeneca": [("astrazeneca", 3, "Careers")],
    "Regeneron Pharmaceuticals": [("regeneron", 1, "Careers")],
    "Moderna": [("modernatx", 1, "M_tx")],
    "Takeda": [("takeda", 3, "External")],
    "Eli Lilly": [("lilly", 5, "LLY")],
    "Vertex Pharmaceuticals": [("vrtx", 501, "Vertex_Careers")],
    "Biogen": [("biibhr", 3, "external")],
    "AbbVie": [("abbvie", 1, "abbvie")],
    "Bayer": [("bayer", 3, "Bayer")],
    "Boehringer Ingelheim": [("boehringer", 3, "BI")],
    "Astellas Pharma": [("astellas", 1, "Astellas")],
    "Daiichi Sankyo": [("daiichisankyo", 1, "Daiichi_Sankyo")],
    "Teva Pharmaceuticals": [("teva", 3, "TevaCareers")],
    "Jazz Pharmaceuticals": [("jazzpharma", 5, "JazzCareers")],
    "Alnylam Pharmaceuticals": [("alnylam", 1, "Alnylam")],
    "Incyte": [("incyte", 1, "Incyte")],
    "BioMarin": [("biomarin", 1, "BioMarin")],
    "Organon": [("organon", 5, "Organon")],
}
# Energy. Same rule: each address is tested on the machine that runs the sweep
# and kept only if it answers with open jobs.
WORKDAY_ENERGY = {
    "Chevron": [("chevron", 5, "jobs")],
    "Shell": [("shell", 3, "ShellCareers")],
    "BP": [("bpinternational", 3, "bpCareers")],
    "ConocoPhillips": [("conocophillips", 1, "External")],
    "Occidental Petroleum": [("oxy", 5, "Corporate")],
    "Phillips 66": [("phillips66", 1, "Phillips66")],
    "Marathon Petroleum": [("mpc", 1, "MPCCareers")],
    "Valero Energy": [("valero", 1, "Valero")],
    "Devon Energy": [("devonenergy", 1, "Devon")],
    "EOG Resources": [("eogresources", 1, "EOG")],
    "Baker Hughes": [("bakerhughes", 5, "BakerHughes")],
    "NOV": [("nov", 5, "NOV")],
    "Williams": [("williams", 5, "External")],
    "Kinder Morgan": [("kindermorgan", 5, "KinderMorgan")],
    "Enbridge": [("enbridge", 3, "enbridge_careers")],
    "TC Energy": [("tcenergy", 3, "CAREER_SITE_TC")],
    "ONEOK": [("oneok", 1, "ONEOK")],
    "Targa Resources": [("targaresources", 1, "Targa")],
    "Cheniere Energy": [("cheniere", 1, "Cheniere")],
    "Energy Transfer": [("energytransfer", 1, "EnergyTransfer")],
    "Enterprise Products Partners": [("enterpriseproducts", 1, "External")],
    "NextEra Energy": [("nee", 1, "NEE")],
    "Duke Energy": [("dukeenergy", 1, "search")],
    "Dominion Energy": [("dominionenergy", 1, "DominionEnergy")],
    "American Electric Power": [("aep", 1, "AEPCareerSite")],
    "Exelon": [("exeloncorp", 5, "exelon")],
    "Constellation Energy": [("constellationenergy", 1, "External")],
    "Vistra": [("vistra", 5, "Vistra_Careers")],
    "NRG Energy": [("nrg", 5, "NRG")],
    "Xcel Energy": [("xcelenergy", 1, "External")],
    "Sempra": [("sempra", 1, "Sempra")],
    "PG&E": [("pge", 1, "PGE")],
    "Edison International": [("edisonint", 1, "EIX")],
    "Entergy": [("entergy", 5, "EntergyCareers")],
    "Southern Company": [("southernco", 1, "External")],
    "DTE Energy": [("dteenergy", 1, "DTE")],
    "Consolidated Edison": [("coned", 1, "ConEd")],
    "Eversource Energy": [("eversource", 1, "External")],
    "CenterPoint Energy": [("centerpointenergy", 1, "CNP")],
    "AES": [("aes", 1, "AES_US")],
    "National Grid": [("nationalgrid", 3, "NationalGrid")],
    "Avangrid": [("avangrid", 5, "Avangrid")],
    "PJM Interconnection": [("pjm", 1, "PJM")],
    "ERCOT": [("ercot", 1, "ERCOT")],
    "MISO": [("misoenergy", 1, "MISO")],
    "GE Vernova": [("gevernova", 5, "Vernova_ExternalSite")],
    "Siemens Energy": [("siemensenergy", 3, "External")],
    "First Solar": [("firstsolar", 1, "FirstSolar")],
    "Enphase Energy": [("enphaseenergy", 5, "Enphase")],
    "Sunrun": [("sunrun", 5, "Sunrun")],
    "Nextracker": [("nextracker", 5, "Nextracker")],
    "Fluence": [("fluenceenergy", 5, "Fluence")],
    "Invenergy": [("invenergyllc", 1, "invenergycareers")],
    "Pattern Energy": [("patternenergy", 1, "Pattern")],
    "Clearway Energy Group": [("clearwayenergy", 5, "Clearway")],
    "Orsted": [("orsted", 3, "Orsted_External")],
    "EDF Renewables": [("edfrenewables", 1, "EDFR")],
    "RWE Clean Energy": [("rwe", 3, "RWE")],
    "Quanta Services": [("quantaservices", 1, "Quanta")],
    "Bloom Energy": [("bloomenergy", 1, "BloomEnergy")],
    "Itron": [("itron", 5, "Itron")],
    "Wood Mackenzie": [("woodmac", 3, "WoodMackenzie")],
    "ICF": [("icf", 5, "ICFExternal_Career_Site")],
    "Guidehouse": [("guidehouse", 1, "External")],
    "DNV": [("dnv", 3, "DNV")],
    "Black & Veatch": [("bv", 1, "BVCareers")],
    "Burns & McDonnell": [("burnsmcd", 1, "External")],
    "Calpine": [("calpine", 5, "Calpine")],
    "Talen Energy": [("talenenergy", 1, "Talen")],
    "Ameresco": [("ameresco", 1, "Ameresco")],
    "Generac": [("generac", 5, "Generac")],
    "Schneider Electric": [("schneiderelectric", 3, "External")],
    "Hitachi Energy": [("hitachi", 1, "hitachi")],
    "National Renewable Energy Laboratory": [("nrel", 5, "NREL")],
    "Pacific Northwest National Laboratory": [("pnnl", 1, "PNNL")],
    "Electric Power Research Institute": [("epri", 5, "EPRI")],
}


def _workday_variants(entries):
    """The stated address first, then the common alternatives, de-duplicated."""
    # Workday limits how fast it can be asked, so only a few guesses per company:
    # the stated address, the same site on the other common servers, then "External".
    out, seen = [], set()
    for tenant, wd, site in entries:
        for n, st in [(wd, site), (1, site), (5, site), (3, site), (wd, "External")]:
            key = (tenant, n, st)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def probe_workday(s, entries):
    """(token, open jobs) when an address answers; None when every guess said no;
    "limited" when Workday refused or timed out, so the company is tried again next run."""
    deadline = time.time() + 120               # the shared pace makes each guess slow
    limited = False
    for tenant, n, site in _workday_variants(entries):
        if time.time() > deadline:
            return "limited"
        host = f"{tenant}.wd{n}.myworkdayjobs.com"
        r = wd_request(s, "POST", f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                       json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
                       timeout=TIMEOUT)
        if r is None:
            limited = True
            continue
        if not r.ok:
            continue
        try:
            d = r.json() or {}
        except ValueError:
            continue
        if d.get("total") or d.get("jobPostings"):
            return f"{host}|{tenant}|{site}", int(d.get("total") or len(d.get("jobPostings") or []))
    return "limited" if limited else None


def discover_workday(reg: dict) -> int:
    """Test every Workday candidate; keep only addresses that answer."""
    s = radar_session()
    todo = []
    retry_after = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%d")
    for name, entries in {**WORKDAY_CANDIDATES, **WORKDAY_ENERGY, **WORKDAY_TECH}.items():
        k = company_key(name)
        c = reg.setdefault(k, {"name": name, "sources": ["curated"], "status": "unresolved",
                               "trials": 0, "ticker": "", "added": TODAY})
        if name in WORKDAY_ENERGY and "energy" not in c.setdefault("sectors", []):
            c["sectors"].append("energy")
        if name in WORKDAY_TECH and "tech" not in c.setdefault("sectors", []):
            c["sectors"].append("tech")
        if c.get("ats") == "workday" and c["status"] == "watching":
            continue                               # already confirmed on an earlier run
        if c.get("status") == "watching":
            continue                               # already read through another careers system
        if (c.get("workday_tried") or "") >= retry_after:
            continue                               # tried recently; Workday is retried monthly
        todo.append((k, name, entries))
    print(f"  Testing Workday careers sites for {len(todo)} large companies "
          f"(at most {WD_DISCOVER_MIN} minutes)…")
    found = 0
    WD_STOP[0] = time.time() + WD_DISCOVER_MIN * 60
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(probe_workday, s, e): (k, n) for k, n, e in todo}
        for fut in as_completed(futs):
            k, name = futs[fut]
            try:
                hit = fut.result()
            except Exception:
                hit = None
            c = reg[k]
            if hit == "limited":
                continue                           # not a real answer: try again next run
            if hit:
                token, total = hit
                c.update({"status": "watching", "ats": "workday", "token": token,
                          "resolved": TODAY, "verified": TODAY, "fails": 0})
                found += 1
                print(f"    + {name[:32]:32} workday  {total} open")
            elif c["status"] != "watching":
                c["workday_tried"] = TODAY
    WD_STOP[0] = float("inf")
    save_json(COMPANIES, reg)
    log(f"workday: {found} of {len(todo)} confirmed ({WD_STATS['requests']} requests, "
        f"{WD_STATS['limited']} rate-limited)")
    print(f"  {found} of {len(todo)} Workday sites confirmed. For the rest, paste the careers URL:")
    print('    python radar.py --add "Company" https://company.wd1.myworkdayjobs.com/SiteName\n')
    return found


# ── Careers pages ────────────────────────────────────────────────────────────
# Most drug, biotech and diagnostics companies aren't on the boards that can be
# guessed by name. Their own website links to the system they use (iCIMS,
# Workday, BambooHR, UKG, Oracle…), so the finder goes website -> careers page
# -> system, a slice of companies each week, and saves what it learns.
CAREERS_MIN = int(os.getenv("RADAR_CAREERS_MIN", "15"))        # minutes per weekly run
CAREERS_RETRY_DAYS = 60
CAREERS_FIRST = {"pharma", "biopharma", "biotech", "diagnostics", "cro", "healthai", "research"}


def _careers_todo(reg: dict) -> list[str]:
    retry = (datetime.now(timezone.utc) - timedelta(days=CAREERS_RETRY_DAYS)).strftime("%Y-%m-%d")
    todo = []
    for k, c in reg.items():
        if (c.get("careers_checked") or "") >= retry:
            continue
        if c.get("status") == "watching" and c.get("ats") != "page":
            continue                               # a plain careers page is looked at again
        secs = set(c.get("sectors") or [])
        if not (secs & (CAREERS_FIRST | {"device"})):
            continue
        todo.append(k)

    def prio(k):
        c = reg[k]
        secs = set(c.get("sectors") or [])
        return (0 if "healthai" in secs else 1,            # healthcare AI companies first
                0 if secs & CAREERS_FIRST else 1,          # then drugs, biotech, diagnostics
                0 if c.get("ticker") else 1,                # public companies
                0 if c.get("country", "US") == "US" else 1,
                -(c.get("trials") or 0), k)
    todo.sort(key=prio)
    return todo


RECHECK_SYSTEMS = {"phenom", "successfactors"}      # systems that became readable on 26 Nov 2026


def recheck_systems(reg: dict, verbose: bool = False) -> int:
    """Companies whose careers page runs on a system that ROTWANG has since
    learned to read: find the site and start watching it. Each company is
    tried once a month."""
    todo = [c for c in reg.values() if c.get("careers_system") in RECHECK_SYSTEMS
            and c.get("status") != "watching" and c.get("careers")
            and (c.get("rechecked") or "0000") < (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")]
    if not todo:
        return 0
    s = requests.Session()
    s.mount("https://", HTTPAdapter(pool_maxsize=20, max_retries=0))
    print(f"  Re-checking {len(todo)} careers sites on Phenom and SuccessFactors…")
    added = 0

    def one(c):
        try:
            hit = careers.find_system(s, c["careers"])
        except Exception:
            hit = None
        if not hit:
            return c, None, [], False
        roles, ok = read_board(s, {"ats": hit[0], "token": hit[1], "name": c["name"],
                                   "sectors": c.get("sectors") or []})
        return c, hit, roles, ok

    todo.sort(key=lambda c: -(c.get("trials") or 0))   # the parent company claims a shared site first
    watched = {(c.get("ats"), c.get("token")) for c in reg.values() if c.get("status") == "watching"}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for c, hit, roles, ok in ex.map(one, todo):
            c["rechecked"] = TODAY
            if hit and ok and (hit[0], hit[1]) in watched:
                c["same_site_as"] = next((o["name"] for o in reg.values() if o.get("status") == "watching"
                                          and (o.get("ats"), o.get("token")) == (hit[0], hit[1])), "")
                if verbose:
                    print(f"    = {c['name'][:34]:34} same site as {c['same_site_as']}")
                continue                           # one site, read once (UCB and UCB Pharma)
            if hit and ok:
                watched.add((hit[0], hit[1]))
                c.update({"status": "watching", "ats": hit[0], "token": hit[1], "resolved": TODAY,
                          "verified": TODAY, "fails": 0, "found_by": "careers page",
                          "careers_system": hit[0]})
                added += 1
            if verbose:
                print(f"    {'+' if hit and ok else '-'} {c['name'][:34]:34} "
                      f"{(hit[0] + ' ' + hit[1]) if hit else 'not found':60} {len(roles)} jobs")
    save_json(COMPANIES, reg)
    print(f"  {added} of {len(todo)} now read directly")
    return added


def find_careers_pages(reg: dict, limit: int = 0, verbose: bool = False) -> int:
    todo = _careers_todo(reg)
    if limit:
        todo = todo[:limit]
    if not todo:
        return 0
    stop = time.time() + CAREERS_MIN * 60
    print(f"  Looking for careers pages: {len(todo)} companies waiting, "
          f"at most {CAREERS_MIN} minutes this run…")
    s = requests.Session()
    s.mount("https://", HTTPAdapter(pool_maxsize=40, max_retries=0))
    stats = {"tried": 0, "site": 0, "page": 0, "read": 0}
    systems, unread = {}, {}

    def one(k):
        if time.time() > stop:
            return k, None, "time"
        c = reg[k]
        try:
            dom, home = (c.get("website"), None) if c.get("website") else careers.find_website(s, c["name"])
            if not dom:
                return k, None, "nosite"
            hit = careers.find_careers(s, c["name"], dom, home or "")
            if not hit:
                return k, {"website": dom}, "nopage"
            if hit.get("ats") == "workday":
                hit["ok"], hit["n"] = True, 0      # read in the sweep's Workday window
            elif hit.get("ats") and not str(hit["ats"]).startswith("unread"):
                trial = dict(c, ats=hit["ats"], token=hit["token"])
                roles, ok = read_board(s, trial)
                hit["ok"], hit["n"] = ok, len(roles)
            return k, dict(hit, website=dom), "page"
        except Exception as e:
            return k, None, f"error {e.__class__.__name__}"

    with ThreadPoolExecutor(max_workers=16) as ex:
        for k, hit, why in ex.map(one, todo):
            if why == "time":
                continue                           # next week
            c = reg[k]
            stats["tried"] += 1
            c["careers_checked"] = TODAY
            if hit and hit.get("website"):
                c["website"] = hit["website"]
                stats["site"] += 1
            if why != "page":
                if verbose:
                    print(f"    - {c['name'][:34]:34} {why}" + (f"  {hit.get('website')}" if hit else ""))
                continue
            stats["page"] += 1
            c["careers"] = hit["careers"]
            ats = hit.get("ats") or "unknown"
            if ats == "unknown":                   # no system: read the page itself
                roles, ok = careers.read_page(s, c["name"], hit["careers"])
                if ok:
                    c.update({"status": "watching", "ats": "page", "token": hit["careers"],
                              "resolved": TODAY, "verified": TODAY, "fails": 0,
                              "found_by": "careers page"})
                    stats["read"] += 1
                c["careers_system"] = "page"
                if ok:
                    systems["own page"] = systems.get("own page", 0) + 1
                else:
                    unread["own page"] = unread.get("own page", 0) + 1
                if verbose:
                    print(f"    ~ {c['name'][:34]:34} {'own page':14} {hit['careers'][:52]:52} "
                          f"{len(roles)} titles: {'; '.join(r.title for r in roles[:3])[:80]}")
                continue
            if ats.startswith("unread:"):
                name = ats.split(":")[-1]
                c["careers_system"] = name
                unread[name] = unread.get(name, 0) + 1
                if verbose:
                    print(f"    ? {c['name'][:34]:34} {name:14} {hit['careers'][:70]}")
                continue
            systems[ats] = systems.get(ats, 0) + 1
            c["careers_system"] = ats
            if hit.get("ok"):
                stats["read"] += 1
                c.update({"status": "watching", "ats": ats, "token": hit["token"], "resolved": TODAY,
                          "verified": TODAY, "fails": 0, "found_by": "careers page"})
            if verbose:
                print(f"    {'+' if hit.get('ok') else 'x'} {c['name'][:34]:34} {ats:14} {str(hit['token'])[:40]:40} "
                      f"{hit.get('n', 0)} jobs")
    save_json(COMPANIES, reg)
    left = len(_careers_todo(reg))
    top = ", ".join(f"{k} {v}" for k, v in sorted(systems.items(), key=lambda x: -x[1]))
    miss = ", ".join(f"{k} {v}" for k, v in sorted(unread.items(), key=lambda x: -x[1])[:8])
    print(f"  Careers pages: {stats['tried']} companies, {stats['site']} websites, {stats['page']} careers pages, "
          f"{stats['read']} new boards read. {left} still to visit.")
    if top:
        print(f"    Systems found: {top}")
    if miss:
        print(f"    Not readable yet: {miss}")
    log(f"careers: {stats} systems={systems} unread={unread} left={left}")
    return stats["read"]


def device_aware(prof: dict) -> dict:
    """Your lane, extended to medical devices: a 'Statistician, Clinical Evidence'
    at a device maker counts, and device-trial language raises the fit score."""
    p = {k: (list(v) if isinstance(v, list) else v) for k, v in prof.items()}
    p.setdefault("gate_ctx", [])
    p["gate_ctx"] += ["device", "surgical", "implant", "orthop", "cardiovascular", "neuro",
                      "510(k)", "pma", "ide", "mdr", "ivd", "in vitro", "medtech", "robot",
                      "imaging", "catheter", "post-market", "postmarket"]
    p.setdefault("gate", [])
    p["gate"] += ["clinical evidence", "clinical statistician", "regulatory statistician"]
    p.setdefault("strong", [])
    p["strong"] += ["medical device", "510(k)", "pma", "ide", "clinical evidence",
                    "post-market", "postmarket", "mdr", "in vitro diagnostic", "ivd",
                    "surgical", "implant", "orthopedic", "cardiovascular"]
    return p


# Technology. Each address is tested on the machine that runs the sweep; wrong ones are dropped.
WORKDAY_TECH = {
    "Nvidia": [("nvidia", 5, "NVIDIAExternalCareerSite")],
    "Intel": [("intel", 1, "External")],
    "Applied Materials": [("amat", 1, "External")],
    "Micron Technology": [("micron", 1, "External")],
    "KLA": [("kla", 1, "Search")],
    "Lam Research": [("lamresearch", 1, "LAM")],
    "Qualcomm": [("qualcomm", 5, "External")],
    "Broadcom": [("broadcom", 1, "External_Career")],
    "Analog Devices": [("analogdevices", 1, "External")],
    "Marvell": [("marvell", 1, "MarvellCareers")],
    "Keysight Technologies": [("keysight", 5, "Careers")],
    "Coherent": [("coherent", 1, "Coherent")],
    "Lumentum": [("lumentum", 5, "LITE")],
    "Corning": [("corning", 5, "Corning")],
    "Ciena": [("ciena", 5, "Careers")],
    "Viavi Solutions": [("viavisolutions", 1, "careers")],
    "IPG Photonics": [("ipgphotonics", 1, "IPG")],
    "MKS Instruments": [("mksinst", 1, "MKSCareersUniversity")],
    "Teledyne Technologies": [("teledyne", 1, "External")],
    "Cognex": [("cognex", 1, "External_Career_Site")],
    "Zebra Technologies": [("zebra", 1, "Zebra_External")],
    "Trimble": [("trimble", 1, "TrimbleCareers")],
    "onsemi": [("onsemi", 5, "ON_Semiconductor_Careers")],
    "NXP Semiconductors": [("nxp", 3, "careers")],
    "GlobalFoundries": [("globalfoundries", 1, "External")],
    "Western Digital": [("wdc", 1, "External")],
    "Synopsys": [("synopsys", 5, "External")],
    "Cadence": [("cadence", 1, "External_Careers")],
    "Entegris": [("entegris", 1, "EntegrisCareers")],
    "Teradyne": [("teradyne", 1, "Teradyne")],
    "Wolfspeed": [("wolfspeed", 5, "Wolfspeed")],
    "Skyworks": [("skyworksinc", 1, "External")],
    "Qorvo": [("qorvo", 5, "Qorvo")],
    "HP": [("hp", 5, "ExternalCareerSite")],
    "Dell Technologies": [("dell", 1, "External")],
    "Hewlett Packard Enterprise": [("hpe", 5, "Jobsathpesite")],
    "Adobe": [("adobe", 5, "external_experienced")],
    "Salesforce": [("salesforce", 12, "External_Career_Site")],
    "Workday": [("workday", 5, "Workday")],
    "Intuit": [("intuit", 1, "External")],
    "PayPal": [("paypal", 1, "jobs")],
    "eBay": [("ebay", 5, "apply")],
    "Zillow": [("zillow", 5, "Zillow_Group_External")],
    "Expedia": [("expedia", 108, "search")],
    "Capital One": [("capitalone", 12, "Capital_One")],
    "Mastercard": [("mastercard", 1, "CorporateCareers")],
    "Equifax": [("equifax", 5, "External")],
    "TransUnion": [("transunion", 5, "TransUnion")],
    "Verisk": [("verisk", 5, "Verisk")],
    "Nielsen": [("nielsen", 5, "Nielsen")],
    "Gartner": [("gartner", 5, "EXT")],
    "Motorola Solutions": [("motorolasolutions", 5, "Careers")],
    "Garmin": [("garmin", 5, "External")],
    "Honeywell": [("honeywell", 5, "Honeywell")],
    "Rockwell Automation": [("rockwellautomation", 1, "External_Rockwell_Automation")],
    "TE Connectivity": [("te", 1, "TEcareers")],
    "Jabil": [("jabil", 5, "Jabil_Careers")],
    "Flex": [("flextronics", 1, "Careers")],
    "L3Harris": [("l3harris", 1, "L3Harris")],
    "Northrop Grumman": [("ngc", 1, "Northrop_Grumman_External_Site")],
    "Boeing": [("boeing", 1, "EXTERNAL_CAREERS")],
    "RTX": [("globalhr", 5, "REC_RTX_Ext_Gateway")],
    "Booz Allen Hamilton": [("bah", 1, "BAH_Jobs")],
    "MITRE": [("mitre", 5, "MITRE")],
    "Accenture": [("accenture", 103, "AccentureCareers")],
    "Westat": [("westat", 1, "Westat")],
    "Bruker": [("bruker", 1, "Bruker")],
    "Novanta": [("novanta", 1, "Novanta")],
    "Ametek": [("ametek", 1, "AMETEK")],
    "Walmart Global Tech": [("walmart", 5, "WalmartExternal")],
    "Disney": [("disney", 5, "disneycareer")],
    "Comcast": [("comcast", 5, "Comcast_Careers")],
    "T-Mobile": [("tmobile", 1, "External")],
    "Samsung Semiconductor": [("sec", 3, "Samsung_Careers")],
    "Sony": [("sonyglobal", 1, "SonyGlobalCareers")],
    "Zoom": [("zoom", 5, "Zoom")],
    "Snowflake": [("snowflake", 1, "Snowflake")],
    "CrowdStrike": [("crowdstrike", 5, "crowdstrikecareers")],
    "Palo Alto Networks": [("paloaltonetworks", 1, "External")],
    "Cisco": [("cisco", 5, "Cisco_Careers")],
    "Arm": [("arm", 1, "External")],
    "ASML": [("asml", 3, "ASMLEXT1")],
    "Zeiss": [("zeissgroup", 3, "External")],
}


def read_workday(s, name: str, token: str, sector: str = ""):
    """Roles from one Workday site. ok is None when this run's Workday time ran out
    first: the site is read next week and gets no strike."""
    host, tenant, site = token.split("|")
    if time.time() > WD_STOP[0]:
        return [], None
    seen, out, ok = set(), [], False
    terms = ({"energy": WORKDAY_ENERGY_TERMS, "tech": WORKDAY_TECH_TERMS}.get(sector, WORKDAY_TERMS)) + (WORKDAY_WIDE_TERMS if SCOPE == "wide" else [])
    pages = [(t, off) for t in terms for off in (0, 20, 40)]
    done_terms = set()
    for term, offset in pages:
        if term in done_terms:
            continue
        if time.time() > WD_STOP[0]:
            break
        r = wd_request(s, "POST", f"https://{host}/wday/cxs/{tenant}/{site}/jobs",
                       json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term})
        if r is None or not r.ok:
            continue
        ok = True
        try:
            d = r.json() or {}
        except ValueError:
            continue
        if offset + 20 >= int(d.get("total") or 0):
            done_terms.add(term)                  # no further pages for this term
        for j in d.get("jobPostings", []) or []:
            path = j.get("externalPath", "")
            if not path or path in seen:
                continue
            seen.add(path)
            t = j.get("title", "")
            out.append(rw.Role(title=t, company=name, location=j.get("locationsText", ""),
                               contract=rw.is_contract(t), source=f"{name} · Workday",
                               url=f"https://{host}/{site}{path}",
                               remote="remote" in (j.get("locationsText", "") or "").lower()))
    if not ok and time.time() > WD_STOP[0]:
        return out, None
    return out, ok


# ── Paylocity ────────────────────────────────────────────────────────────────
# Many small US biotechs post through Paylocity, which Greenhouse/Lever probing
# never finds. A board lives at recruiting.paylocity.com/Recruiting/Jobs/All/<id>
# and carries every open job as data (window.pageData.Jobs). The id can't be
# guessed, but any single Paylocity job page links back to its board, so boards
# are learned from job links seen in the feeds — and can be added by hand.
PAYLOCITY_BOARDS = {
    "Sagimet Biosciences": "331b8214-40e0-411d-9911-6e766b6210c6",
}
# Boards confirmed by hand. Seeded every run, so a company here is always read,
# whatever an earlier automatic probe concluded.
KNOWN_BOARDS = {
    "Cytokinetics": ("greenhouse", "cytokinetics"),
    "PTC Therapeutics": ("workday", "ptcbio.wd5.myworkdayjobs.com|ptcbio|PTC_Careers"),
    "Broad Institute": ("workday", "broadinstitute.wd1.myworkdayjobs.com|broadinstitute|broad_institute"),
    **{name: ("paylocity", guid) for name, guid in PAYLOCITY_BOARDS.items()},
}
_PL_HOST = "https://recruiting.paylocity.com/Recruiting/Jobs"
_GUID = r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"


def _first(d: dict, keys, default=""):
    for k in keys:
        v = d.get(k)
        if v not in (None, "", []):
            return v
    return default


def _page_data(html: str) -> dict:
    """Pull the window.pageData = {...}; object out of a Paylocity page."""
    m = re.search(r"window\.pageData\s*=\s*", html or "")
    if not m:
        return {}
    i, depth, start = m.end(), 0, None
    for j in range(i, min(len(html), i + 5_000_000)):
        ch = html[j]
        if ch == "{":
            depth += 1
            if start is None:
                start = j
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(html[start:j + 1])
                except ValueError:
                    return {}
    return {}


def _paylocity_text(s, job_id) -> str:
    """Description of one posting (for pay and contract terms), as plain text."""
    try:
        r = s.get(f"{_PL_HOST}/Details/{job_id}", timeout=TIMEOUT,
                  headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 rotwang-radar"})
        if not r.ok:
            return ""
        d = _page_data(r.text)
        body = _first(d.get("Job", {}) if isinstance(d.get("Job"), dict) else d,
                      ["Description", "JobDescription", "description"], "")
        if not body:
            body = re.sub(r"<script.*?</script>", " ", r.text, flags=re.S | re.I)
        return rw.clean(body)[:4000]
    except Exception:
        return ""


def read_paylocity(s, name: str, guid: str):
    try:
        r = s.get(f"{_PL_HOST}/All/{guid}", timeout=(5, 25),
                  headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 rotwang-radar"})
    except Exception:
        return [], False
    if not r.ok:
        return [], False
    jobs = (_page_data(r.text) or {}).get("Jobs") or []
    out = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        title = rw.clean(str(_first(j, ["JobTitle", "Title", "title", "Name"])))
        jid = _first(j, ["JobId", "Id", "id", "JobID"])
        if not title or not jid:
            continue
        loc = j.get("LocationName") or j.get("JobLocation") or ""
        if isinstance(loc, dict):
            loc = ", ".join(str(v) for v in [loc.get("City"), loc.get("State")] if v)
        remote = bool(j.get("IsRemote")) or "remote" in f"{title} {loc}".lower()
        body = ""
        if quant_title(title) or leadership_title(title):
            body = _paylocity_text(s, jid)     # only fetch descriptions worth reading
        out.append(rw.Role(title=title, company=name, location=str(loc), remote=remote,
                           contract=rw.is_contract(title, str(_first(j, ["EmploymentType", "JobType"])), body),
                           pay=rw.extract_pay(body) if hasattr(rw, "extract_pay") else "",
                           source=f"{name} · Paylocity", url=f"{_PL_HOST}/Details/{jid}",
                           posted=str(_first(j, ["PublishedDate", "PostedDate", "CreatedDate"]))[:10],
                           body=body))
    return out, True


def learn_paylocity(reg: dict, urls) -> int:
    """Turn any Paylocity job links seen in the feeds into watched boards."""
    s, n = radar_session(), 0
    for u in {u for u in urls if u and "recruiting.paylocity.com" in u.lower()}:
        m = re.search(r"/Jobs/All/(" + _GUID + ")", u)
        guid, name = (m.group(1) if m else ""), ""
        try:
            if not guid:
                jid = re.search(r"/Details/(\d+)", u)
                if not jid:
                    continue
                r = s.get(f"{_PL_HOST}/Details/{jid.group(1)}", timeout=TIMEOUT,
                          headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0 rotwang-radar"})
                g = re.search(r"/Jobs/All/(" + _GUID + ")", r.text or "", re.I)
                t = re.search(r"<title>\s*(.*?)\s+-\s+", r.text or "", re.S)
                if not g:
                    continue
                guid, name = g.group(1), display_name(t.group(1)) if t else ""
        except Exception:
            continue
        if not name:
            continue
        k = company_key(name)
        c = reg.setdefault(k, {"name": name, "sources": ["feed"], "trials": 0, "ticker": "", "added": TODAY})
        if c.get("ats") == "paylocity" and c.get("status") == "watching":
            continue
        c.update({"status": "watching", "ats": "paylocity", "token": guid, "resolved": TODAY,
                  "verified": TODAY, "fails": 0})
        n += 1
        log(f"paylocity: learned board for {name}")
    return n


def read_board(s, c: dict):
    if c["ats"] in careers.READERS:
        return careers.READERS[c["ats"]](s, c["name"], c["token"])
    if c["ats"] == "workday":
        return read_workday(s, c["name"], c["token"], company_sector(c))
    if c["ats"] == "paylocity":
        return read_paylocity(s, c["name"], c["token"])
    _, roles, ok = rw.task_board(s, (c["ats"], c["name"], c["token"]), True)
    return roles, ok


# If most boards fail at once, the problem is this computer's connection, not
# the boards: on 6 Nov 2026 all 531 failed in 15 seconds, straight after a
# 50-minute board search had used up Windows' network connections. A sweep
# like that is retried after a pause, never penalises the boards, and is
# reported as a failure — never as "no new roles".
MASS_FAIL_SHARE = 0.4
MASS_FAIL_WAIT  = 150          # seconds before the retry
SWEEP_HEALTH = {"total": 0, "failed": 0, "mass": False}


def _read_many(keys, reg):
    s = radar_session()

    def one(k):
        try:
            return k, read_board(s, reg[k])
        except Exception as e:                  # one bad board never stops the sweep
            log(f"sweep error  {reg[k]['name']}: {e.__class__.__name__}: {e}")
            return k, ([], False)

    out = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for fut in as_completed([ex.submit(one, k) for k in keys]):
            k, res = fut.result()
            out[k] = res
    return out


def keep_role(r, prof: dict, company: str, ck: str, agency: bool = False,
              sector: str = "", direct: bool = False):
    """Scope and scoring for one role from any source. Returns the radar row or
    None. In quant scope a contract role outside your lane must have a
    quantitative title; in wide scope any professional contract role passes."""
    if _NEVER.search(r.title or ""):
        return None                               # internships, co-ops: never
    if _ENGINE_SRC.search(r.source or "") and (_AI_GIG.search(company or "") or _ENGINE_JUNK.search(r.title or "")):
        return None                               # job-engine noise: AI-training gigs, junior and teaching posts
    lane = rw.gate(r, prof)
    soft_index = bool(re.search(r"Adzuna|Careerjet", r.source or "")) and not agency   # an employer's own listing on an index
    if not r.contract and (_employer_contract(r.title, "") if soft_index else rw.is_contract(r.title)):
        r.contract = True
    if r.contract and _CONTRACT_NOUN.search(r.title or "") and not _ENGAGEMENT.search(r.title or ""):
        r.contract = False                        # a pricing/contracting job, not a contract
    if not lane and leadership_title(r.title):
        lane = True                                # leadership in your discipline is your lane
    if not lane:
        if SCOPE == "narrow":
            return None
        if SCOPE == "quant":
            # Candidates only need to LOOK quantitative here; whether they are
            # contract, in health, or what they pay is decided by reading the
            # description (classify_rows). Without a key, the old rule applies.
            if not quant_title(r.title, sector == "energy"):
                return None
            if not _api_key() and not (r.contract and wide_contract(r)):
                return None
            if not _api_key() and _SOFT_ONLY(r.title):
                return None                        # "Consultant" as a permanent job title
        elif not r.contract or not wide_contract(r):
            return None
    r = rw.finish(r, prof)
    if not lane:
        r.fit = max(5, r.fit - (8 if SCOPE == "quant" else 15))
    if agency:
        r.fit = max(5, r.fit - 4)                 # a middleman takes a cut
    sector_known = bool(sector)
    if not sector:                                # recruiters and index-only names: go by the text
        text = f"{r.title} {r.body or ''}"
        if _HEALTH_TITLE.search(r.title or "") or _life_science(text):
            sector = "health"
        elif _energy_work(text):
            sector = "energy"
        else:
            sector = "tech"
    if sector == "tech" and not strict_title(r.title):
        return None                               # technology boards are mostly engineering: title must be your discipline
    if sector == "energy" and _ENERGY_NOT.search(r.title or "") and not leadership_title(r.title):
        return None                               # engineering and field work, not analytics
    return {"sector": sector, "direct": direct, "source": (r.source or "").split("·")[-1].strip(),
            "key": r.dkey, "title": r.title, "url": r.url, "contract": r.contract,
            "pay": r.pay, "hourly": hourly_rate(r.pay), "location": r.location,
            "remote": r.remote, "fit": r.fit, "posted": r.posted, "lane": bool(lane),
            "us": bool(r.remote) or us_or_remote(r.location),
            "agency": agency, "company": company, "ck": ck, "sk": sector_known,
            "body": (r.body or "")[:1800]}


# ── the classifier: Claude reads each job ────────────────────────────────────
_CLASS_SYSTEM = """You screen job postings for a PhD statistician / biostatistician who wants CONTRACT or
HOURLY work in three industries: health & life sciences, energy, and technology.
For each posting, read the title AND the description and return one JSON object per posting:
{"id": <id>,
 "lane": true|false,      // true ONLY if the work itself is biostatistics, statistics, statistical or
                          // clinical programming (SAS/R/Python), data science, machine learning / AI
                          // engineering or programming, bioinformatics / computational biology,
                          // epidemiology / real-world-evidence analytics, or quantitative modeling.
                          // AI work counts in every industry: ML / AI engineers and scientists, applied
                          // or research scientists in AI, LLM / generative AI / NLP / computer vision
                          // engineering, MLOps for models, AI data scientists, AI architects and
                          // hands-on AI consultants. NOT AI work: annotation, labelling, "AI trainer",
                          // model rating or evaluation gigs, AI sales, AI product or program management.
                          // In energy that means: statistics, data science, ML / AI, forecasting (load,
                          // price, generation, weather), risk / reliability / asset-failure modeling,
                          // optimization / operations research, trading or market quantitative analysis,
                          // program impact evaluation (EM&V), econometrics.
                          // false for clinical operations, CRA, medical writing, regulatory affairs,
                          // translation, sales, QA/validation, lab bench work, IT support, data entry,
                          // and in energy: petroleum / reservoir / electrical / mechanical / civil
                          // engineering, field or plant operations, construction, land, permitting,
                          // project management, SCADA / controls, accounting, generic BI reporting.
 "sector": "health"|"energy"|"tech"|"other",
                          // tech: optics / photonics / lasers / imaging, semiconductors and chip equipment,
                          // electronics and hardware, instruments, software / internet / AI companies,
                          // telecom, aerospace and defense technology, robotics, and the analytics
                          // consultancies and staffing firms that place contractors with them.
                          // At tech employers "lane" means: statistics, data science, experimentation /
                          // A-B testing / causal inference, applied or research science that is modeling,
                          // forecasting, yield / reliability / quality statistics (DOE, SPC), statistical
                          // image or signal algorithms, econometrics, survey statistics, and all
                          // AI / machine-learning engineering and research (see above).
                          // NOT lane at tech employers: general software, web, data-platform or cloud
                          // engineering not centred on ML / AI models, IT, product or program
                          // management, sales, annotation or "AI trainer" gigs.
                          // health: employer or client is biotech, pharma, diagnostics, medical devices,
                          // a CRO, a hospital/health system, a payer, or health / life-science AI.
                          // energy: oil & gas, pipelines / midstream, electric or gas utilities, grid
                          // operators, solar / wind / storage developers or manufacturers, nuclear,
                          // energy software / analytics, energy trading, energy consulting or research.
 "engagement": "contract"|"contract_to_hire"|"freelance"|"hourly"|"part_time_contract"|"permanent"|"unclear",
                          // contract = fixed-term, contractor, W2/1099/C2C, consultant engagement, temp.
                          // "Consultant" as a permanent job title at a consulting firm is "permanent".
 "hourly_low": number|null, "hourly_high": number|null,
                          // US$ per hour ONLY if pay is stated; convert an annual salary by /2080.
                          // null if no pay is stated. Never estimate.
 "us_or_remote": true|false,
 "seniority": "individual"|"lead"|"associate_director"|"director"|"senior_director"|"vp_head",
 "convertible": true|false,
                          // true when lane is true AND any of: the role is director level or above
                          // in biostatistics / biometrics / statistical programming / data science /
                          // analytics / forecasting / quantitative research / machine learning / AI;
                          // OR it is the lead or sole statistician for a company's trials (e.g.
                          // reports to the CMO, owns SAPs and submissions); OR stated pay >= $200,000
                          // a year or >= $96/hr. These are needs a senior consultant can propose to
                          // fill on a fractional or contract basis even when posted as permanent.
 "why": "<= 12 words"}
Return ONLY a JSON array, no prose."""


def _api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "")


def _class_key(row: dict) -> str:
    return hashlib.sha1(f"v4|{row['key']}|{row['title']}".encode("utf-8")).hexdigest()[:16]


class _Busy(Exception):
    """Claude asked us to slow down (per-minute limit) or was briefly overloaded."""


CLASS_MINUTES = int(os.getenv("RADAR_CLASS_MIN", "60"))   # reading time per run; the rest waits a week
CLASS_STATS: dict = {}


def _ask_claude(batch: list[dict]) -> list[dict]:
    items = [{"id": i, "title": r["title"], "company": r["company"], "location": r["location"],
              "pay_field": r.get("pay") or "", "description": (r.get("body") or "")[:1500]}
             for i, r in enumerate(batch)]
    resp = requests.post("https://api.anthropic.com/v1/messages", timeout=(10, 120), json={
        "model": CLASSIFY_MODEL, "max_tokens": 8000, "system": _CLASS_SYSTEM,
        "messages": [{"role": "user", "content": json.dumps(items, ensure_ascii=False)}]},
        headers={"x-api-key": _api_key(), "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    if resp.status_code in (429, 529) or resp.status_code >= 500:
        raise _Busy(resp.headers.get("retry-after") or "")
    if not resp.ok:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    text = "".join(b.get("text", "") for b in data.get("content", []))
    CLASS_STATS["stop:" + str(data.get("stop_reason"))] = CLASS_STATS.get("stop:" + str(data.get("stop_reason")), 0) + 1
    a, b = text.find("["), text.rfind("]")
    try:
        return json.loads(text[a:b + 1]) if a >= 0 and b > a else []
    except ValueError:
        pass
    # A cut-off or untidy answer: keep every complete verdict in it
    out, dec, i = [], json.JSONDecoder(), text.find("{")
    while 0 <= i < len(text):
        try:
            obj, end = dec.raw_decode(text, i)
            if isinstance(obj, dict) and "id" in obj:
                out.append(obj)
            i = text.find("{", end)
        except ValueError:
            i = text.find("{", i + 1)
    CLASS_STATS["salvaged"] = CLASS_STATS.get("salvaged", 0) + 1
    return out


def classify_rows(rows: list[dict]) -> int:
    """Attach Claude's reading ("cls") to each row. Each job is judged once;
    the verdict is cached in radar_class.json, so later runs only pay for new
    postings. Any failure leaves rows unjudged and the keyword rules decide."""
    if not _api_key() or not rows:
        return 0
    cache = load_json(CLASS_CACHE, {})
    todo, seen = [], set()
    for r in rows:
        ck = _class_key(r)
        if ck in cache:
            r["cls"] = cache[ck]
        elif len(r.get("body") or "") < 120:
            continue                               # nothing to read beyond the title: keyword rules decide
        elif ck not in seen:
            seen.add(ck); todo.append(r)
    if todo:
        print(f"  Reading {len(todo)} new job descriptions with Claude…")
    done, waits, fails, last_err = 0, 0, 0, ""
    stop = time.time() + CLASS_MINUTES * 60
    for i in range(0, len(todo), CLASS_BATCH):
        batch = todo[i:i + CLASS_BATCH]
        out = None
        for attempt in range(6):
            if time.time() > stop:
                break
            try:
                out = _ask_claude(batch)
                break
            except _Busy as e:
                waits += 1
                try:
                    pause = min(90, max(5, float(str(e) or 0)))
                except ValueError:
                    pause = 20
                time.sleep(pause if str(e) else 15 * (attempt + 1))
            except Exception as e:
                out = e
                break
        if time.time() > stop and out is None:
            print(f"  Claude reading stopped for time: {len(todo) - i} descriptions wait for the next run")
            break
        if out is None:
            log(f"classify: batch still busy after retries — keyword rules used for {len(batch)} roles")
            continue
        if isinstance(out, Exception):
            e = out
            fails += 1
            last_err = f"{e.__class__.__name__}: {str(e)[:120]}"
            log(f"classify: batch failed ({e.__class__.__name__}: {e}) — keyword rules used for {len(batch)} roles")
            if "401" in str(e) or "403" in str(e):
                print("  Claude rejected the API key — keyword rules used this run.")
                break
            continue
        for v in out:
            try:
                r = batch[int(v.get("id"))]
            except (TypeError, ValueError, IndexError):
                continue
            v.pop("id", None)
            cache[_class_key(r)] = v
            done += 1
        if (i // CLASS_BATCH) % 5 == 4:
            save_json(CLASS_CACHE, cache)
    save_json(CLASS_CACHE, cache)
    for r in rows:
        r.setdefault("cls", cache.get(_class_key(r)))
    print(f"  Claude read {done} of {len(todo)} new descriptions"
          + (f" (waited {waits} times for its per-minute limit)" if waits else "")
          + (f"; {fails} batches failed: {last_err}" if fails else "") + f" {CLASS_STATS}")
    log(f"classify: {done} newly judged, {len(rows) - len(todo)} from cache")
    return done


def apply_verdicts(rows: list[dict]) -> list[dict]:
    """Turn Claude's verdict into what the radar shows. Shown: your field, in
    health, and contract or hourly. Permanent roles only with --perm."""
    out = []
    for r in rows:
        v = r.get("cls")
        if v:
            r["lane"] = bool(v.get("lane"))
            sec = v.get("sector") or ("health" if v.get("health") else "other")
            r["health"] = sec in ("health", "energy", "tech")   # "in one of your industries"
            if sec in ("health", "energy", "tech"):
                r["sector"] = sec
            r["contract"] = (v.get("engagement") or "") in CONTRACT_KINDS
            r["engagement"] = v.get("engagement") or ""
            lo, hi = v.get("hourly_low"), v.get("hourly_high")
            if isinstance(lo, (int, float)) or isinstance(hi, (int, float)):
                h = round(((lo or hi) + (hi or lo)) / 2)
                r["hourly"] = h if 0 < h <= 400 else r.get("hourly")
            if "us_or_remote" in v:
                r["us"] = bool(v["us_or_remote"])
            r["why"] = v.get("why", "")
            r["seniority"] = v.get("seniority", "")
            if leadership_title(r["title"]) and _CORE_LEAD.search(r["title"] or ""):
                r["lane"] = True                   # never let a reading drop a statistics leadership title
            if not (r["lane"] and r["health"]):
                continue
            conv = v.get("convertible")
        elif not r.get("lane") and SCOPE == "quant" and _api_key():
            continue                               # unjudged and not plainly in your lane
        else:
            conv = None
            # Nobody read this posting: the title has to say your discipline,
            # and a company of unknown industry has to show it in the text.
            if not strict_title(r["title"]):
                continue
        if conv is None:                           # old cached verdicts, or no key: keyword rule
            pay = r.get("pay") or ""
            # High pay alone never qualifies: the title must be the discipline.
            conv = leadership_title(r["title"]) or (
                bool(_LEAD_FIELD.search(r["title"] or "")) and bool(pay) and "est." not in pay
                and rw.pay_value(pay) >= LEAD_PAY_ANNUAL)
        r["convertible"] = bool(conv) and not r.get("contract")
        if r.get("sector") == "tech" and r["convertible"] and not _TECH_PITCH.search(r["title"] or ""):
            r["convertible"] = False               # technology has thousands of senior ML titles: keep
                                                   # only heads of statistics / data science as pitches
        if r.get("contract") or r["convertible"] or SHOW_PERM:
            out.append(r)
        else:
            r["_perm"] = True
    return out


def contract_sweep(reg: dict, prof: dict) -> list[dict]:
    """Adzuna, contract listings only, for your roles by name — where staffing
    firms and CROs post hourly work. One request per query."""
    app_id, app_key = "", ""
    try:
        app_id, app_key = rw.adzuna_keys()
    except Exception:
        pass
    if not app_id:
        return []
    s = radar_session()
    out = []
    plan = ([(q, True, "") for q in CONTRACT_QUERIES] + [(q, False, "") for q in LEADERSHIP_QUERIES]
            + [(q, True, "energy") for q in ENERGY_CONTRACT_QUERIES]
            + [(q, False, "energy") for q in ENERGY_LEADERSHIP_QUERIES]
            + [(q, True, "tech") for q in TECH_CONTRACT_QUERIES]
            + [(q, False, "tech") for q in TECH_LEADERSHIP_QUERIES])
    for q, contract_only, hint in plan:
        params = {"app_id": app_id, "app_key": app_key, "results_per_page": 50,
                  "what": q, "max_days_old": 30, "content-type": "application/json"}
        if contract_only:
            params["contract"] = 1
        try:
            r = s.get("https://api.adzuna.com/v1/api/jobs/us/search/1", params=params, timeout=(5, 20))
            if r.status_code == 429:
                log("contract sweep: Adzuna rate limit reached — stopping for this run"); break
            res = (r.json() or {}).get("results", []) if r.ok else []
        except Exception:
            continue
        for j in res:
            title = rw.clean(j.get("title", ""))
            comp = ((j.get("company") or {}).get("display_name") or "").strip()
            if not title or not comp:
                continue
            lo, hi = j.get("salary_min"), j.get("salary_max")
            pay = ""
            if lo or hi:
                pay = f"${int(lo or hi):,}–${int(hi or lo):,}"
                if str(j.get("salary_is_predicted", "0")) == "1":
                    pay += " est."
            body = rw.clean(j.get("description", ""))[:1500]
            if not contract_only and not leadership_title(title):
                continue                              # leadership queries keep leadership titles only
            agency = rw.is_agency(comp) or company_key(comp) in {company_key(n) for n in _feed_firms()}
            # Adzuna labels nearly everything "contract" (even pharma directors).
            # Trust the label for staffing firms; employers need the word in the text.
            role = rw.Role(title=title, company=comp, location=(j.get("location") or {}).get("display_name", ""),
                           pay=pay, contract=(rw.is_contract(title, "contract" if contract_only else "", body)
                                              if agency else _employer_contract(title, body)),
                           remote=bool(re.search(r"remote|anywhere", f"{title} {body[:400]}", re.I)),
                           source=f"{comp} · Adzuna", url=j.get("redirect_url", ""),
                           posted=(j.get("created") or "")[:10], body=body)
            ck = _feed_company(reg, comp, agency)
            sector = company_sector(reg.get(ck)) or \
                ("health" if _HEALTH_TITLE.search(title) else "energy" if _ENERGY_TITLE.search(title) else hint)
            row = keep_role(role, prof, comp, ck, agency, sector=sector)
            if row:
                out.append(row)
        time.sleep(0.3)
    print(f"  Index sweep: {len(out)} candidates from {len(plan)} searches "
          f"(contract and leadership, life sciences and energy)")
    return out


# ── the recruiter feed ───────────────────────────────────────────────────────
FEED_STATE = HERE / "radar_feed_state.json"


def _feed_firms() -> list[str]:
    d = load_json(HERE / "rotwang_targets.json", {})
    g = d.get("targets", {}) if isinstance(d, dict) else {}
    seen, out = set(), []
    for n in (g.get("recruiter", []) + g.get("staffing_medtech", []) + g.get("device_cro", [])
              + g.get("energy_staffing", []) + g.get("tech_staffing", [])):
        k = company_key(n)
        if k and k not in seen:
            seen.add(k); out.append(n)
    return out


def _feed_company(reg: dict, name: str, agency: bool) -> str:
    k = company_key(name)
    c = reg.get(k)
    if c is None:
        reg[k] = {"name": name, "sources": ["feed"], "status": "feed", "trials": 0,
                  "ticker": "", "added": TODAY, "sectors": ["agency"] if agency else []}
    elif c["status"] not in ("watching",):
        c.setdefault("sources", [])
        if "feed" not in c["sources"]:
            c["sources"].append("feed")
        if c["status"] in ("none", "mismatch", "unresolved", "error"):
            c["status"] = "feed"                  # readable through the indexes
    return k


def agency_feed(reg: dict, prof: dict) -> list[dict]:
    """Roles from the indexes for the recruiting and consulting firms, plus every
    quantitative remote role Remotive carries. One Remotive request; one Adzuna
    request per firm, rotating through the list a slice per run."""
    firms = _feed_firms()
    firm_keys = {company_key(n): n for n in firms}
    s = radar_session()
    out, n_rem, n_adz = [], 0, 0

    # Remotive: the whole remote-jobs list in one call, filtered here.
    try:
        r = s.get("https://remotive.com/api/remote-jobs", timeout=(5, 30))
        jobs = (r.json() or {}).get("jobs", []) if r.ok else []
    except Exception:
        jobs = []
    for j in jobs:
        title = rw.clean(j.get("title", ""))
        comp = (j.get("company_name") or "").strip()
        if not title or not comp:
            continue
        agency = company_key(comp) in firm_keys
        if not (quant_title(title) or agency):
            continue
        body = rw.clean(j.get("description", ""))[:1500]
        role = rw.Role(title=title, company=comp, location=j.get("candidate_required_location", "") or "Remote",
                       pay=j.get("salary", "") or "", contract=rw.is_contract(title, j.get("job_type", ""), body),
                       remote=True, source=f"{comp} · Remotive", url=j.get("url", ""),
                       posted=(j.get("publication_date") or "")[:10], body=body)
        ck = _feed_company(reg, comp, agency)
        row = keep_role(role, prof, comp, ck, agency, sector=company_sector(reg.get(ck)))
        if row:
            out.append(row); n_rem += 1

    # Adzuna: one query per firm, by company name, rotating through the list.
    app_id, app_key = "", ""
    try:
        app_id, app_key = rw.adzuna_keys()
    except Exception:
        pass
    if app_id and firms:
        state = load_json(FEED_STATE, {})
        order = sorted(firms, key=lambda n: state.get(n, "0000-00-00"))
        batch = order[:AGENCY_FEED_PER_RUN]
        for name in batch:
            state[name] = TODAY
            try:
                r = s.get("https://api.adzuna.com/v1/api/jobs/us/search/1",
                          params={"app_id": app_id, "app_key": app_key, "results_per_page": 50,
                                  "company": name, "max_days_old": 45, "content-type": "application/json"},
                          timeout=(5, 20))
                if r.status_code == 429:
                    log("agency feed: Adzuna rate limit reached — stopping the feed for this run")
                    break
                res = (r.json() or {}).get("results", []) if r.ok else []
            except Exception:
                continue
            for j in res:
                title = rw.clean(j.get("title", ""))
                comp = ((j.get("company") or {}).get("display_name") or name).strip()
                if not title or not (quant_title(title) or rw.gate(rw.Role(title=title), prof)):
                    continue
                lo, hi = j.get("salary_min"), j.get("salary_max")
                pay = ""
                if lo or hi:
                    pay = f"${int(lo or hi):,}–${int(hi or lo):,}"
                    if str(j.get("salary_is_predicted", "0")) == "1":
                        pay += " est."
                body = rw.clean(j.get("description", ""))[:1500]
                role = rw.Role(title=title, company=comp, location=(j.get("location") or {}).get("display_name", ""),
                               pay=pay, contract=(j.get("contract_type") == "contract") or rw.is_contract(title, "", body),
                               remote=bool(re.search(r"remote|anywhere", f"{title} {body[:400]}", re.I)),
                               source=f"{comp} · Adzuna", url=j.get("redirect_url", ""),
                               posted=(j.get("created") or "")[:10], body=body)
                ck = _feed_company(reg, comp, True)
                row = keep_role(role, prof, comp, ck, True, sector=company_sector(reg.get(ck)))
                if row:
                    out.append(row); n_adz += 1
            time.sleep(0.3)
        save_json(FEED_STATE, state)
    print(f"  Recruiter feed: {n_rem} from Remotive, {n_adz} from Adzuna"
          + ("" if app_id else " (no Adzuna key — Remotive only)"))
    return out


# ── Job search engines: where headhunters and staffing firms post ─────────────
# Contract work is mostly posted by recruiters, on LinkedIn, Indeed, Dice,
# ZipRecruiter and their own sites. Google Jobs gathers all of those.
# These searches name the contract roles directly.
SEARCH_QUERIES = [
    # (query, industry hint, pages)
    ("contract biostatistician", "health", 2),
    ("biostatistician contractor remote", "health", 2),
    ("contract statistical programmer clinical", "health", 2),
    ("SAS programmer clinical trials contract", "health", 1),
    ("contract statistician", "", 1),
    ("AI engineer contract healthcare", "health", 1),
    ("machine learning engineer contract", "tech", 1),
    ("AI data scientist contract", "", 1),
    ("generative AI LLM engineer contract", "tech", 1),
    ("principal biostatistician consultant", "health", 1),
    ("real world evidence data scientist contract", "health", 1),
    ("epidemiologist contract", "health", 1),
    ("contract data scientist pharmaceutical", "health", 1),
    ("biometrics consultant contract", "health", 1),
    ("R programmer clinical contract", "health", 1),
    ("statistical consultant clinical trials", "health", 1),
    ("bioinformatics scientist contract", "health", 1),
    ("computational biologist contract", "health", 1),
    ("machine learning scientist biotech contract", "health", 1),
    ("freelance biostatistician", "health", 1),
    ("medical device statistician contract", "health", 1),
    ("health economics outcomes research contract", "health", 1),
    ("contract data scientist energy", "energy", 1),
    ("load forecasting analyst contract", "energy", 1),
    ("quantitative analyst power markets contract", "energy", 1),
    ("contract data scientist", "tech", 2),
    ("applied scientist contract", "tech", 1),
    ("data scientist experimentation contract", "tech", 1),
    ("contract machine learning scientist", "tech", 1),
    ("data scientist semiconductor contract", "tech", 1),
    ("statistician contractor", "tech", 1),
]
# Headhunters and specialist firms searched by name: the firms that place
# biostatisticians and statistical programmers. Google Jobs gets the first
# FIRMS_ON_GOOGLE of them (one search each).
FIRM_SEARCHES = ["Penfield Search Partners", "The Lotus Group", "Orbis Clinical", "ClinChoice",
                 "Smith Hanley Associates", "Planet Pharma", "Proclinical", "Barrington James",
                 "Hobson Prior", "Advanced Clinical", "Katalyst Healthcares", "Real Life Sciences",
                 "EPM Scientific", "Sterling Life Sciences", "Klein Hersh", "Joule", "Kelly Science",
                 "Randstad Life Sciences", "Spectraforce", "Russell Tobin", "Harnham", "Burtch Works",
                 "Cytel", "Veristat", "Statistics & Data Corporation", "Medpace", "Precision for Medicine",
                 "Catalyst Clinical Research", "Peoplelink", "Synectics", "Integrated Resources",
                 "Insight Global", "TEKsystems", "Actalent", "Kforce", "Apex Systems", "Yoh"]
FIRMS_ON_GOOGLE = 8
FIRMS_TWO_PAGES = 3          # Penfield, Lotus and Orbis get a second page
# AI-training marketplaces post "expert" gigs (model evaluation, labelling) under
# statistics titles. They are not consulting engagements, so they never show.
_AI_GIG = re.compile(r"mercor|outlier|superannotate|auraone|remotebridge|xperteez|weekday ai|alignerr|"
                     r"opentrain|yo ai labs|virtual vocations|"
                     r"data ?annotation|remotasks|scale ai|micro1|invisible tech|handshake ai|toloka|"
                     r"\bappen\b|telus (international|digital)|crowdgen|prolific|snorkel|"
                     r"remote quest jobs|^confidential$|^dice$|upwork", re.I)
# Engine listings carry more noise than company boards: teaching, junior
# research support and AI-benchmark gigs share words with the real roles.
_ENGINE_JUNK = re.compile(r"instructor|teacher|teaching|tutor|coordinator|research associate|"
                          r"\bassistant\b|benchmark|kaggle|\bai expert\b|evaluator|\brater\b|"
                          r"label(l)?ing|annotat|ai training|training tasks?|\bai models?\b|\btasks\b|task author|"
                          r"expert \(|\bjunior\b|\bjr\.?\b|entry[- ]level|volunteer|unpaid|new grad|"
                          r"recent grad|^university\b|\bassociate (data scientist|machine learning|ml engineer|"
                          r"statistician|bioinformatician)|professor|faculty|lecturer|tenure", re.I)
_ENGINE_SRC = re.compile(r"adzuna|google jobs|remotive|jsearch|careerjet|asa career", re.I)
# For an employer's own listing on an index, "Consultant" is often a permanent
# title (universities and hospitals: "Biostatistical Consultant II"). Only a hard
# word in the title, or the description saying so, makes it a contract.
_HARD_CONTRACT = re.compile(r"\bcontract(or|ors)?\b|freelance|fractional|temporary|\binterim\b|\b1099\b|"
                            r"\btemp\b|zero[- ]hours?|short[- ]term consultant|\be ?t consultant\b", re.I)


def _employer_contract(title: str, body: str) -> bool:
    t = rw.CONTRACT_FALSE_FRIENDS.sub(" ", title or "")
    b = rw.CONTRACT_FALSE_FRIENDS.sub(" ", body or "")
    return bool(_HARD_CONTRACT.search(t) or rw.CONTRACT_BODY_RE.search(b))
_ENERGY_TITLE = re.compile(r"energy|power (market|system|trad)|\bgrid\b|utilit|renewable|solar|wind|"
                           r"battery|oil|\bgas\b|load forecast", re.I)
SERPAPI_PER_RUN = int(os.getenv("SERPAPI_PER_RUN", "33"))   # free plan: 250 a month, room for tests
_NO_LINK = re.compile(r"jooble", re.I)      # never sent there
_JOBBOARD_SITES = re.compile(r"linkedin|indeed|ziprecruiter|glassdoor|dice|monster|careerbuilder|"
                             r"simplyhired|talent\.com|jooble|adzuna|builtin|ladders|lensa|jobright|"
                             r"bebee|learn4good|teal|whatjobs|getwork|jobgether|snagajob|upwork", re.I)


def _engine_role(reg, prof, title, comp, loc, pay, jtype, url, posted, body, remote, label, hint, firm_keys,
                 soft=False):
    """soft: the index labels nearly everything contract, so an employer's own
    listing needs the word in its title or text (staffing firms keep the label)."""
    title, comp = rw.clean(str(title or "")), str(comp or "").strip()
    if not title or not comp or not url:
        return None
    if not (quant_title(title, hint == "energy") or strict_title(title) or leadership_title(title)):
        return None
    if _JUNK.search(title) or _ENGINE_JUNK.search(title) or _AI_GIG.search(comp):
        return None
    body = rw.clean(str(body or ""))[:1800]
    agency = rw.is_agency(comp) or company_key(comp) in firm_keys
    contract = (_employer_contract(title, body) if soft and not agency
                else rw.is_contract(title, str(jtype or ""), body))
    role = rw.Role(title=title, company=comp, location=str(loc or ""), pay=str(pay or ""),
                   contract=contract, remote=bool(remote),
                   source=f"{comp} · {label}", url=str(url), posted=str(posted or ""), body=body)
    ck = _feed_company(reg, comp, agency)
    sector = company_sector(reg.get(ck)) or ""
    if not sector:                                # the title says the industry, else the search does
        sector = "health" if _HEALTH_TITLE.search(title) else \
                 "energy" if _ENERGY_TITLE.search(title) else hint
    return keep_role(role, prof, comp, ck, agency, sector=sector)


def google_jobs(reg: dict, prof: dict) -> list[dict]:
    """Google Jobs through SerpApi (key in SERPAPI_KEY): LinkedIn, Indeed, Dice,
    ZipRecruiter and staffing-firm listings in one search."""
    key = os.getenv("SERPAPI_KEY", "").strip()
    if not key:
        print("  Google Jobs: no SERPAPI_KEY set — skipped")
        return []
    # A rerun in the same week reuses the last searches: the free plan has 250 a month.
    saved = load_json(GOOGLE_SAVE, {})
    fresh = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    if os.getenv("RADAR_GOOGLE", "search") == "reuse" and saved.get("rows") and saved.get("date", "") >= fresh:
        print(f"  Google Jobs: reusing {len(saved['rows'])} listings from {saved['date']} (no searches spent)")
        return [dict(r) for r in saved["rows"]]
    s = requests.Session()                        # no automatic retries: each retry is a paid search
    s.headers.update({"User-Agent": "rotwang-radar/1.0"})
    firm_keys = {company_key(n) for n in _feed_firms()}
    out, used, seen, read, slow = [], 0, set(), 0, 0
    budget = SERPAPI_PER_RUN
    try:                                          # the account check is free
        a = s.get("https://serpapi.com/account.json", params={"api_key": key}, timeout=(10, 30)).json()
        left = a.get("total_searches_left", a.get("plan_searches_left"))
        if isinstance(left, int):
            budget = max(0, min(budget, left))
            print(f"  Google Jobs: {left} searches left this month")
    except Exception as e:
        log(f"google jobs account check: {e.__class__.__name__}: {e}")
    plan = [(f"{f} biostatistician", "health", 2 if i < FIRMS_TWO_PAGES else 1)
            for i, f in enumerate(FIRM_SEARCHES[:FIRMS_ON_GOOGLE])] + SEARCH_QUERIES
    for q, hint, pages in plan:
        token = None
        for page in range(pages):
            if used >= budget:
                break
            params = {"engine": "google_jobs", "q": q, "location": "United States", "hl": "en",
                      "gl": "us", "api_key": key}
            if token:
                params["next_page_token"] = token
            r = None
            for attempt in range(2):              # a slow search finishes on SerpApi's side and
                try:                              # is then served from its cache, free
                    r = s.get("https://serpapi.com/search.json", params=params, timeout=(10, 90))
                    break
                except Exception as e:
                    slow += 1
                    log(f"google jobs '{q}' attempt {attempt + 1}: {e.__class__.__name__}")
                    time.sleep(5)
            if r is None:
                break
            used += 1
            try:
                d = r.json() if r.ok else (r.json() if r.content else {})
            except ValueError:
                d = {}
            if not r.ok:
                log(f"google jobs '{q}': HTTP {r.status_code} {str(d.get('error') if isinstance(d, dict) else '')[:120]}")
                if r.status_code in (401, 403, 429):
                    used = budget                  # bad key or month used up: stop asking
                break
            for j in d.get("jobs_results", []) or []:
                read += 1
                de = j.get("detected_extensions") or {}
                opts = [o for o in (j.get("apply_options") or [])
                        if o.get("link") and not _NO_LINK.search(o.get("link"))]
                if not opts and _NO_LINK.search(str(j.get("via") or "")):
                    continue                       # posted only on Jooble: never shown
                # the employer's or recruiter's own page first, a job board second
                link = next((o.get("link") for o in opts if o.get("link") and not _JOBBOARD_SITES.search(o.get("link"))), None) \
                    or (opts[0].get("link") if opts else None) or j.get("share_link")
                via = re.sub(r"^via\s+", "", str(j.get("via") or "Google Jobs"))
                if _NO_LINK.search(via):
                    via = (opts[0].get("title") if opts else "") or "web"
                ident = (j.get("title"), j.get("company_name"))
                if ident in seen:
                    continue
                seen.add(ident)
                row = _engine_role(reg, prof, j.get("title"), j.get("company_name"), j.get("location"),
                                   de.get("salary") or "", de.get("schedule_type") or " ".join(j.get("extensions") or []),
                                   link, de.get("posted_at") or "", j.get("description"),
                                   bool(de.get("work_from_home")), f"Google Jobs ({via})", hint, firm_keys)
                if row:
                    out.append(row)
            token = ((d.get("serpapi_pagination") or {}).get("next_page_token"))
            if not token:
                break
            time.sleep(0.5)
    print(f"  Google Jobs: {used} searches, {read} listings read, {len(out)} candidates"
          + (f", {slow} slow answers retried" if slow else ""))
    if used:
        save_json(GOOGLE_SAVE, {"date": TODAY, "rows": out})
    return out


def _annual_pay(lo, hi, period: str) -> str:
    """Pay as the radar's parser reads it: hourly stays hourly, anything else a year."""
    try:
        lo, hi = float(lo or 0), float(hi or 0)
    except (TypeError, ValueError):
        return ""
    if not (lo or hi):
        return ""
    lo, hi = lo or hi, hi or lo
    p = (period or "").strip().upper()[:1]
    if p == "H":
        return f"${lo:,.0f}–${hi:,.0f} an hour"
    mult = {"Y": 1, "A": 1, "M": 12, "W": 52, "D": 260}.get(p)
    if not mult:
        return ""
    return f"${lo * mult:,.0f}–${hi * mult:,.0f} a year"


def _engine_budget(save: Path, per_run: int, per_month: int) -> tuple[int, dict]:
    """Searches this run may spend, keeping the month inside the free plan."""
    st = load_json(save, {})
    month = TODAY[:7]
    used = st.get("used", 0) if st.get("month") == month else 0
    return max(0, min(per_run, per_month - used)), {"month": month, "used": used}


def _reuse(save: Path, label: str):
    """Manual reruns in the same week reuse the last results: searches are limited."""
    saved = load_json(save, {})
    fresh = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")
    if os.getenv("RADAR_GOOGLE", "search") == "reuse" and saved.get("rows") and saved.get("date", "") >= fresh:
        print(f"  {label}: reusing {len(saved['rows'])} listings from {saved['date']} (no searches spent)")
        return [dict(r) for r in saved["rows"]]
    return None


JSEARCH_SAVE = HERE / "radar_jsearch.json"
JSEARCH_PER_RUN = int(os.getenv("JSEARCH_PER_RUN", "38"))       # free plan: 200 a month
JSEARCH_PER_MONTH = int(os.getenv("JSEARCH_PER_MONTH", "190"))   # room left for tests
# JSearch reads Google's job index too, so it asks what the SerpApi searches
# don't: listings by job site (Dice, LinkedIn, Indeed, ZipRecruiter), niche
# specialties, and the staffing firms after the first few.
JSEARCH_QUERIES = [
    ("contract biostatistician via dice", "health"),
    ("contract statistical programmer via dice", "health"),
    ("SAS programmer contract via dice", "health"),
    ("contract data scientist healthcare via dice", "health"),
    ("machine learning engineer contract via dice", "tech"),
    ("AI engineer contract via dice", "tech"),
    ("contract biostatistician via linkedin", "health"),
    ("contract statistical programmer via linkedin", "health"),
    ("biostatistics consultant via linkedin", "health"),
    ("contract data scientist via linkedin", "tech"),
    ("AI engineer contract via linkedin", "tech"),
    ("machine learning scientist contract via linkedin", "tech"),
    ("contract biostatistician via indeed", "health"),
    ("contract statistical programmer via indeed", "health"),
    ("contract biostatistician via ziprecruiter", "health"),
    ("contract statistician via ziprecruiter", ""),
    ("pharmacometrics consultant contract", "health"),
    ("real world evidence consultant contract", "health"),
    ("HEOR consultant contract", "health"),
    ("CDISC SDTM ADaM programmer contract", "health"),
    ("R shiny developer clinical contract", "health"),
    ("bioinformatician contract", "health"),
    ("clinical data scientist contract", "health"),
    ("healthcare AI engineer contract", "health"),
    ("LLM engineer contract", "tech"),
    ("NLP data scientist contract healthcare", "health"),
    ("data scientist energy contract", "energy"),
    ("fractional head of data science", ""),
    ("interim head of biostatistics", "health"),
]


def jsearch_jobs(reg: dict, prof: dict) -> list[dict]:
    """JSearch (OpenWeb Ninja; key in JSEARCH_API_KEY): Google's job index —
    LinkedIn, Indeed, Glassdoor, ZipRecruiter, Dice — with full descriptions."""
    key = os.getenv("JSEARCH_API_KEY", "").strip()
    if not key:
        print("  JSearch: no JSEARCH_API_KEY set — skipped")
        return []
    again = _reuse(JSEARCH_SAVE, "JSearch")
    if again is not None:
        return again
    budget, st = _engine_budget(JSEARCH_SAVE, JSEARCH_PER_RUN, JSEARCH_PER_MONTH)
    s = requests.Session()                        # no automatic retries: each one is a search
    s.headers.update({"x-api-key": key, "User-Agent": "rotwang-radar/1.0"})
    firm_keys = {company_key(n) for n in _feed_firms()}
    plan = JSEARCH_QUERIES + [(f"{f} biostatistician", "health") for f in FIRM_SEARCHES[FIRMS_ON_GOOGLE:]]
    out, used, read, seen = [], 0, 0, set()
    for q, hint in plan:
        if used >= budget:
            break
        try:
            r = s.get("https://api.openwebninja.com/jsearch/search-v2", timeout=(10, 60),
                      params={"query": q, "country": "us", "date_posted": "week"})
        except Exception as e:
            log(f"jsearch '{q}': {e.__class__.__name__}")
            continue
        used += 1
        if r.status_code in (401, 403, 429):
            log(f"jsearch: HTTP {r.status_code} — stopping for this run")
            print(f"  JSearch: HTTP {r.status_code} — key refused or month used up")
            break
        try:
            d = r.json() if r.ok else {}
        except ValueError:
            d = {}
        jobs = d.get("data") if isinstance(d, dict) else None
        if isinstance(jobs, dict):
            jobs = jobs.get("jobs") or jobs.get("data") or []
        for j in jobs or []:
            if not isinstance(j, dict):
                continue
            read += 1
            ident = (j.get("job_title"), j.get("employer_name"))
            if ident in seen:
                continue
            seen.add(ident)
            opts = [o for o in (j.get("apply_options") or []) if isinstance(o, dict) and o.get("apply_link")]
            link = next((o["apply_link"] for o in opts if not _JOBBOARD_SITES.search(o["apply_link"])), None) \
                or j.get("job_apply_link") or (opts[0]["apply_link"] if opts else None) or j.get("job_google_link")
            if link and _NO_LINK.search(link):
                continue
            loc = j.get("job_location") or ", ".join(x for x in (j.get("job_city"), j.get("job_state")) if x)
            jtype = " ".join(str(x) for x in ([j.get("job_employment_type")] + list(j.get("job_employment_types") or [])
                                              + [j.get("contract_duration")]) if x)
            pay = _annual_pay(j.get("job_min_salary"), j.get("job_max_salary"), j.get("job_salary_period") or "")
            pub = str(j.get("job_publisher") or "web")
            row = _engine_role(reg, prof, j.get("job_title"), j.get("employer_name"), loc, pay, jtype, link,
                               str(j.get("job_posted_at_datetime_utc") or "")[:10], j.get("job_description"),
                               bool(j.get("job_is_remote")), f"JSearch ({pub})", hint, firm_keys)
            if row:
                out.append(row)
        time.sleep(0.3)
    print(f"  JSearch: {used} searches, {read} listings read, {len(out)} candidates")
    if used:
        save_json(JSEARCH_SAVE, {"date": TODAY, "rows": out, "month": st["month"], "used": st["used"] + used})
    return out


CAREERJET_SAVE = HERE / "radar_careerjet.json"


def careerjet_jobs(reg: dict, prof: dict) -> list[dict]:
    """Careerjet (publisher key in CAREERJET_API_KEY): a job search engine like
    Adzuna, contract listings only, for your roles by name."""
    key = os.getenv("CAREERJET_API_KEY", "").strip()
    if not key:
        print("  Careerjet: no CAREERJET_API_KEY set — skipped")
        return []
    again = _reuse(CAREERJET_SAVE, "Careerjet")
    if again is not None:
        return again
    s = radar_session()
    ua = careers.UA["User-Agent"]
    ip = ""
    try:                                          # Careerjet asks for the caller's address
        ip = s.get("https://api.ipify.org", timeout=(5, 15)).text.strip()
    except Exception:
        pass
    firm_keys = {company_key(n) for n in _feed_firms()}
    plan = ([(q, "") for q in CONTRACT_QUERIES] + [(q, "tech") for q in TECH_CONTRACT_QUERIES]
            + [(q, "energy") for q in ENERGY_CONTRACT_QUERIES])
    out, used, read, seen = [], 0, 0, set()
    for q, hint in plan:
        try:
            r = s.get("https://search.api.careerjet.net/v4/query", auth=(key, ""), timeout=(5, 30),
                      params={"locale_code": "en_US", "keywords": q, "contract_type": "c", "sort": "date",
                              "page_size": 50, "fragment_size": 800, "user_ip": ip or "127.0.0.1",
                              "user_agent": ua})
        except Exception as e:
            log(f"careerjet '{q}': {e.__class__.__name__}")
            continue
        used += 1
        if r.status_code in (401, 403, 429):
            log(f"careerjet: HTTP {r.status_code} {r.text.replace(key, '<key>')[:160]!r} — stopping")
            print(f"  Careerjet: HTTP {r.status_code} — key refused or limit reached")
            break
        try:
            d = r.json() if r.ok else {}
        except ValueError:
            d = {}
        for j in (d.get("jobs") if isinstance(d, dict) else None) or []:
            if not isinstance(j, dict):
                continue
            read += 1
            ident = (j.get("title"), j.get("company"))
            if ident in seen:
                continue
            seen.add(ident)
            pay = _annual_pay(j.get("salary_min"), j.get("salary_max"), str(j.get("salary_type") or "")) \
                or str(j.get("salary") or "")
            loc = j.get("locations") or ""
            body = j.get("description") or ""
            row = _engine_role(reg, prof, j.get("title"), j.get("company"), loc, pay, "contract", j.get("url"),
                               str(j.get("date") or "")[:10], body,
                               bool(re.search(r"remote|anywhere", f"{loc} {body[:300]}", re.I)),
                               "Careerjet", hint, firm_keys, soft=True)
            if row:
                out.append(row)
        time.sleep(0.5)
    print(f"  Careerjet: {used} searches, {read} listings read, {len(out)} candidates")
    if used:
        save_json(CAREERJET_SAVE, {"date": TODAY, "rows": out})
    return out


ASA_TERMS = ["biostatistician", "statistician", "statistical programmer", "biostatistics",
             "statistical consultant", "contract", "consultant", "data scientist", "pharmacometrics",
             "epidemiologist", "machine learning", "real world evidence"]


def asa_jobs(reg: dict, prof: dict) -> list[dict]:
    """ASA Career Connect, the American Statistical Association's job board:
    its public job feed, one request per search word. No key."""
    import html as _html
    import xml.etree.ElementTree as ET
    s = radar_session()
    firm_keys = {company_key(n) for n in _feed_firms()}
    out, read, seen, ok = [], 0, set(), 0
    for kw in ASA_TERMS:
        try:
            r = s.get("https://careerconnect.amstat.org/jobsrss/", headers=careers.UA, timeout=(5, 30),
                      params={"keywords": kw, "countrycode": "US"})
            root = ET.fromstring(r.content) if r.ok else None
        except Exception as e:
            log(f"asa '{kw}': {e.__class__.__name__}")
            continue
        if root is None:
            continue
        ok += 1
        for it in root.iter("item"):
            read += 1
            head, link = (it.findtext("title") or "").strip(), (it.findtext("link") or "").strip()
            if not link or link in seen:
                continue
            seen.add(link)
            comp, _, title = head.partition(": ")
            if not title:
                comp, title = "", head
            desc = re.sub(r"\s+", " ", _html.unescape(re.sub(r"<[^>]+>", " ", it.findtext("description") or ""))).strip()
            lead = desc.split(": ", 1)[0]                 # "Salary: Employer: text"
            pay = lead if re.search(r"\$\s?\d", lead) else ""
            row = _engine_role(reg, prof, title, comp, "", pay, "", link,
                               careers._rss_date(it.findtext("pubDate") or ""), desc,
                               bool(re.search(r"\bremote\b", desc, re.I)), "ASA Career Connect", "", firm_keys)
            if row:
                out.append(row)
        time.sleep(0.4)
    print(f"  ASA Career Connect: {ok} searches, {read} listings read, {len(out)} candidates")
    return out


def open_feeds(reg: dict, prof: dict) -> list[dict]:
    """Free job indexes that need no key: Remote OK, Jobicy, Himalayas, The Muse.
    Each is one or a few requests; any that fails or changes shape is skipped."""
    s = radar_session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (rotwang-radar; job search for one person)"})
    got: list[tuple] = []          # (title, company, location, pay, type, url, posted, body, remote)

    def money(lo, hi):
        try:
            lo, hi = int(float(lo or 0)), int(float(hi or 0))
        except (TypeError, ValueError):
            return ""
        return f"${(lo or hi):,}–${(hi or lo):,}" if (lo or hi) else ""

    def remoteok():
        r = s.get("https://remoteok.com/api", timeout=(5, 30))
        for j in (r.json() if r.ok else []):
            if isinstance(j, dict) and j.get("position"):
                got.append((j.get("position"), j.get("company"), j.get("location") or "Remote",
                            money(j.get("salary_min"), j.get("salary_max")), " ".join(j.get("tags") or []),
                            j.get("url") or j.get("apply_url"), (j.get("date") or "")[:10],
                            j.get("description"), True))

    def jobicy():
        for tag in ("data-science", "statistics", "machine-learning", "biostatistics"):
            r = s.get("https://jobicy.com/api/v2/remote-jobs", params={"count": 50, "tag": tag},
                      timeout=(5, 30))
            for j in ((r.json() or {}).get("jobs", []) if r.ok else []):
                jt = j.get("jobType")
                got.append((j.get("jobTitle"), j.get("companyName"), j.get("jobGeo") or "Remote",
                            money(j.get("annualSalaryMin"), j.get("annualSalaryMax")),
                            " ".join(jt) if isinstance(jt, list) else str(jt or ""),
                            j.get("url"), (j.get("pubDate") or "")[:10],
                            j.get("jobDescription") or j.get("jobExcerpt"), True))
            time.sleep(0.5)

    def himalayas():
        for off in (0, 20, 40, 60, 80):
            r = s.get("https://himalayas.app/jobs/api", params={"limit": 20, "offset": off}, timeout=(5, 30))
            for j in ((r.json() or {}).get("jobs", []) if r.ok else []):
                loc = j.get("locationRestrictions") or []
                got.append((j.get("title"), j.get("companyName"),
                            ", ".join(loc) if isinstance(loc, list) else str(loc or "") or "Remote",
                            money(j.get("minSalary"), j.get("maxSalary")), str(j.get("employmentType") or ""),
                            j.get("applicationLink") or j.get("guid"), str(j.get("pubDate") or "")[:10],
                            j.get("description") or j.get("excerpt"), True))
            time.sleep(0.4)

    def muse():
        for page in range(4):
            r = s.get("https://www.themuse.com/api/public/jobs",
                      params=[("category", "Data Science"), ("category", "Data and Analytics"), ("page", page)],
                      timeout=(5, 30))
            for j in ((r.json() or {}).get("results", []) if r.ok else []):
                locs = ", ".join((x or {}).get("name", "") for x in j.get("locations") or [])
                got.append((j.get("name"), (j.get("company") or {}).get("name"), locs, "",
                            str(j.get("type") or ""), (j.get("refs") or {}).get("landing_page"),
                            (j.get("publication_date") or "")[:10], j.get("contents"),
                            "remote" in locs.lower()))
            time.sleep(0.4)

    counts = {}
    for label, fn in [("Remote OK", remoteok), ("Jobicy", jobicy), ("Himalayas", himalayas), ("The Muse", muse)]:
        before = len(got)
        try:
            fn()
        except Exception as e:
            log(f"open feed {label}: {e.__class__.__name__}: {e}")
        counts[label] = len(got) - before
        got[before:] = [g + (label,) for g in got[before:]]

    firm_keys = {company_key(n) for n in _feed_firms()}
    out = []
    for title, comp, loc, pay, jtype, url, posted, body, remote, label in got:
        title, comp = rw.clean(str(title or "")), str(comp or "").strip()
        if not title or not comp or not url:
            continue
        if not (quant_title(title, True) or leadership_title(title)):
            continue
        body = rw.clean(str(body or ""))[:1500]
        role = rw.Role(title=title, company=comp, location=str(loc or ""), pay=pay,
                       contract=rw.is_contract(title, jtype, body), remote=bool(remote),
                       source=f"{comp} · {label}", url=str(url), posted=str(posted or ""), body=body)
        agency = rw.is_agency(comp) or company_key(comp) in firm_keys
        ck = _feed_company(reg, comp, agency)
        row = keep_role(role, prof, comp, ck, agency, sector=company_sector(reg.get(ck)))
        if row:
            out.append(row)
    print("  Open feeds: " + ", ".join(f"{n} read from {k}" for k, n in counts.items())
          + f" — {len(out)} candidates")
    return out


def dedupe_boards(reg: dict) -> int:
    """One board claimed by several similar names ("Applied Therapeutics",
    "Applied Medical"…) would show every job several times. Keep the single
    best claim — the name that spells the board's address, else the oldest —
    and mark the rest as mismatches."""
    by_board: dict[str, list[str]] = {}
    for k, c in reg.items():
        if c.get("status") == "watching" and c.get("ats") and c.get("token"):
            by_board.setdefault(f"{c['ats']}|{str(c['token']).lower()}", []).append(k)
    dropped = 0
    for board, keys in by_board.items():
        if len(keys) < 2:
            continue
        def score(k):
            c = reg[k]
            return (_is_strong(c["name"], c["token"]), "curated" in c.get("sources", []),
                    len(c.get("sources", [])), c.get("resolved", "") == "" or c.get("resolved", "~"))
        keep = max(keys, key=score)
        for k in keys:
            if k != keep:
                reg[k].update({"status": "mismatch", "wrong_board": board, "checked": TODAY})
                dropped += 1
                log(f"duplicate board: {reg[k]['name']} shared {board} with {reg[keep]['name']} — dropped")
    return dropped


def sweep(reg: dict, prof: dict) -> tuple[list[dict], list[dict], int]:
    dup = dedupe_boards(reg)
    if dup:
        print(f"  {dup} duplicate claims on shared boards removed")
    watching = [k for k, c in reg.items() if c["status"] == "watching"]
    # Workday sites are slow to ask, so they go last, least recently read first,
    # inside a fixed time window; the rest wait for next week without a strike.
    watching.sort(key=lambda k: (reg[k].get("ats") == "workday",
                                 reg[k].get("swept", "") if reg[k].get("ats") == "workday" else ""))
    print(f"  Sweeping {len(watching)} boards (Workday sites: at most {WD_READ_MIN} minutes)…")
    WD_STOP[0] = time.time() + WD_READ_MIN * 60
    results = _read_many(watching, reg)
    skipped = [k for k, (_, ok) in results.items() if ok is None]
    if skipped:
        print(f"  {len(skipped)} Workday sites left for next week (time window used up)")
        log(f"sweep: {len(skipped)} Workday sites skipped for time")
    bad = [k for k, (_, ok) in results.items() if ok is False]
    mass = len(watching) >= 20 and len(bad) > MASS_FAIL_SHARE * len(watching)
    if mass:
        print(f"  {len(bad)} of {len(watching)} boards didn't answer — that's this computer's "
              f"connection, not the boards. Waiting {MASS_FAIL_WAIT // 60} minutes, then retrying…")
        log(f"sweep: {len(bad)}/{len(watching)} failed — pausing {MASS_FAIL_WAIT}s and retrying")
        time.sleep(MASS_FAIL_WAIT)
        results.update(_read_many(bad, reg))
        bad = [k for k, (_, ok) in results.items() if ok is False]
        mass = len(bad) > MASS_FAIL_SHARE * len(watching)
        log(f"sweep retry: {len(bad)}/{len(watching)} still failing" + (" — NOT penalising boards" if mass else ""))
    WD_STOP[0] = float("inf")
    SWEEP_HEALTH.update({"total": len(watching), "failed": len(bad), "mass": mass})
    current, failed = [], 0

    for k in watching:
            roles, ok = results.get(k, ([], False))
            c = reg[k]
            if ok is None:
                continue                             # not read this week; no strike
            if not ok:
                failed += 1
                if not mass:                         # a strike only when the network was fine
                    c["fails"] = c.get("fails", 0) + 1
                    if c["fails"] >= STRIKES:
                        c["status"] = "stale"        # re-resolved on a later sweep
                continue
            c["fails"] = 0
            c["swept"] = TODAY
            kept = []
            for r in roles:
                if _NEVER.search(r.title or ""):
                    continue                        # internships, co-ops: never, either scope
                row = keep_role(r, prof, c["name"], k, sector=company_sector(c),
                                direct="agency" not in (c.get("sectors") or []))
                if row:
                    kept.append(row)
            c["open"] = len(kept)
            current.extend(kept)

    # Recruiters and consultancies post on their own sites but syndicate to the
    # indexes; the feed reads them from there. Companies seen only through the
    # feed are recorded so they reach the globe.
    for name, fn in [("google jobs", google_jobs), ("jsearch", jsearch_jobs), ("careerjet", careerjet_jobs),
                     ("asa career connect", asa_jobs),
                     ("agency feed", agency_feed), ("contract sweep", contract_sweep),
                     ("open feeds", open_feeds)]:
        try:
            current.extend(fn(reg, prof))
        except Exception as e:                    # a feed must never sink the sweep
            log(f"{name} error: {e.__class__.__name__}: {e}")

    try:
        learned = learn_paylocity(reg, [r.get("url", "") for r in current])
        if learned:
            print(f"  Learned {learned} new Paylocity board(s) from feed links — read from the next sweep")
    except Exception as e:
        log(f"paylocity learning error: {e.__class__.__name__}: {e}")

    # De-duplicate across sources, then let Claude read every candidate.
    uniq, keys = [], set()
    for r in current:
        if r["key"] not in keys:
            keys.add(r["key"]); uniq.append(r)
    try:
        classify_rows(uniq)
    except Exception as e:
        log(f"classify error: {e.__class__.__name__}: {e}")
    perm_count = 0
    shown = apply_verdicts(uniq)
    perm_count = sum(1 for r in uniq if r.get("_perm"))
    SWEEP_HEALTH["perm"] = perm_count
    SWEEP_HEALTH["candidates"] = len(uniq)
    current = shown

    seen = load_json(SEEN, {})
    baseline = not seen
    new = []
    for r in current:
        if r["key"] not in seen:
            seen[r["key"]] = TODAY
            if not baseline:
                new.append(r)
        r["first_seen"] = seen[r["key"]]
        r["new"] = r["first_seen"] == TODAY and not baseline
    save_json(SEEN, seen)
    save_json(COMPANIES, reg)
    in_lane = sum(r["lane"] for r in current)
    print(f"  {in_lane} roles in your lane + {len(current) - in_lane} other contract roles"
          f" ({SCOPE} scope)")
    if SWEEP_HEALTH.get("mass"):
        print(f"  SWEEP FAILED: {SWEEP_HEALTH['failed']} of {SWEEP_HEALTH['total']} boards unreachable after a retry."
              " Check the connection and run again — nothing was penalised.")
    print(f"  {len(current)} roles on the radar, {len(new)} new"
          + (" (first sweep — baseline set, nothing counted as new)" if baseline else "")
          + (f", {failed} boards unreachable" if failed else ""))
    return current, new, failed


# ═══════════════════════════════════════════════════════════════ 4. ALERT ════

_SENIOR_RANK = {"vp_head": 6, "senior_director": 5, "director": 4, "associate_director": 3, "lead": 2,
                "individual": 1}


def alert_text(new: list[dict], current: list[dict], reg: dict) -> str:
    stamp = datetime.now(timezone.utc).strftime("%d %b %Y · %H:%M UTC")
    watching = sum(c["status"] == "watching" for c in reg.values())
    h = SWEEP_HEALTH
    if h.get("mass"):
        return (f"# ROTWANG RADAR — {stamp}\n\n"
                f"SWEEP DID NOT COMPLETE — {h['failed']} of {h['total']} job boards could not be reached, "
                f"even after a retry.\n\nThis is a connection problem on this computer, not an empty market. "
                f"Boards were not penalised. Check the internet connection and run the sweep again.\n"
                + (f"\n{len(current)} role(s) were read from the boards that did answer.\n" if current else ""))
    lines = [f"# ROTWANG RADAR — {stamp}", "",
             f"{len(new)} new role(s) · {len(current)} on the radar · "
             f"{watching} of {len(reg)} companies watched · {SCOPE} scope", ""]
    if not new:
        if h.get("perm"):
            lines.append(f"({h['perm']} permanent roles in your field are hidden — run with --perm to list them.)\n")
        lines.append("No new roles since the last sweep."
                     + (f" ({h['failed']} of {h['total']} boards could not be reached this time.)"
                        if h.get("failed") else ""))
        return "\n".join(lines) + "\n"
    # Rate first, then US/remote before overseas, then fit.
    # Rate first, then US/remote before overseas, then fit.
    rank = lambda r: (-(r.get("hourly") or 0), 0 if r.get("us", True) else 1, -r["fit"])
    order = sorted(new, key=rank)
    groups = [
        ("## Contract & hourly — rate stated, highest first",
         [r for r in order if r.get("contract") and r.get("hourly")]),
        ("## Contract — rate not stated", [r for r in order if r.get("contract") and not r.get("hourly")]),
        ("## Leadership & lead-statistician roles — pitch as fractional or contract",
         sorted([r for r in order if r.get("convertible")],
                key=lambda r: (-(r.get("hourly") or 0), -_SENIOR_RANK.get(r.get("seniority", ""), 0), -r["fit"]))),
        ("## Other permanent roles in your field (--perm)",
         [r for r in order if not r.get("contract") and not r.get("convertible")]),
    ]
    for title, group in groups:
        if not group:
            continue
        lines += [f"{title} ({len(group)})", ""]
        for r in group:
            bits = [b for b in [r["location"], r["pay"]] if b]
            if r.get("hourly"):
                bits.append(f"≈ ${r['hourly']}/hr" + (" salary-equivalent" if r.get("convertible") else ""))
            if r.get("agency"):
                bits.append("via recruiter")
            if r.get("engagement") and r["engagement"] not in ("contract", "permanent"):
                bits.append(r["engagement"].replace("_", " "))
            lines.append(f"- **{r['title']}** — {r['company']} (fit {r['fit']})"
                         + (f" · {' · '.join(bits)}" if bits else "") + f"  \n  {r['url']}")
        lines.append("")
    return "\n".join(lines) + "\n"


ISSUE_OUT = HERE / "radar_issue.md"
ALERT_MIN_RATE = int(os.getenv("RADAR_ALERT_MIN_RATE", "0"))   # $/hr; 0 lists every new contract
APP_URL = os.getenv("RADAR_APP_URL", "https://archimedesrazorblade18.github.io/rotwang/")


def issue_text(new: list[dict]) -> str:
    """The Monday alert: new contracts, highest rate first, then new roles worth
    pitching. Empty when there is nothing new, so no alert is sent."""
    new = [r for r in new if not _NEVER.search(r.get("title") or "")]
    con = [r for r in new if r.get("contract") and (r.get("hourly") or 0) >= ALERT_MIN_RATE
           or (r.get("contract") and not r.get("hourly") and ALERT_MIN_RATE == 0)]
    con.sort(key=lambda r: (-(r.get("hourly") or 0), 0 if r.get("us", True) else 1, -r["fit"]))
    pitch = sorted([r for r in new if r.get("convertible")],
                   key=lambda r: (-(r.get("hourly") or 0), -_SENIOR_RANK.get(r.get("seniority", ""), 0)))
    if not con and not pitch:
        return ""
    def line(r):
        rate = f"**${r['hourly']}/hr** · " if r.get("hourly") else ""
        where = f" · {r['location']}" if r.get("location") else ""
        return f"- {rate}[{tidy_title(r['title'])}]({r['url']}) — {r['company']}{where}"
    out = [f"**{len(con)} new contract role(s)** and {len(pitch)} new role(s) to pitch as contract. "
           f"[Open ROTWANG]({APP_URL})", ""]
    if con:
        out += ["### New contracts", ""] + [line(r) for r in con[:30]]
        if len(con) > 30:
            out.append(f"- …and {len(con) - 30} more in the app")
        out.append("")
    if pitch:
        out += ["### New roles to pitch as contract", ""] + [line(r) for r in pitch[:8]]
        if len(pitch) > 8:
            out.append(f"- …and {len(pitch) - 8} more in the app")
    return "\n".join(out) + "\n"


def send_email(body: str, n_new: int) -> None:
    to, user, pw = (os.getenv("RADAR_EMAIL_TO"), os.getenv("RADAR_SMTP_USER"),
                    os.getenv("RADAR_SMTP_PASS"))
    if not n_new:
        return
    if not (to and user and pw):
        print("  Email not configured (RADAR_EMAIL_TO / RADAR_SMTP_USER / RADAR_SMTP_PASS) — skipped")
        return
    msg = EmailMessage()
    msg["Subject"] = f"ROTWANG RADAR · {n_new} new role(s)"
    msg["From"], msg["To"] = user, to
    msg.set_content(body)
    with smtplib.SMTP(os.getenv("RADAR_SMTP_HOST", "smtp.gmail.com"),
                      int(os.getenv("RADAR_SMTP_PORT", "587"))) as srv:
        srv.starttls()
        srv.login(user, pw)
        srv.send_message(msg)
    print(f"  Emailed {n_new} new role(s) to {to}")


def write_html(reg: dict, current: list[dict]) -> None:
    by_co: dict[str, list] = {}
    for r in current:
        by_co.setdefault(r["ck"], []).append(
            {k: r.get(k) for k in ("title", "url", "contract", "pay", "hourly", "location",
                                   "remote", "fit", "posted", "first_seen", "new", "lane", "agency",
                                   "convertible", "seniority", "engagement")})
    companies = []
    for k, c in reg.items():
        if c["status"] != "watching" and k not in by_co:
            continue
        companies.append({"k": k, "n": c["name"], "t": c.get("trials", 0),
                          "src": c.get("sources", []), "ats": c.get("ats", ""),
                          "sec": c.get("sectors", []), "sp": c.get("specialty", ""),
                          "roles": sorted(by_co.get(k, []), key=lambda r: -r["fit"])})
    counts = {s: sum(c["status"] == s for c in reg.values())
              for s in ("watching", "none", "unresolved", "stale")}
    payload = {"generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
               "universe": len(reg), "counts": counts, "companies": companies}
    blob = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    HTML_OUT.write_text(RADAR_TEMPLATE.replace("__RADAR_DATA__", blob), encoding="utf-8")
    print(f"  {HTML_OUT.name} written — open it to see the radar")


def write_app_data(reg: dict, current: list[dict]) -> None:
    """docs/data.json — everything the installed app shows, as one small file."""
    roles = []
    for r in current:
        if not r.get("us", True) or _NEVER.search(r.get("title") or ""):
            continue
        c = reg.get(r.get("ck"), {})
        roles.append({
            "id": r["key"], "title": tidy_title(r["title"]), "company": r.get("company") or c.get("name", ""),
            "sector": r.get("sector") or company_sector(c) or "health",
            "kind": "contract" if r.get("contract") else "pitch",
            "engagement": r.get("engagement") or ("contract" if r.get("contract") else "permanent"),
            "hourly": r.get("hourly"), "pay": r.get("pay") or "", "location": r.get("location") or "",
            "remote": bool(r.get("remote")), "fit": r.get("fit") or 0,
            "first_seen": r.get("first_seen") or TODAY, "posted": r.get("posted") or "",
            "url": r.get("url") or "", "agency": bool(r.get("agency")),
            "direct": bool(r.get("direct")), "source": r.get("source") or c.get("ats", ""),
            "why": r.get("why") or "", "seniority": r.get("seniority") or ""})
    watched = {"health": 0, "energy": 0, "tech": 0}
    for c in reg.values():
        if c.get("status") == "watching":
            watched[company_sector(c) or "health"] += 1
    payload = {
        "generated": datetime.now(timezone.utc).isoformat(timespec="minutes"),
        "universe": len(reg), "watched": watched,
        "boards": {"total": SWEEP_HEALTH.get("total", 0), "failed": SWEEP_HEALTH.get("failed", 0),
                   "mass_failure": bool(SWEEP_HEALTH.get("mass"))},
        "reader": bool(_api_key()), "index": bool(_has_adzuna()),
        "engines": {"google_jobs": bool(os.getenv("SERPAPI_KEY")),
                    "adzuna": bool(_has_adzuna()),
                    "jsearch": bool(os.getenv("JSEARCH_API_KEY")),
                    "careerjet": bool(os.getenv("CAREERJET_API_KEY"))},
        "roles": roles}
    APP_DATA.parent.mkdir(parents=True, exist_ok=True)
    if SWEEP_HEALTH.get("mass") and APP_DATA.exists():
        # A failed sweep must not wipe a good list: keep the roles, record the failure.
        old = load_json(APP_DATA, {})
        payload["roles"] = old.get("roles", [])
        payload["generated"] = old.get("generated", payload["generated"])
        payload["last_attempt_failed"] = TODAY
    save_json(APP_DATA, payload)
    print(f"  {APP_DATA.relative_to(HERE)} written — {len(payload['roles'])} roles for the app")


def _has_adzuna() -> bool:
    if os.getenv("ADZUNA_APP_ID") and os.getenv("ADZUNA_APP_KEY"):
        return True
    return bool(load_json(HERE / "rotwang_keys.json", {}).get("app_id"))


def merge_curated(reg: dict) -> int:
    """Names added to rotwang_targets.json join the universe without a full rebuild."""
    with contextlib.redirect_stdout(io.StringIO()):
        cur = fetch_curated()
    d = load_json(HERE / "rotwang_targets.json", {})
    groups = d.get("targets", {}) if isinstance(d, dict) else {}
    domains = d.get("domains", {}) if isinstance(d, dict) else {}
    added = 0
    for g, sector in CURATED_GROUPS.items():          # every group a name sits in, not only the first
        for n in groups.get(g, []):
            k = company_key(n)
            if not k:
                continue
            if k not in reg:
                reg[k] = {"name": cur.get(k, {}).get("name", n), "sources": [], "status": "unresolved",
                          "trials": 0, "ticker": "", "added": TODAY}
                added += 1
            c = reg[k]
            if domains.get(n) and not c.get("website"):
                c["website"] = domains[n]         # known address: the careers finder starts there
            if "curated" not in c.setdefault("sources", []):
                c["sources"].append("curated")
            if sector not in c.setdefault("sectors", []):
                c["sectors"].append(sector)
    return added


def weekly_prepare() -> None:
    """The Monday run, before the sweep: refresh the universe monthly, take in
    new hand-picked names, and test the Workday candidates."""
    meta = load_json(META, {})
    reg = load_json(COMPANIES, {})
    stale = (datetime.now(timezone.utc) - timedelta(days=28)).strftime("%Y-%m-%d")
    if not reg or meta.get("built", "0") < stale:
        try:
            build_universe()
        except Exception as e:
            log(f"weekly: universe rebuild failed: {e!r}")
        reg = load_json(COMPANIES, {})
    n = merge_curated(reg)
    if n:
        print(f"  {n} new hand-picked companies added to the universe")
    save_json(COMPANIES, reg)
    try:
        discover_workday(reg)
    except Exception as e:
        log(f"weekly: workday discovery failed: {e!r}")


# ═══════════════════════════════════════════════════════════════════ CLI ════

def status(reg: dict) -> int:
    if not reg:
        print("\nNo universe yet. Run:  python radar.py --build\n")
        return 1
    c = {s: sum(v["status"] == s for v in reg.values())
         for s in ("watching", "unresolved", "none", "stale", "error", "mismatch")}
    ats = {}
    for v in reg.values():
        if v["status"] == "watching":
            ats[v["ats"]] = ats.get(v["ats"], 0) + 1
    src = {}
    for v in reg.values():
        for s in v.get("sources", []):
            src[s] = src.get(s, 0) + 1
    lit = sum(v.get("open", 0) > 0 for v in reg.values())
    print(f"\n  Universe     {len(reg)} companies  ({', '.join(f'{n} {k}' for k, n in src.items())})")
    sect, wsect = {}, {}
    for v in reg.values():
        for x in v.get("sectors", []) or ["unknown"]:
            sect[x] = sect.get(x, 0) + 1
            if v["status"] == "watching":
                wsect[x] = wsect.get(x, 0) + 1
    print("  Sectors      " + ", ".join(f"{k} {n} ({wsect.get(k, 0)} watched)"
                                      for k, n in sorted(sect.items(), key=lambda x: -x[1])))
    print(f"  Watching     {c['watching']}  ({', '.join(f'{n} {k}' for k, n in sorted(ats.items()))})")
    print(f"  Lit          {lit} with an open role in your lane")
    print(f"  Unresolved   {c['unresolved']}  (probed {RESOLVE_BUDGET} per sweep)")
    print(f"  Blind        {c['none']}  no public board found — custom sites, or none open")
    print(f"  Stale        {c['stale']}  board stopped answering, re-probing")
    if c["mismatch"]:
        print(f"  Mismatched   {c['mismatch']}  board belonged to a different company — excluded")
    if c["error"]:
        print(f"  Errors       {c['error']}  retried next sweep — details in radar_log.txt")
    left = c["unresolved"] + c["error"]
    if left:
        print(f"\n  Resolve the rest now:  python radar.py --resolve {left}")
    print("  Map a blind company by hand:  python radar.py --add \"Name\" https://careers-url\n")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="ROTWANG RADAR — watch every biotech careers board")
    p.add_argument("--build", action="store_true", help="build or refresh the company universe")
    p.add_argument("--resolve", type=int, metavar="N", help="resolve careers boards for N companies, then stop")
    p.add_argument("--status", action="store_true", help="coverage report")
    p.add_argument("--add", nargs=2, metavar=("NAME", "URL"), help="map a company to a careers URL")
    p.add_argument("--no-resolve", action="store_true", help="sweep without resolving new companies")
    p.add_argument("--open", action="store_true", help="open radar.html when done")
    p.add_argument("--hosts", nargs="+", metavar="URL", help="testing: show what careers pages load")
    p.add_argument("--check-keys", action="store_true", help="test the search keys (never prints them)")
    p.add_argument("--classify-test", metavar="ATS:TOKEN", help="testing: let Claude judge one board")
    p.add_argument("--engine-test", metavar="NAME", help="testing: run one search engine alone (jsearch, careerjet, asa) with a few searches and print what it found")
    p.add_argument("--recheck", action="store_true", help="testing: re-check careers sites on newly readable systems")
    p.add_argument("--peek", nargs="+", metavar="URL", help="testing: show how a careers page lists jobs")
    p.add_argument("--read", nargs="+", metavar="ATS:TOKEN", help="testing: run a reader, e.g. phenom:careers.roche.com|global/en")
    p.add_argument("--careers", type=int, metavar="N",
                   help="look for the careers pages of N companies and show what was found")
    p.add_argument("--workday", action="store_true",
                   help="test Workday careers sites for large device & pharma companies")
    p.add_argument("--perm", action="store_true", help="also list permanent roles in your field")
    p.add_argument("--weekly", action="store_true",
                   help="the full Monday run: refresh companies, find boards, sweep, update the app")
    p.add_argument("--wide", action="store_true",
                   help="every professional contract role, not only quantitative ones")
    p.add_argument("--narrow", action="store_true",
                   help="your lane only (biostat / RWE / programming / comp bio) — no other contract roles")
    a = p.parse_args()
    global SCOPE
    global SHOW_PERM
    SHOW_PERM = a.perm
    if a.narrow:
        SCOPE = "narrow"
    elif a.wide:
        SCOPE = "wide"

    if a.build:
        return build_universe()
    if a.weekly:
        weekly_prepare()
    reg = load_json(COMPANIES, {})
    if a.status:
        return status(reg)
    if not reg:
        print("\nNo universe yet. Run:  python radar.py --build\n")
        return 1
    if a.add:
        name, url = a.add
        parsed = parse_careers_url(url)
        if not parsed:
            print("\nThat URL isn't a Greenhouse, Lever, Ashby, SmartRecruiters, Workable, "
                  "Recruitee or Workday careers page. Paste the page that lists the jobs.\n")
            return 1
        k = company_key(name)
        reg.setdefault(k, {"name": name, "sources": ["manual"], "trials": 0, "ticker": "",
                           "added": TODAY})
        reg[k].update({"status": "watching", "ats": parsed[0], "token": parsed[1],
                       "resolved": TODAY, "verified": TODAY, "fails": 0})
        save_json(COMPANIES, reg)
        print(f"\n  {name} -> {parsed[0]} ({parsed[1]}). Watched from the next sweep.\n")
        return 0
    if a.workday:
        discover_workday(reg)
        return status(reg)
    if a.careers:
        find_careers_pages(reg, a.careers, verbose=True)
        return 0
    if a.recheck:
        recheck_systems(reg, verbose=True)
        return 0
    if a.classify_test:
        global CLASS_CACHE
        CLASS_CACHE = Path(tempfile.gettempdir()) / "rotwang_class_test.json"
        ats, tok = a.classify_test.split(":", 1)
        roles, ok = read_board(requests.Session(), {"ats": ats, "token": tok, "name": tok.split("|")[0], "sectors": ["pharma"]})
        rows = [{"key": r.dkey, "title": r.title, "company": r.company, "location": r.location,
                 "pay": r.pay, "body": r.body or ""} for r in roles]
        t0 = time.time()
        classify_rows(rows)
        print(f"  {len(rows)} rows, {sum(1 for r in rows if r.get('cls'))} judged in {time.time() - t0:.0f}s")
        for r in rows[:6]:
            print(f"    {r['title'][:50]:50} {json.dumps(r.get('cls'))[:150]}")
        return 0
    if a.engine_test:
        global JSEARCH_PER_RUN
        JSEARCH_PER_RUN = int(os.getenv("RADAR_TEST_SEARCHES", "3"))
        prof = device_aware(rw.active_profile(PROFILE))
        fn = {"jsearch": jsearch_jobs, "careerjet": careerjet_jobs, "asa": asa_jobs}[a.engine_test]
        if a.engine_test == "careerjet":
            global CONTRACT_QUERIES, TECH_CONTRACT_QUERIES, ENERGY_CONTRACT_QUERIES
            CONTRACT_QUERIES, TECH_CONTRACT_QUERIES, ENERGY_CONTRACT_QUERIES = CONTRACT_QUERIES[:3], [], []
        rows = fn(reg, prof)
        for r in rows[:25]:
            print(f"    {'C' if r.get('contract') else '-'} {str(r.get('hourly') or ''):>4} "
                  f"{r['title'][:55]:55} | {r.get('company', '')[:24]:24} | {r.get('source', '')[-28:]}")
        bodies = [len(r.get("body") or "") for r in rows]
        if bodies:
            print(f"  description length: median {sorted(bodies)[len(bodies) // 2]} characters")
        return 0
    if a.check_keys:
        s = requests.Session()
        aid, akey = os.getenv("ADZUNA_APP_ID", "").strip(), os.getenv("ADZUNA_APP_KEY", "").strip()
        print(f"  Adzuna: app id {len(aid)} characters, app key {len(akey)} characters")
        if aid and akey:
            r = s.get("https://api.adzuna.com/v1/api/jobs/us/search/1",
                      params={"app_id": aid, "app_key": akey, "results_per_page": 5, "what": "biostatistician"},
                      timeout=(5, 30))
            body = r.text.replace(aid, "<id>").replace(akey, "<key>")[:300]
            print(f"  Adzuna answer: HTTP {r.status_code} {body!r}")
        sk = os.getenv("SERPAPI_KEY", "").strip()
        if sk:
            r = s.get("https://serpapi.com/account.json", params={"api_key": sk}, timeout=(5, 30))
            d = r.json() if r.ok else {}
            print(f"  SerpApi: HTTP {r.status_code}, plan {d.get('plan_name')}, "
                  f"{d.get('total_searches_left', d.get('plan_searches_left'))} searches left")
        jk = os.getenv("JSEARCH_API_KEY", "").strip()
        print(f"  JSearch: key {'set' if jk else 'not set'}" + (f" ({len(jk)} characters)" if jk else ""))
        if jk:
            r = s.get("https://api.openwebninja.com/jsearch/search-v2", headers={"x-api-key": jk},
                      params={"query": "contract biostatistician", "country": "us", "date_posted": "week"},
                      timeout=(10, 60))
            try:
                d = r.json()
                n = len(d.get("data") or []) if isinstance(d.get("data"), list) else d.get("data")
                shape = f"{n} jobs" if isinstance(n, int) else f"keys {list(d)[:6]}"
            except ValueError:
                shape = repr(r.text.replace(jk, "<key>")[:160])
            print(f"  JSearch answer: HTTP {r.status_code}, {shape}")
        ck = os.getenv("CAREERJET_API_KEY", "").strip()
        print(f"  Careerjet: key {'set' if ck else 'not set'}" + (f" ({len(ck)} characters)" if ck else ""))
        if ck:
            r = s.get("https://search.api.careerjet.net/v4/query", auth=(ck, ""), timeout=(5, 30),
                      params={"locale_code": "en_US", "keywords": "biostatistician", "contract_type": "c",
                              "page_size": 5, "user_ip": "127.0.0.1", "user_agent": careers.UA["User-Agent"]})
            print(f"  Careerjet answer: HTTP {r.status_code} {r.text.replace(ck, '<key>')[:200]!r}")
        ak = _api_key().strip()
        print(f"  Anthropic: key {'set' if ak else 'not set'}" + (f" ({len(ak)} characters)" if ak else ""))
        if ak:
            r = s.post("https://api.anthropic.com/v1/messages", timeout=(10, 60), json={
                "model": CLASSIFY_MODEL, "max_tokens": 5, "messages": [{"role": "user", "content": "Say ok"}]},
                headers={"x-api-key": ak, "anthropic-version": "2023-06-01", "content-type": "application/json"})
            print(f"  Anthropic answer: HTTP {r.status_code} {r.text.replace(ak, '<key>')[:120]!r}")
            lim = {k.replace("anthropic-ratelimit-", ""): v for k, v in r.headers.items()
                   if k.lower().startswith("anthropic-ratelimit-") and k.lower().endswith("-limit")}
            print(f"  Anthropic per-minute limits: {lim}")
        return 0
    if a.peek:
        s = requests.Session()
        for u in a.peek:
            print(careers.peek(s, u))
        return 0
    if a.read:
        s = requests.Session()
        for spec in a.read:
            ats, tok = spec.split(":", 1)
            t0 = time.time()
            roles, ok = read_board(s, {"ats": ats, "token": tok, "name": tok.split("|")[0], "sectors": ["pharma"]})
            print(f"  {spec}: ok={ok} {len(roles)} roles in {time.time() - t0:.0f}s")
            for r in roles[:8]:
                print(f"      {r.title[:60]:60} | {r.location[:24]:24} | {r.url[:90]}")
        return 0
    if a.hosts:
        s = requests.Session()
        for u in a.hosts:
            print(careers.page_hosts(s, u))
        return 0
    if a.resolve is not None:
        reverify(reg)
        resolve(reg, a.resolve)
        return status(reg)

    prof = device_aware(rw.active_profile(PROFILE))
    print(f"\nROTWANG RADAR · {prof['label']} · {TODAY}\n")
    for name, (ats, token) in KNOWN_BOARDS.items():
        k = company_key(name)
        c = reg.setdefault(k, {"name": name, "sources": ["curated"], "trials": 0, "ticker": "", "added": TODAY})
        if not (c.get("ats") == ats and c.get("token") == token and c.get("status") == "watching"):
            c.update({"status": "watching", "ats": ats, "token": token,
                      "resolved": TODAY, "verified": TODAY, "fails": 0})
    reverify(reg)
    if not a.no_resolve:
        resolve(reg, RESOLVE_BUDGET)
        try:
            recheck_systems(reg)
        except Exception as e:
            log(f"recheck error: {e.__class__.__name__}: {e}")
        try:
            find_careers_pages(reg)
        except Exception as e:                    # the finder must never sink the sweep
            log(f"careers error: {e.__class__.__name__}: {e}")
    current, new, _ = sweep(reg, prof)
    body = alert_text(new, current, reg)
    ALERT_OUT.write_text(body, encoding="utf-8")
    print(f"  {ALERT_OUT.name} written")
    try:
        ISSUE_OUT.write_text(issue_text(new), encoding="utf-8")
    except Exception as e:
        log(f"issue text error: {e.__class__.__name__}: {e}")
    try:
        send_email(body, len(new))
    except Exception as e:
        print(f"  Email failed ({e.__class__.__name__}: {e})")
    write_html(reg, current)
    try:
        write_app_data(reg, current)
    except Exception as e:
        log(f"app data error: {e.__class__.__name__}: {e}")
        print(f"  App data not written ({e.__class__.__name__}: {e})")
    if a.open:
        webbrowser.open(HTML_OUT.resolve().as_uri())
    print()
    return 0


RADAR_TEMPLATE = r'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ROTWANG — Radar</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Limelight&family=Poiret+One&family=IBM+Plex+Mono:wght@400;500;600&family=IBM+Plex+Sans:wght@400;500&display=swap');
:root{
  --void:#0B121C; --steel:#111B27; --steel-2:#18263A; --rivet:#2C4256; --haze:#7C96AC;
  --silver:#E4EAF0; --dim:#4A6076; --gold:#E6B23C; --gold-deep:#B8862A; --gold-ink:#3A2C0E;
  --marquee:'Limelight',Georgia,serif; --deco:'Poiret One','Century Gothic',sans-serif;
  --mono:'IBM Plex Mono',ui-monospace,Consolas,monospace; --sans:'IBM Plex Sans',system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;}
body{background:var(--void);color:var(--silver);font-family:var(--sans);font-size:14px;}
button,input{font:inherit;color:inherit;}
button{background:none;border:0;cursor:pointer;}
:focus-visible{outline:2px solid var(--gold);outline-offset:2px;}

.shell{display:grid;grid-template-rows:auto 1fr;height:100vh;}
.mast{display:flex;align-items:baseline;gap:18px;padding:18px 26px 14px;border-bottom:1px solid var(--rivet);flex-wrap:wrap;}
.mark{font-family:var(--marquee);font-size:25px;letter-spacing:.16em;color:var(--gold);text-shadow:0 0 22px rgba(230,178,60,.3);}
.mark small{font-family:var(--mono);font-size:10px;letter-spacing:.3em;color:var(--haze);margin-left:10px;}
.stats{display:flex;gap:22px;flex-wrap:wrap;margin-left:auto;font-family:var(--mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--haze);}
.stats b{display:block;font-family:var(--deco);font-size:24px;letter-spacing:0;color:var(--silver);text-transform:none;line-height:1;margin-bottom:3px;}
.stats .hot b{color:var(--gold);}

.body{display:grid;grid-template-columns:minmax(0,1fr) 400px;min-height:0;}
.scope{position:relative;display:grid;place-items:center;min-height:0;padding:18px;
  background:radial-gradient(60% 60% at 50% 50%,rgba(24,38,58,.55),transparent 70%);}
canvas{width:100%;height:100%;max-width:min(100%,86vh);aspect-ratio:1;display:block;cursor:crosshair;}
.legend{position:absolute;left:22px;bottom:18px;display:flex;flex-direction:column;gap:6px;
  font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;color:var(--haze);}
.legend span{display:flex;align-items:center;gap:8px;}
.legend i{width:9px;height:9px;border-radius:50%;display:inline-block;}
.stamp{position:absolute;right:22px;bottom:18px;font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;color:var(--dim);text-align:right;}

.side{border-left:1px solid var(--rivet);display:grid;grid-template-rows:auto auto 1fr;min-height:0;background:var(--steel);}
.filters{display:flex;gap:6px;flex-wrap:wrap;padding:14px 16px 10px;}
.f{font-family:var(--mono);font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;color:var(--haze);
  border:1px solid var(--rivet);padding:6px 10px;border-radius:2px;}
.f:hover{color:var(--silver);border-color:var(--gold-deep);}
.f[aria-pressed="true"]{background:var(--gold);border-color:var(--gold);color:var(--gold-ink);font-weight:600;}
.find{width:calc(100% - 32px);margin:0 16px 12px;background:var(--steel-2);border:1px solid var(--rivet);
  border-radius:2px;padding:8px 10px;font-size:13px;}
.find::placeholder{color:var(--dim);}
.find:focus{border-color:var(--gold-deep);outline:none;}
.list{overflow-y:auto;min-height:0;padding:0 8px 20px;}
.co{border-top:1px solid var(--rivet);padding:12px 8px 10px;cursor:pointer;}
.co:hover,.co.sel{background:var(--steel-2);}
.co.sel{box-shadow:inset 2px 0 0 var(--gold);}
.co-h{display:flex;align-items:baseline;gap:8px;}
.co-n{font-family:var(--deco);font-size:18px;color:var(--silver);flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.co-m{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--dim);white-space:nowrap;}
.role{display:block;text-decoration:none;color:inherit;padding:7px 0 2px;}
.role-t{font-size:13.5px;line-height:1.35;color:var(--silver);}
.role:hover .role-t{color:var(--gold);}
.role-m{font-family:var(--mono);font-size:10.5px;color:var(--haze);margin-top:3px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
.tag{font-family:var(--mono);font-size:9px;letter-spacing:.12em;text-transform:uppercase;padding:1px 6px;border:1px solid var(--rivet);border-radius:2px;color:var(--haze);}
.tag.c{border-color:var(--gold-deep);color:var(--gold);}
.tag.n{background:var(--gold);border-color:var(--gold);color:var(--gold-ink);font-weight:600;}
.empty{padding:40px 20px;text-align:center;color:var(--haze);line-height:1.6;}
.empty b{display:block;font-family:var(--marquee);font-size:22px;letter-spacing:.12em;color:var(--gold);font-weight:400;margin-bottom:8px;}

@media (max-width:900px){
  .shell{height:auto;}
  .body{grid-template-columns:1fr;}
  .scope{height:min(100vw,560px);}
  .side{border-left:0;border-top:1px solid var(--rivet);}
  .list{max-height:none;}
  .legend,.stamp{display:none;}
}
</style>
</head>
<body>
<script>window.__RADAR__ = __RADAR_DATA__;</script>
<div class="shell">
  <header class="mast">
    <div class="mark">ROTWANG<small>RADAR</small></div>
    <div class="stats" id="stats"></div>
  </header>
  <div class="body">
    <section class="scope" aria-label="Radar of watched companies">
      <canvas id="cv" role="img" aria-label="Radar display. Each point is a watched company; lit points have open roles in your field."></canvas>
      <div class="legend">
        <span><i style="background:#E6B23C;box-shadow:0 0 8px #E6B23C"></i>contract role open</span>
        <span><i style="background:#E4EAF0"></i>permanent role open — pitch as contract</span>
        <span><i style="background:#2C4256"></i>watched, nothing in your lane</span>
      </div>
      <div class="stamp" id="stamp"></div>
    </section>
    <aside class="side">
      <div class="filters">
        <button class="f" id="fNew" aria-pressed="false">New</button>
        <button class="f" id="fCon" aria-pressed="false">Contract</button>
        <button class="f" id="fRem" aria-pressed="false">Remote</button>
      </div>
      <input class="find" id="find" placeholder="Company, title, method…" aria-label="Filter">
      <div class="list" id="list"></div>
    </aside>
  </div>
</div>
<script>
"use strict";
var D = window.__RADAR__ || {companies: [], counts: {}, universe: 0};
var esc = function (s) { return String(s == null ? "" : s).replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;"); };
var F = {n: false, c: false, r: false, q: ""}, SEL = null;

function hash(s, seed) { var h = seed || 2166136261; for (var i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return (h >>> 0) / 4294967296; }

function visibleRoles(c) {
  return c.roles.filter(function (r) {
    if (F.n && !r["new"]) return false;
    if (F.c && !r.contract) return false;
    if (F.r && !r.remote) return false;
    if (F.q) { var b = (c.n + " " + r.title + " " + r.location).toLowerCase(); if (b.indexOf(F.q) < 0) return false; }
    return true;
  });
}

/* ---------- blip geometry: angle is stable per company; lit companies sit
   closer to the centre the better the fit; quiet ones drift on the rim ---------- */
/* Fit scores cluster (most land 60–85), so distance is scaled to this sweep's
   own spread: the best fit on the radar sits at the core, the weakest near the
   outer ring. Absolute scores are still shown in the list. */
var FITS = D.companies.filter(function (c) { return c.roles.length; })
  .map(function (c) { return c.roles.reduce(function (m, r) { return Math.max(m, r.fit); }, 0); });
var FMIN = FITS.length ? Math.min.apply(null, FITS) : 0, FMAX = FITS.length ? Math.max.apply(null, FITS) : 100;
var BLIPS = D.companies.map(function (c) {
  var best = c.roles.reduce(function (m, r) { return Math.max(m, r.fit); }, 0);
  var lit = c.roles.length > 0;
  var rel = FMAX > FMIN ? (best - FMIN) / (FMAX - FMIN) : 1;
  return {
    c: c, a: hash(c.k) * Math.PI * 2,
    r: lit ? 0.1 + 0.64 * (1 - rel) + 0.05 * (hash(c.k, 7) - 0.5) : 0.84 + 0.13 * hash(c.k, 99),
    lit: lit, best: best,
    contract: c.roles.some(function (r) { return r.contract; }),
    fresh: c.roles.some(function (r) { return r["new"]; })
  };
});

/* ---------- header ---------- */
(function () {
  var roles = 0, fresh = 0, con = 0, lit = 0;
  D.companies.forEach(function (c) { if (c.roles.length) lit++; c.roles.forEach(function (r) { roles++; if (r["new"]) fresh++; if (r.contract) con++; }); });
  var w = (D.counts || {}).watching || D.companies.length, blind = ((D.counts || {}).none || 0);
  document.getElementById("stats").innerHTML =
    '<span class="hot"><b>' + fresh + '</b>new</span>' +
    '<span class="hot"><b>' + con + '</b>contract</span>' +
    '<span><b>' + roles + '</b>roles</span>' +
    '<span><b>' + lit + '</b>lit</span>' +
    '<span><b>' + w + '</b>watched</span>' +
    '<span><b>' + (D.universe || 0) + '</b>universe</span>' +
    '<span><b>' + blind + '</b>blind</span>';
  var t = D.generated ? new Date(D.generated) : null;
  document.getElementById("stamp").innerHTML = t ? "last sweep<br>" + esc(t.toLocaleString()) : "";
})();

/* ---------- list ---------- */
function drawList() {
  var rows = D.companies.map(function (c) { return {c: c, rs: visibleRoles(c)}; })
    .filter(function (x) { return x.rs.length; })
    .sort(function (a, b) {
      var an = a.rs.some(function (r) { return r["new"]; }), bn = b.rs.some(function (r) { return r["new"]; });
      if (an !== bn) return an ? -1 : 1;
      var ac = a.rs.some(function (r) { return r.contract; }), bc = b.rs.some(function (r) { return r.contract; });
      if (ac !== bc) return ac ? -1 : 1;
      return b.rs[0].fit - a.rs[0].fit;
    });
  var el = document.getElementById("list");
  if (!rows.length) {
    el.innerHTML = D.companies.length
      ? '<div class="empty"><b>Quiet sky</b>Nothing matches these filters right now.</div>'
      : '<div class="empty"><b>No sweep yet</b>Run <code>python radar.py</code> to fill the radar.</div>';
    return;
  }
  el.innerHTML = rows.map(function (x) {
    var c = x.c;
    return '<div class="co' + (SEL === c.k ? " sel" : "") + '" data-k="' + esc(c.k) + '">' +
      '<div class="co-h"><span class="co-n">' + esc(c.n) + '</span><span class="co-m">' +
      (c.t ? c.t + " trial" + (c.t > 1 ? "s" : "") + " · " : "") + esc(c.ats) + '</span></div>' +
      x.rs.map(function (r) {
        return '<a class="role" href="' + esc(r.url) + '" target="_blank" rel="noreferrer">' +
          '<div class="role-t">' + esc(r.title) + '</div><div class="role-m">' +
          (r["new"] ? '<span class="tag n">new</span>' : "") +
          (r.contract ? '<span class="tag c">contract</span>' : '<span class="tag">permanent</span>') +
          '<span>fit ' + r.fit + '</span>' + (r.pay ? '<span>' + esc(r.pay) + '</span>' : "") +
          (r.location ? '<span>' + esc(r.location) + '</span>' : "") + '</div></a>';
      }).join("") + '</div>';
  }).join("");
  if (SEL) { var s = el.querySelector('[data-k="' + CSS.escape(SEL) + '"]'); if (s) s.scrollIntoView({block: "nearest"}); }
}
document.getElementById("list").addEventListener("click", function (e) {
  if (e.target.closest("a")) return;
  var co = e.target.closest(".co"); if (!co) return;
  SEL = SEL === co.dataset.k ? null : co.dataset.k; drawList();
});
function toggle(id, key) {
  var b = document.getElementById(id);
  b.onclick = function () { F[key] = !F[key]; b.setAttribute("aria-pressed", F[key]); drawList(); };
}
toggle("fNew", "n"); toggle("fCon", "c"); toggle("fRem", "r");
document.getElementById("find").oninput = function (e) { F.q = e.target.value.trim().toLowerCase(); drawList(); };

/* ---------- the scope ---------- */
var cv = document.getElementById("cv"), ctx = cv.getContext("2d"), dpr = Math.min(window.devicePixelRatio || 1, 2);
var W = 0, R = 0, C = 0;
function size() {
  var b = cv.getBoundingClientRect(); W = Math.floor(Math.min(b.width, b.height) * dpr);
  cv.width = W; cv.height = W; C = W / 2; R = W / 2 - 14 * dpr;
}
window.addEventListener("resize", size); size();
var still = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
var sweep = -Math.PI / 2, last = 0;

function blipShown(b) {
  if (!b.lit) return !(F.n || F.c || F.r || F.q);
  return visibleRoles(b.c).length > 0;
}

function frame(t) {
  var dt = last ? (t - last) / 1000 : 0; last = t;
  if (!still) sweep = (sweep + dt * (Math.PI * 2 / 5)) % (Math.PI * 2);
  ctx.clearRect(0, 0, W, W);

  // rings, crosshair, bearing ticks
  ctx.lineWidth = 1 * dpr;
  for (var i = 1; i <= 4; i++) {
    ctx.strokeStyle = i === 4 ? "rgba(124,150,172,.45)" : "rgba(44,66,86,.9)";
    ctx.beginPath(); ctx.arc(C, C, R * i / 4, 0, Math.PI * 2); ctx.stroke();
  }
  ctx.strokeStyle = "rgba(44,66,86,.7)";
  ctx.beginPath(); ctx.moveTo(C - R, C); ctx.lineTo(C + R, C); ctx.moveTo(C, C - R); ctx.lineTo(C, C + R); ctx.stroke();
  for (var d = 0; d < 360; d += 10) {
    var a = d * Math.PI / 180, l = d % 30 ? 5 : 10;
    ctx.strokeStyle = d % 30 ? "rgba(74,96,118,.6)" : "rgba(124,150,172,.7)";
    ctx.beginPath(); ctx.moveTo(C + Math.cos(a) * R, C + Math.sin(a) * R);
    ctx.lineTo(C + Math.cos(a) * (R - l * dpr), C + Math.sin(a) * (R - l * dpr)); ctx.stroke();
  }
  // ring labels
  ctx.fillStyle = "rgba(124,150,172,.55)"; ctx.font = (9 * dpr) + "px 'IBM Plex Mono', monospace";
  ctx.fillText("STRONG FIT", C + 4 * dpr, C - R * 0.25 + 12 * dpr);
  ctx.fillText("QUIET", C + 4 * dpr, C - R + 22 * dpr);

  // sweep trail
  if (!still) {
    for (var k = 0; k < 36; k++) {
      var a0 = sweep - k * 0.035, a1 = a0 - 0.035;
      ctx.fillStyle = "rgba(230,178,60," + (0.16 * (1 - k / 36)) + ")";
      ctx.beginPath(); ctx.moveTo(C, C); ctx.arc(C, C, R, a1, a0); ctx.closePath(); ctx.fill();
    }
    ctx.strokeStyle = "rgba(230,178,60,.85)"; ctx.lineWidth = 1.5 * dpr;
    ctx.beginPath(); ctx.moveTo(C, C); ctx.lineTo(C + Math.cos(sweep) * R, C + Math.sin(sweep) * R); ctx.stroke();
  }

  // blips — quiet ones first so lit ones draw on top
  var pulse = (Math.sin(t / 260) + 1) / 2;
  BLIPS.slice().sort(function (a, b) { return a.lit - b.lit; }).forEach(function (b) {
    if (!blipShown(b)) return;
    var x = C + Math.cos(b.a) * R * b.r, y = C + Math.sin(b.a) * R * b.r;
    b.x = x; b.y = y;
    var behind = ((sweep - b.a) % (Math.PI * 2) + Math.PI * 2) % (Math.PI * 2);
    var glow = still ? 0.6 : Math.max(0, 1 - behind / (Math.PI * 1.6));
    if (!b.lit) {
      ctx.fillStyle = "rgba(74,96,118," + (0.35 + 0.45 * glow) + ")";
      ctx.beginPath(); ctx.arc(x, y, 1.6 * dpr, 0, Math.PI * 2); ctx.fill();
      return;
    }
    var col = b.contract ? "230,178,60" : "228,234,240";
    var rad = (3.2 + 2.2 * (b.best / 100)) * dpr;
    ctx.shadowColor = "rgba(" + col + ",.9)"; ctx.shadowBlur = (6 + 14 * glow) * dpr;
    ctx.fillStyle = "rgba(" + col + "," + (0.55 + 0.45 * glow) + ")";
    ctx.beginPath(); ctx.arc(x, y, rad, 0, Math.PI * 2); ctx.fill();
    ctx.shadowBlur = 0;
    if (b.fresh) {
      ctx.strokeStyle = "rgba(230,178,60," + (0.25 + 0.6 * (1 - pulse)) + ")"; ctx.lineWidth = 1.2 * dpr;
      ctx.beginPath(); ctx.arc(x, y, rad + (4 + 9 * pulse) * dpr, 0, Math.PI * 2); ctx.stroke();
    }
    if (SEL === b.c.k) {
      ctx.strokeStyle = "rgba(230,178,60,1)"; ctx.lineWidth = 1.5 * dpr;
      ctx.strokeRect(x - 11 * dpr, y - 11 * dpr, 22 * dpr, 22 * dpr);
      ctx.fillStyle = "rgba(228,234,240,.95)"; ctx.font = (12 * dpr) + "px 'Poiret One', sans-serif";
      var label = b.c.n.length > 30 ? b.c.n.slice(0, 29) + "…" : b.c.n;
      var tx = x + 16 * dpr, tw = ctx.measureText(label).width;
      if (tx + tw > W - 8 * dpr) tx = x - 16 * dpr - tw;
      ctx.fillText(label, tx, y + 4 * dpr);
    }
  });
  if (!still) requestAnimationFrame(frame);
}
requestAnimationFrame(frame);
if (still) { /* redraw on interaction when motion is off */ }

cv.addEventListener("click", function (e) {
  var b = cv.getBoundingClientRect(), x = (e.clientX - b.left) * dpr, y = (e.clientY - b.top) * dpr;
  var best = null, bd = 16 * dpr;
  BLIPS.forEach(function (p) {
    if (!p.lit || p.x == null || !blipShown(p)) return;
    var d = Math.hypot(p.x - x, p.y - y); if (d < bd) { bd = d; best = p; }
  });
  SEL = best ? best.c.k : null; drawList();
  if (still) requestAnimationFrame(frame);
});
drawList();
</script>
</body>
</html>
'''

if __name__ == "__main__":
    log("run: " + " ".join(sys.argv[1:]))
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception:
        import traceback
        log("CRASH\n" + traceback.format_exc())
        print("\n  radar.py hit an error — the details are in radar_log.txt.")
        print("  Tell Claude to check the radar log.\n")
        sys.exit(1)
