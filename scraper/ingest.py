#!/usr/bin/env python3
"""Pull published jobs from Ashby, Greenhouse and Lever job boards into a local SQLite database.

Usage:
    python3 ingest.py                         # all boards in slugs.txt, companies.tsv, greenhouse.tsv, lever.tsv
    python3 ingest.py --sources ashby         # only some platforms (comma-separated)
    python3 ingest.py --retry-missing         # re-probe boards that 404'd before

Uses only the Python standard library. Personal, non-commercial use.
"""
import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

try:
    import fcntl
except ImportError:  # Windows: no run lock, which only matters with the scheduler
    fcntl = None

HERE = os.path.dirname(os.path.abspath(__file__))
API = os.environ.get("ASHBY_API_BASE", "https://api.ashbyhq.com/posting-api/job-board")
UA = "tech3000-job-index/1.0 (free, non-commercial job search)"
WORKERS = 6          # keep this modest; we are a guest on these APIs
PER_REQUEST_DELAY = 0.3
SCHEMA_VERSION = 3   # v2: search index keyed by jobs.rowid; v3: salary/level/years columns

SCHEMA = """
CREATE TABLE IF NOT EXISTS boards(
    slug TEXT PRIMARY KEY, status TEXT, job_count INTEGER, last_checked TEXT, name TEXT);
CREATE TABLE IF NOT EXISTS jobs(
    url TEXT PRIMARY KEY, slug TEXT, title TEXT, department TEXT, team TEXT,
    location TEXT, secondary_locations TEXT, is_remote INTEGER, workplace_type TEXT,
    employment_type TEXT, comp TEXT, description TEXT, published_at TEXT,
    apply_url TEXT, first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS runs(
    started TEXT, finished TEXT, boards_ok INTEGER, boards_total INTEGER, jobs INTEGER, note TEXT);
CREATE INDEX IF NOT EXISTS jobs_slug ON jobs(slug);
CREATE INDEX IF NOT EXISTS jobs_active ON jobs(active);
"""
FTS = "CREATE VIRTUAL TABLE IF NOT EXISTS jobs_fts USING fts5(title, department, team, location, description)"
NEW_JOB_COLUMNS = {"salary_min": "REAL", "salary_max": "REAL", "salary_currency": "TEXT",
                   "level": "TEXT", "years_min": "INTEGER"}


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_db(path):
    db = sqlite3.connect(path, timeout=60)
    db.execute("PRAGMA journal_mode=WAL")   # lets the site keep reading while a refresh writes
    db.executescript(SCHEMA)
    if "name" not in {r[1] for r in db.execute("PRAGMA table_info(boards)")}:
        db.execute("ALTER TABLE boards ADD COLUMN name TEXT")
    have = {r[1] for r in db.execute("PRAGMA table_info(jobs)")}
    for col, typ in NEW_JOB_COLUMNS.items():
        if col not in have:
            db.execute(f"ALTER TABLE jobs ADD COLUMN {col} {typ}")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version < 2:
        # Older databases keyed the search index by URL, which gets slow at scale. Rebuild it.
        db.execute("DROP TABLE IF EXISTS jobs_fts")
        db.execute(FTS)
        db.execute("INSERT INTO jobs_fts(rowid, title, department, team, location, description) "
                   "SELECT rowid, title, department, team, location || ' ' || secondary_locations, description "
                   "FROM jobs WHERE active = 1")
    db.execute(FTS)
    if version < 3:
        # Level can be filled straight away from titles; salary and years fill in on the next fetch.
        for rid, title, emp in db.execute("SELECT rowid, title, employment_type FROM jobs").fetchall():
            db.execute("UPDATE jobs SET level = ? WHERE rowid = ?", (classify_level(title, emp), rid))
    db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    db.commit()
    return db


# ---------------------------------------------------------------- field parsing

ANNUAL = {"1 YEAR": 1, "6 MONTH": 2, "3 MONTH": 4, "2 MONTH": 6, "1 MONTH": 12, "0.5 MONTH": 24,
          "2 WEEK": 26, "1 WEEK": 52, "1 DAY": 260, "1 HOUR": 2080}


