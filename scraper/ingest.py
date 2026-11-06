#!/usr/bin/env python3
"""Pull published jobs from Ashby public job boards into a local SQLite database.

Usage:
    python3 ingest.py                      # reads slugs.txt + companies.tsv, writes jobs.db
    python3 ingest.py --companies ''       # only your own slugs.txt
    python3 ingest.py --retry-missing      # re-probe boards that 404'd before

Uses only the Python standard library. Personal, non-commercial use.
"""
import argparse
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
UA = "ashby-jobs-personal-index/0.3 (personal, non-commercial)"
WORKERS = 6          # keep this modest; we are a guest on Ashby's API
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

def fetch_board(slug):
    """Return (status, jobs). status is 'ok', 'not_found' or 'error:<detail>'."""
    url = f"{API}/{urllib.parse.quote(slug, safe='')}?includeCompensation=true"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    for attempt in range(3):
        try:
            time.sleep(PER_REQUEST_DELAY)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.load(resp)
            return "ok", data.get("jobs", [])
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "not_found", []
            if e.code in (429, 500, 502, 503, 504) and attempt < 2:
                wait = int(e.headers.get("Retry-After", 0) or 0) or 5 * (attempt + 1)
                time.sleep(min(wait, 60))
                continue
            return f"error:http_{e.code}", []
        except Exception as e:  # network hiccup, bad JSON, etc.
            if attempt < 2:
                time.sleep(3 * (attempt + 1))
                continue
            return f"error:{type(e).__name__}", []
    return "error:unknown", []


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


def store_board(db, slug, status, jobs, name=""):
    ts = now()
    count = 0
    if status == "ok":
        seen = set()
        for j in jobs:
            # isListed=false means "direct link only": respect that and skip.
            if j.get("isListed") is False:
                continue
            url = j.get("jobUrl")
            if not url or url in seen:
                continue
            seen.add(url)
            count += 1
            desc = j.get("descriptionPlain") or ""
            smin, smax, ccy = parse_salary(j)
            rec = {
                "url": url, "slug": slug, "title": j.get("title") or "", "department": j.get("department") or "",
                "team": j.get("team") or "", "location": j.get("location") or "",
                "secondary_locations": secondary(j), "is_remote": 1 if j.get("isRemote") else 0,
                "workplace_type": j.get("workplaceType") or "", "employment_type": j.get("employmentType") or "",
                "comp": comp_text(j), "published_at": j.get("publishedAt") or "",
                "apply_url": j.get("applyUrl") or url, "salary_min": smin, "salary_max": smax,
                "salary_currency": ccy, "level": classify_level(j.get("title"), j.get("employmentType")),
                "years_min": years_required(desc),
            }
            # The description lives only in the search index (jobs_fts) to keep the file small.
            db.execute(UPSERT, [rec[c] for c in COLUMNS] + [ts, ts])
            rid = db.execute("SELECT rowid FROM jobs WHERE url = ?", (url,)).fetchone()[0]
            db.execute("DELETE FROM jobs_fts WHERE rowid = ?", (rid,))
            db.execute(
                "INSERT INTO jobs_fts(rowid, title, department, team, location, description) VALUES (?,?,?,?,?,?)",
                (rid, rec["title"], rec["department"], rec["team"],
                 rec["location"] + " " + rec["secondary_locations"], desc),
            )
        # Anything from this board we did not see this time has been taken down.
        # Only done after a successful fetch, so an outage never wipes data.
        for rid, u in db.execute("SELECT rowid, url FROM jobs WHERE slug = ? AND active = 1", (slug,)).fetchall():
            if u not in seen:
                db.execute("UPDATE jobs SET active = 0 WHERE rowid = ?", (rid,))
                db.execute("DELETE FROM jobs_fts WHERE rowid = ?", (rid,))
    db.execute(
        "INSERT INTO boards(slug, status, job_count, last_checked, name) VALUES (?,?,?,?,?) "
        "ON CONFLICT(slug) DO UPDATE SET status=excluded.status, "
        "job_count=CASE WHEN excluded.status = 'ok' THEN excluded.job_count ELSE boards.job_count END, "
        "last_checked=excluded.last_checked, name=COALESCE(NULLIF(excluded.name, ''), boards.name)",
        (slug, status, count, ts, name),
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
    ap.add_argument("--companies", default=os.path.join(HERE, "companies.tsv"),
                    help="community board list (use --companies '' to skip it)")
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
    db = open_db(args.db)
    boards = load_slugs(args.slugs, args.companies)
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
        for (slug, name), (status, jobs) in zip(boards, pool.map(fetch_board, slugs)):
            n = store_board(db, slug, status, jobs, name)
            done += 1
            if status == "ok":
                ok += 1
                total += n
            else:
                problems.append((slug, status))
            if len(slugs) <= 25:
                print(f"  {slug}: {n} jobs" if status == "ok" else f"  {slug}: {status}")
            elif done % 250 == 0 or done == len(slugs):
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