def parse_salary(job):
    """Annualised (min, max, currency) from Ashby's structured compensation, or (None, None, '')."""
    comp = job.get("compensation") or {}
    parts = list(comp.get("summaryComponents") or [])
    if not parts:
        for tier in comp.get("compensationTiers") or []:
            parts += tier.get("components") or []
    lo = hi = None
    cur = ""
    for c in parts:
        if c.get("compensationType") != "Salary":
            continue
        mult = ANNUAL.get(str(c.get("interval") or "").upper())
        mn, mx = c.get("minValue"), c.get("maxValue")
        if not mult or (mn is None and mx is None):
            continue
        ccy = c.get("currencyCode") or ""
        if cur and ccy and ccy != cur:
            continue  # keep one currency per job
        mn = mn if mn is not None else mx
        mx = mx if mx is not None else mn
        try:
            mn, mx = float(mn) * mult, float(mx) * mult
        except (TypeError, ValueError):
            continue
        if mx <= 0 or mx > 5_000_000:
            continue
        cur = cur or ccy
        lo = mn if lo is None else min(lo, mn)
        hi = mx if hi is None else max(hi, mx)
    return lo, hi, cur


LEVEL_RULES = [
    ("intern", re.compile(r"\b(intern|interns|internship|co-?op|apprentice(ship)?)\b", re.I)),
    ("director", re.compile(r"\b(chief|ceo|cto|cfo|coo|cmo|cpo|ciso|vp|vice president|svp|evp|head of|"
                            r"director|general manager)\b", re.I)),
    ("staff", re.compile(r"\b(staff|principal|distinguished|lead|architect)\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr|iii|iv)\b", re.I)),
    ("entry", re.compile(r"\b(junior|jr|entry[- ]level|new grad(uate)?|graduate|early[- ]career|i)\b|"
                         r"^associate\b(?!.*\b(director|counsel|partner|principal|vice)\b)", re.I)),
]


def classify_level(title, employment_type=""):
    """Best guess at seniority from the title. Anything without a signal counts as 'mid'."""
    if (employment_type or "") == "Intern":
        return "intern"
    t = (title or "").replace("Sr.", "Senior").replace("Jr.", "Junior")
    for level, rx in LEVEL_RULES:
        if rx.search(t):
            return level
    return "mid"


NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
                "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fifteen": 15}
YEARS_RX = re.compile(
    r"\b(\d{1,2}|" + "|".join(NUMBER_WORDS) + r")\s*\+?\s*(?:(?:-|–|—|to)\s*\d{1,2}\s*)?\+?\s*"
    r"(?:or more\s+|plus\s+)?years?\b([^.\n;]{0,70})", re.I)


def years_required(description):
    """Minimum years of experience mentioned in the description, or None."""
    text = (description or "")[:30000]
    for m in YEARS_RX.finditer(text):
        after = m.group(2).lower()
        if "experience" not in after and "experience" not in text[max(0, m.start() - 30):m.start()].lower():
            continue
        raw = m.group(1).lower()
        n = NUMBER_WORDS.get(raw) if raw in NUMBER_WORDS else int(raw)
        if n is not None and 0 <= n <= 20:
            return n
    return None


# ---------------------------------------------------------------- fetching and storing

# ---------------------------------------------------------------- sources
#
# Board keys: an Ashby board is just its name ("openai"); other platforms are prefixed:
# "greenhouse:stripe", "lever:netflix" (Lever's EU-hosted boards: "lever:eu/acme").

GREENHOUSE_API = os.environ.get("GREENHOUSE_API_BASE", "https://boards-api.greenhouse.io/v1/boards")
LEVER_API = os.environ.get("LEVER_API_BASE", "https://api.lever.co/v0/postings")
LEVER_EU_API = os.environ.get("LEVER_EU_API_BASE", "https://api.eu.lever.co/v0/postings")
SOURCES = ("ashby", "greenhouse", "lever")
LIST_FILES = {"ashby": "companies.tsv", "greenhouse": "greenhouse.tsv", "lever": "lever.tsv"}
STORE_DESCRIPTIONS = True   # the CI build turns this off (--no-descriptions) to keep the database small


def split_key(key):
    head, _, rest = key.partition(":")
    if rest and head in ("greenhouse", "lever"):
        return head, rest
    return "ashby", key


def make_key(source, slug):
    return slug if source == "ashby" else f"{source}:{slug}"


def get_json(url):
    """GET a JSON document with retries. Returns (status, data); status is 'ok', 'not_found' or 'error:...'."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(3):
        try:
            time.sleep(PER_REQUEST_DELAY)
            with urllib.request.urlopen(req, timeout=60) as resp:
                return "ok", json.load(resp)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "not_found", None
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                wait = int(e.headers.get("Retry-After", 0) or 0) or 5 * (attempt + 1)
                time.sleep(min(wait, 60))
                continue
            return f"error:http_{e.code}", None
        except Exception as e:  # network hiccup, bad JSON, etc.
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            return f"error:{type(e).__name__}", None
    return "error:unknown", None


def fetch_board(slug):
    """Ashby: (status, raw jobs). Kept for discover.py and tests."""
    status, data = get_json(f"{API}/{urllib.parse.quote(slug, safe='')}?includeCompensation=true")
    return status, (data or {}).get("jobs", []) if status == "ok" else []


def normalize_all(fn, items):
    """Normalize each job, skipping any single malformed one instead of losing the whole board."""
    out = []
    for item in items:
        try:
            out.append(fn(item))
        except Exception:
            continue
    return out


def fetch(key):
    """Fetch any board. Returns (status, [normalized job records])."""
    source, slug = split_key(key)
    try:
        if source == "ashby":
            status, jobs = fetch_board(slug)
            return status, normalize_all(normalize_ashby, [j for j in jobs if j.get("isListed") is not False])
        if source == "greenhouse":
            status, data = get_json(f"{GREENHOUSE_API}/{urllib.parse.quote(slug, safe='')}/jobs?content=true")
            return status, normalize_all(normalize_greenhouse, (data or {}).get("jobs", [])) if status == "ok" else []
        base, name = (LEVER_EU_API, slug[3:]) if slug.startswith("eu/") else (LEVER_API, slug)
        status, data = get_json(f"{base}/{urllib.parse.quote(name, safe='')}?mode=json")
        return status, normalize_all(normalize_lever, data or []) if status == "ok" and isinstance(data, list) else []
    except Exception as e:   # one malformed board must never stop the whole run
        return f"error:parse_{type(e).__name__}", []


TAG = re.compile(r"<[^>]+>")


def plain(text):
    """HTML (possibly entity-escaped, as Greenhouse sends it) to plain text."""
    text = html.unescape(html.unescape(text or ""))
    return " ".join(html.unescape(TAG.sub(" ", text)).split())


def infer_workplace(location, title, desc):
    head = f"{location} {title}".lower()
    if "hybrid" in head:
        return "Hybrid"
    if "remote" in head:
        return "Remote"
    d = (desc or "")[:6000].lower()
    if re.search(r"\bhybrid\b", d):
        return "Hybrid"
    if re.search(r"\b(fully remote|100% remote|remote[- ]first|remote position|remote role|work from anywhere)\b", d):
        return "Remote"
    return "OnSite" if location else ""


CCY_SYMBOL = {"$": "USD", "US$": "USD", "CA$": "CAD", "C$": "CAD", "A$": "AUD", "£": "GBP", "€": "EUR"}
CCY_CODES = {"USD", "CAD", "GBP", "EUR", "AUD", "SGD", "CHF", "INR", "NZD"}
PAY_RX = re.compile(
    r"(?P<c1>US\$|CA\$|C\$|A\$|\$|£|€|\b(?:USD|CAD|GBP|EUR|AUD|SGD|CHF|NZD)\b)\s?"
    r"(?P<a>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(?P<ka>[kK])?\s*"
    r"(?:-|–|—|to)\s*"
    r"(?P<c2>US\$|CA\$|C\$|A\$|\$|£|€)?\s?(?P<b>\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?(?P<kb>[kK])?"
    r"(?:\s*(?P<c3>USD|CAD|GBP|EUR|AUD|SGD|CHF|NZD)\b)?"
    r"(?P<tail>[^.\n]{0,40})")
PAY_CONTEXT = re.compile(r"salary|compensation|pay|base|wage|range|ote|earn", re.I)


def parse_salary_text(text):
    """Yearly (min, max, currency) from a pay range written in a job description, or (None, None, '')."""
    t = text or ""
    for m in PAY_RX.finditer(t):
        if not PAY_CONTEXT.search(t[max(0, m.start() - 250):m.start()]):
            continue
        try:
            a = float(m.group("a").replace(",", "")) * (1000 if m.group("ka") or m.group("kb") else 1)
            b = float(m.group("b").replace(",", "")) * (1000 if m.group("kb") else 1)
        except ValueError:
            continue
        tail = (m.group("tail") or "").lower()
        if re.search(r"\b(hour|hr|hourly)\b", tail) and 10 <= a <= b <= 500:
            a, b = a * 2080, b * 2080
        if not (15000 <= a <= b <= 2_000_000) or b > 4 * a:
            continue
        code = m.group("c3") or (m.group("c1") if m.group("c1") in CCY_CODES else "")
        ccy = code or CCY_SYMBOL.get(m.group("c1"), "USD")
        if not code and m.group("c1") == "$":
            window = t[max(0, m.start() - 300):m.end() + 60]
            if re.search(r"\bCAD\b|Canadian", window):
                ccy = "CAD"
        return a, b, ccy
    return None, None, ""


def record(**r):
    r.setdefault("secondary_locations", "")
    r.setdefault("team", "")
    r.setdefault("comp", "")
    r["level"] = classify_level(r["title"], r["employment_type"])
    r["years_min"] = years_required(r["desc"])
    r["apply_url"] = r.get("apply_url") or r["url"]
    return r


def normalize_ashby(j):
    smin, smax, ccy = parse_salary(j)
    desc = j.get("descriptionPlain") or ""
    return record(
        url=j.get("jobUrl") or "", title=j.get("title") or "", department=j.get("department") or "",
        team=j.get("team") or "", location=j.get("location") or "", secondary_locations=secondary(j),
        is_remote=1 if j.get("isRemote") else 0, workplace_type=j.get("workplaceType") or "",
        employment_type=j.get("employmentType") or "", comp=comp_text(j), published_at=j.get("publishedAt") or "",
        apply_url=j.get("applyUrl"), salary_min=smin, salary_max=smax, salary_currency=ccy, desc=desc)


def normalize_greenhouse(j):
    desc = plain(j.get("content"))
    loc = ((j.get("location") or {}).get("name") or "").strip()
    title = j.get("title") or ""
    wt = infer_workplace(loc, title, desc)
    smin, smax, ccy = parse_salary_text(desc)
    depts = [d.get("name") for d in j.get("departments") or [] if d.get("name")]
    return record(
        url=j.get("absolute_url") or "", title=title, department=depts[0] if depts else "",
        team=depts[1] if len(depts) > 1 else "", location=loc, is_remote=1 if wt == "Remote" else 0,
        workplace_type=wt, employment_type="Intern" if re.search(r"\bintern(ship)?\b", title, re.I) else "",
        published_at=j.get("first_published") or j.get("updated_at") or "",
        salary_min=smin, salary_max=smax, salary_currency=ccy, desc=desc)


LEVER_WORK = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "OnSite", "on-site": "OnSite"}
LEVER_INTERVAL = {"per-year-salary": 1, "per-month-salary": 12, "per-week-salary": 52,
                  "per-day-wage": 260, "per-hour-wage": 2080}


def lever_employment(commitment):
    c = (commitment or "").lower()
    if "intern" in c:
        return "Intern"
    if "part" in c:
        return "PartTime"
    if "contract" in c or "freelance" in c:
        return "Contract"
    if "temp" in c:
        return "Temporary"
    return "FullTime" if "full" in c else ""


def normalize_lever(p):
    cats = p.get("categories") or {}
    title = p.get("text") or ""
    loc = (cats.get("location") or "").strip()
    others = [l for l in cats.get("allLocations") or [] if l and l != loc]
    parts = [p.get("descriptionPlain") or ""]
    for lst in p.get("lists") or []:
        parts.append(f"{lst.get('text') or ''}: {plain(lst.get('content'))}")
    parts.append(p.get("additionalPlain") or "")
    desc = " ".join(x for x in parts if x)
    wt = LEVER_WORK.get((p.get("workplaceType") or "").lower()) or infer_workplace(loc, title, desc)
    smin = smax = None
    ccy = ""
    sr = p.get("salaryRange") or {}
    mult = LEVER_INTERVAL.get(sr.get("interval") or "")
    if mult and sr.get("min") is not None and sr.get("max") is not None:
        try:
            smin, smax, ccy = float(sr["min"]) * mult, float(sr["max"]) * mult, sr.get("currency") or "USD"
        except (TypeError, ValueError):
            smin = smax = None
    if smin is None:
        smin, smax, ccy = parse_salary_text(desc)
    created = p.get("createdAt")
    try:
        published = datetime.fromtimestamp(created / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OverflowError, OSError):
        published = ""
    emp = lever_employment(cats.get("commitment"))
    return record(
        url=p.get("hostedUrl") or "", title=title, department=cats.get("department") or cats.get("team") or "",
        team=cats.get("team") or "", location=loc, secondary_locations="; ".join(others),
        is_remote=1 if wt == "Remote" else 0, workplace_type=wt, employment_type=emp,
        published_at=published, apply_url=p.get("applyUrl"),
        salary_min=smin, salary_max=smax, salary_currency=ccy, desc=desc)


def comp_text(job):
    c = job.get("compensation") or {}
    return c.get("scrapeableCompensationSalarySummary") or c.get("compensationTierSummary") or ""


def secondary(job):
    return "; ".join(s.get("location", "") for s in job.get("secondaryLocations") or [] if s.get("location"))


COLUMNS = ["url", "slug", "title", "department", "team", "location", "secondary_locations", "is_remote",
           "workplace_type", "employment_type", "comp", "published_at", "apply_url",
           "salary_min", "salary_max", "salary_currency", "level", "years_min"]
UPSERT = (
    f"INSERT INTO jobs({', '.join(COLUMNS)}, description, first_seen, last_seen, active) "
    f"VALUES ({', '.join('?' * len(COLUMNS))}, '', ?, ?, 1) "
    "ON CONFLICT(url) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in COLUMNS if c != "url")
    + ", description='', last_seen=excluded.last_seen, active=1"
)


def store_board(db, key, status, records, name=""):
    ts = now()
    count = 0
    if status == "ok":
        seen = set()
        for rec in records:
            url = rec.get("url")
            if not url or url in seen:
                continue
            seen.add(url)
            count += 1
            rec["slug"] = key
            # The description lives only in the search index (jobs_fts) to keep the file small.
            db.execute(UPSERT, [rec[c] for c in COLUMNS] + [ts, ts])
            rid = db.execute("SELECT rowid FROM jobs WHERE url = ?", (url,)).fetchone()[0]
            db.execute("DELETE FROM jobs_fts WHERE rowid = ?", (rid,))
            db.execute(
                "INSERT INTO jobs_fts(rowid, title, department, team, location, description) VALUES (?,?,?,?,?,?)",
                (rid, rec["title"], rec["department"], rec["team"],
                 rec["location"] + " " + rec["secondary_locations"], rec["desc"] if STORE_DESCRIPTIONS else ""),
            )
        # Anything from this board we did not see this time has been taken down.
        # Only done after a successful fetch, so an outage never wipes data.
        for rid, u in db.execute("SELECT rowid, url FROM jobs WHERE slug = ? AND active = 1", (key,)).fetchall():
            if u not in seen:
                db.execute("UPDATE jobs SET active = 0 WHERE rowid = ?", (rid,))
                db.execute("DELETE FROM jobs_fts WHERE rowid = ?", (rid,))
    db.execute(
        "INSERT INTO boards(slug, status, job_count, last_checked, name) VALUES (?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET status=excluded.status, "
        "job_count=CASE WHEN excluded.status = 'ok' THEN excluded.job_count ELSE boards.job_count END, "
        "last_checked=excluded.last_checked, name=COALESCE(NULLIF(excluded.name, ''), boards.name)",
        (key, status, count, ts, name),
    )
    db.commit()
    return count


def load_slugs(*paths):
    """Read board lists. Each line is `slug` or `slug<TAB>Company Name`; # starts a comment.
    Duplicates are merged case-insensitively: the first spelling wins, and a name from
    any later line fills in a missing one."""
    out, index = [], {}
    for path in paths:
        if not path or not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.lstrip().startswith("#"):
                    continue
                parts = line.rstrip("\n").split("\t")
                slug = parts[0].split(" #", 1)[0].strip()
                name = parts[1].strip() if len(parts) > 1 else ""
                if not slug:
                    continue
                key = slug.lower()
                if key not in index:
                    index[key] = len(out)
                    out.append([slug, name])
                elif name and not out[index[key]][1]:
                    out[index[key]][1] = name
    return [tuple(b) for b in out]


def take_lock(db_path, wait=0.0):
    """Make sure only one refresh runs at a time (scheduler + Refresh button). Returns the handle or None.
    `wait` retries for that many seconds, so a brief status check from the site can't block a real run."""
    if fcntl is None:
        return True
    handle = open(db_path + ".lock", "w")
    deadline = time.time() + wait
    while True:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.time() >= deadline:
                handle.close()
                return None
            time.sleep(0.25)


def main():
    try:
        sys.stdout.reconfigure(line_buffering=True)   # progress shows up live in log files
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--slugs", default=os.path.join(HERE, "slugs.txt"), help="your own board list")
    ap.add_argument("--sources", default=",".join(SOURCES), help="platforms to fetch, e.g. ashby,greenhouse")
    ap.add_argument("--no-descriptions", action="store_true",
                    help="don't keep job descriptions in the search index (smaller database)")
    ap.add_argument("--db", default=os.path.join(HERE, "jobs.db"))
    ap.add_argument("--workers", type=int, default=WORKERS, help=f"parallel requests (default {WORKERS})")
    ap.add_argument("--retry-missing", action="store_true", help="re-probe boards that 404'd before")
    args = ap.parse_args()

    lock = take_lock(args.db, wait=5)
    if not lock:
        print("Another refresh is already running; nothing to do.")
        return 0

    started = now()
    print(f"[{started}] Refresh started.")
    global STORE_DESCRIPTIONS
    STORE_DESCRIPTIONS = not args.no_descriptions
    wanted = {x.strip() for x in args.sources.split(",") if x.strip()}
    db = open_db(args.db)
    boards, seen_keys = [], set()
    for slug, name in load_slugs(args.slugs):          # your own list; may hold any platform's keys
        if split_key(slug)[0] in wanted and slug.lower() not in seen_keys:
            seen_keys.add(slug.lower())
            boards.append((slug, name))
    for source in SOURCES:
        if source not in wanted:
            continue
        for slug, name in load_slugs(os.path.join(HERE, LIST_FILES[source])):
            key = make_key(source, slug)
            if key.lower() not in seen_keys:
                seen_keys.add(key.lower())
                boards.append((key, name))
    if not args.retry_missing:
        missing = {r[0] for r in db.execute("SELECT slug FROM boards WHERE status = 'not_found'")}
        before = len(boards)
        boards = [b for b in boards if b[0] not in missing]
        if before - len(boards):
            print(f"Skipping {before - len(boards)} board(s) that 404'd earlier (use --retry-missing to retry).")

    slugs = [b[0] for b in boards]
    minutes = max(1, round(len(slugs) * 1.0 / args.workers / 60))
    print(f"Fetching {len(slugs)} board(s) with {args.workers} workers, roughly {minutes} min. "
          "Safe to stop (Ctrl+C) and rerun.")
    total = ok = done = 0
    problems = []
    pool = ThreadPoolExecutor(max_workers=args.workers)
    try:
        for (slug, name), (status, jobs) in zip(boards, pool.map(fetch, slugs)):
            n = store_board(db, slug, status, jobs, name)
            done += 1
            if status == "ok":
                ok += 1
                total += n
            else:
                problems.append((slug, status))
            if len(slugs) <= 25:
                print(f"  {slug}: {n} jobs" if status == "ok" else f"  {slug}: {status}")
            elif done % 1000 == 0 or done == len(slugs):
                print(f"  {done}/{len(slugs)} boards, {total} jobs so far")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)   # so Ctrl+C stops promptly
        note = "complete" if done == len(slugs) else "stopped early"
        db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?)", (started, now(), ok, len(slugs), total, note))
        db.commit()
    if problems and len(slugs) > 25:
        nf = sum(1 for _, s in problems if s == "not_found")
        errs = [p for p in problems if p[1] != "not_found"]
        msg = f"{nf} board(s) no longer exist (skipped next time); {len(errs)} failed"
        print(msg + (": " + ", ".join(f"{s} ({e})" for s, e in errs[:10]) if errs else "."))
    active = db.execute("SELECT COUNT(*) FROM jobs WHERE active = 1").fetchone()[0]
    print(f"[{now()}] Done. {ok}/{len(slugs)} boards ok, {total} jobs this run, {active} active jobs.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped. Everything fetched so far is saved; rerun to continue.")
