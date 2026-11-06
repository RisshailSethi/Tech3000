#!/usr/bin/env python3
"""Find companies that use Ashby and add new ones to companies.tsv (runs weekly via schedule.py).

Usage:
    python3 update_companies.py           # look for companies added since the last run
    python3 update_companies.py --full    # rescan every source from the beginning (slow, ~15+ min)

Where candidates come from (all free, no account needed):
  * the community list at github.com/AnshumanAtrey/ashby-jobs-search, in case it gets updated
  * the Wayback Machine's index of jobs.ashbyhq.com pages
  * Hacker News comments that link to jobs.ashbyhq.com ("Who's hiring" threads)
  * public new-grad / internship job lists on GitHub that link to Ashby boards

Each new name is checked against its live Ashby board page: it is kept only if a real (non-demo)
company owns it, under the board name Ashby itself uses. Names that turn out not to be companies
are remembered, so they aren't checked again. Existing entries are never removed; closed boards
are simply skipped by ingest.py.

The approach follows scripts/refresh_companies.py in github.com/AnshumanAtrey/ashby-jobs-search
(MIT License, Copyright (c) 2026 Anshuman Atrey), rewritten here without third-party packages.
"""
import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
COMPANIES = os.path.join(HERE, "companies.tsv")
STATE = os.path.join(HERE, "discovery_state.json")
UA = "Mozilla/5.0 (compatible; ashby-jobs-personal-index/0.4; personal, non-commercial)"

# Overridable for testing.
UPSTREAM = os.environ.get("ASHBY_UPSTREAM_URL",
                          "https://raw.githubusercontent.com/AnshumanAtrey/ashby-jobs-search/main/src/companies.tsv")
WAYBACK = os.environ.get("ASHBY_WAYBACK_URL", "https://web.archive.org/cdx/search/cdx")
HN = os.environ.get("ASHBY_HN_URL", "https://hn.algolia.com/api/v1/search_by_date")
GITHUB_RAW = os.environ.get("ASHBY_GITHUB_RAW", "https://raw.githubusercontent.com")
BOARD_PAGE = os.environ.get("ASHBY_BOARD_URL", "https://jobs.ashbyhq.com")
POSTING_API = os.environ.get("ASHBY_API_BASE", "https://api.ashbyhq.com/posting-api/job-board")

GITHUB_LISTS = [
    "SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "speedyapply/2026-SWE-College-Jobs/main/README.md",
    "speedyapply/2027-SWE-College-Jobs/main/README.md",
    "vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json",
    "vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
]
# The community list already covers everything up to its build date, so a first run starts here.
FIRST_RUN_FROM = datetime(2026, 9, 1, tzinfo=timezone.utc)
FULL_FROM = datetime(2020, 1, 1, tzinfo=timezone.utc)
LINK = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9._%~&-]{0,99})", re.I)
NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~ &-]{0,99}")
APP_DATA = "window.__appData = "
CREDIT = (
    "# Community list of Ashby job boards: board name<TAB>company name.\n"
    "# Source: https://github.com/AnshumanAtrey/ashby-jobs-search (src/companies.tsv)\n"
    "# MIT License, Copyright (c) 2026 Anshuman Atrey. New boards are added by update_companies.py.\n"
)


def log(msg):
    print(msg, flush=True)


def get(url, params=None, timeout=60, tries=3):
    """GET with retries. Returns (status, text); status 0 means the request failed."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(5 * (attempt + 1))
                continue
            return e.code, ""
        except Exception:
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            return 0, ""
    return 0, ""


def first_part(path):
    """'/acme/123?x' -> 'acme'. None if it doesn't look like a board name."""
    part = urllib.parse.unquote(path.strip("/").split("/")[0].split("?")[0].split("#")[0]).strip().rstrip(".")
    return part if part and NAME_OK.fullmatch(part) else None


def links_in(text):
    out = set()
    for m in LINK.finditer(html.unescape(text or "")):
        part = first_part(m.group(1))
        if part:
            out.add(part)
    return out


# ---------------------------------------------------------------- sources

def from_upstream():
    status, text = get(UPSTREAM)
    if status != 200:
        raise RuntimeError(f"HTTP {status}" if status else "couldn't connect")
    rows = {}
    for line in text.splitlines():
        if line.strip() and not line.startswith("#") and "\t" in line:
            slug, name = line.split("\t", 1)
            rows[slug.strip()] = name.strip()
    if len(rows) < 500:
        raise RuntimeError(f"list looks wrong ({len(rows)} rows)")
    return rows   # already verified upstream: slug -> name


def from_wayback(since):
    found, key = set(), None
    for _ in range(50):
        params = {"url": "jobs.ashbyhq.com/", "matchType": "prefix", "fl": "original", "collapse": "urlkey",
                  "limit": "150000", "showResumeKey": "true", "from": since.strftime("%Y%m%d%H%M%S")}
        if key:
            params["resumeKey"] = key
        status, body = get(WAYBACK, params, timeout=300)
        if status != 200:
            raise RuntimeError(f"HTTP {status}" if status else "couldn't connect")
        rows, _, rest = body.rstrip("\n").partition("\n\n")
        for line in rows.splitlines():
            part = first_part(urllib.parse.urlparse(line.strip()).path)
            if part:
                found.add(part)
        key = rest.strip() or None
        if not key or not rows.strip():
            break
    return found


def from_hacker_news(since):
    found = set()
    start, step, end = int(since.timestamp()), 86400 * 60, int(time.time())
    while start < end:
        for page in range(5):
            params = {"query": "ashbyhq", "tags": "comment", "hitsPerPage": 1000, "page": page,
                      "numericFilters": f"created_at_i>{start},created_at_i<={start + step}"}
            status, body = get(HN, params)
            if status != 200:
                raise RuntimeError(f"HTTP {status}" if status else "couldn't connect")
            data = json.loads(body)
            for hit in data.get("hits", []):
                found |= links_in(hit.get("comment_text"))
            if page >= data.get("nbPages", 1) - 1:
                break
        start += step
    return found


def from_github_lists():
    found, ok = set(), 0
    for path in GITHUB_LISTS:
        status, body = get(f"{GITHUB_RAW}/{path}", tries=2)
        if status == 200:
            ok += 1
            found |= links_in(body)
    if not ok:
        raise RuntimeError("no list could be downloaded")
    return found


# ---------------------------------------------------------------- verification

def verify(candidate):
    """Returns ('ok', slug, name), ('no', candidate, None) or ('error', candidate, None)."""
    time.sleep(0.2)
    status, page = get(f"{BOARD_PAGE}/{urllib.parse.quote(candidate, safe='')}", timeout=30)
    if status == 404:
        return "no", candidate, None
    if status == 200:
        start = page.find(APP_DATA)
        if start >= 0:
            try:
                data, _ = json.JSONDecoder().raw_decode(page, start + len(APP_DATA))
            except ValueError:
                data = None
            if isinstance(data, dict):
                org = data.get("organization")
                if not isinstance(org, dict) or org.get("isDemoOrg"):
                    return "no", candidate, None
                slug = org.get("hostedJobsPageSlug") or candidate
                return "ok", slug, (org.get("name") or "").strip() or slug
    # Page missing or its format changed: fall back to the job postings API, which 404s for unknown names.
    status, _ = get(f"{POSTING_API}/{urllib.parse.quote(candidate, safe='')}", timeout=30)
    if status == 200:
        return "ok", candidate, candidate
    if status == 404:
        return "no", candidate, None
    return "error", candidate, None


# ---------------------------------------------------------------- state and files

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def known_slugs():
    sys.path.insert(0, HERE)
    from ingest import load_slugs
    return {s.lower() for s, _ in load_slugs(os.path.join(HERE, "slugs.txt"), COMPANIES)}


def parse_time(value, default):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--full", action="store_true", help="rescan all sources from 2020 and recheck rejected names")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    t0 = time.time()
    run_started = datetime.now(timezone.utc)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    log(f"[{stamp}] Looking for new Ashby companies...")
    state = load_state()
    rejected = set() if args.full else {r.lower() for r in state.get("rejected", [])}
    aliases = {a.lower() for a in state.get("aliases", [])}   # names that resolved to a different board name
    known = (known_slugs() if os.path.exists(COMPANIES) else set()) | aliases

    # 1. The community list: already verified, so new rows are added directly.
    new_rows = {}
    try:
        for slug, name in from_upstream().items():
            if slug.lower() not in known:
                new_rows[slug.lower()] = (slug, name)
        log(f"  community list: {len(new_rows)} new")
    except Exception as e:
        log(f"  community list: skipped ({e})")

    # 2. Wayback, Hacker News and GitHub lists: candidates that need checking.
    candidates = {}
    cursors = {}
    for name, key, fn in [("Wayback Machine", "wayback_from", from_wayback),
                          ("Hacker News", "hn_from", from_hacker_news),
                          ("GitHub job lists", None, lambda _since: from_github_lists())]:
        since = FULL_FROM if args.full else parse_time(state.get(key), FIRST_RUN_FROM) if key else None
        try:
            found = fn(since)
            fresh = {c for c in found if c.lower() not in known and c.lower() not in rejected
                     and c.lower() not in new_rows}
            for c in fresh:
                candidates.setdefault(c.lower(), c)
            if key:
                cursors[key] = run_started.isoformat()   # only advanced when the source worked
            log(f"  {name}: {len(found)} names seen, {len(fresh)} not yet known"
                + (f" (since {since:%Y-%m-%d})" if since else ""))
        except Exception as e:
            log(f"  {name}: skipped this time ({e}); will retry from the same point next run")

    # Names that couldn't be checked last time (network trouble) get another go.
    for c in state.get("retry", []):
        if c.lower() not in known and c.lower() not in rejected and c.lower() not in new_rows:
            candidates.setdefault(c.lower(), c)

    # 3. Check each unknown name against Ashby.
    checked = errors = 0
    retry = []
    if candidates:
        log(f"  checking {len(candidates)} name(s) against Ashby...")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            names = sorted(candidates.values())
            for cand, (result, slug, cname) in zip(names, pool.map(verify, names)):
                checked += 1
                if result == "ok":
                    if cand.lower() != slug.lower():
                        aliases.add(cand.lower())
                    if slug.lower() not in known and slug.lower() not in new_rows:
                        new_rows[slug.lower()] = (slug, cname)
                elif result == "no":
                    rejected.add(slug.lower())
                else:
                    errors += 1
                    retry.append(slug)

    # 4. Save.
    if new_rows:
        if not os.path.exists(COMPANIES):
            with open(COMPANIES, "w", encoding="utf-8") as f:
                f.write(CREDIT)
        with open(COMPANIES, "a", encoding="utf-8") as f:
            f.write(f"# added {stamp} by update_companies.py\n")
            for slug, cname in sorted(new_rows.values(), key=lambda r: r[0].lower()):
                f.write(f"{slug}\t{cname.replace(chr(9), ' ')}\n")
    state.update(cursors)
    state["rejected"] = sorted(rejected)
    state["retry"] = sorted(retry)
    state["aliases"] = sorted(aliases)
    state["last_run"] = run_started.isoformat()
    state["last_added"] = len(new_rows)
    save_state(state)

    total = len(known - aliases) + len(new_rows)
    names = ", ".join(r[1] for r in sorted(new_rows.values(), key=lambda r: r[1].lower())[:15])
    log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {len(new_rows)} new compan{'y' if len(new_rows) == 1 else 'ies'} "
        f"added ({total} total) in {round(time.time() - t0)}s."
        + (f" {errors} name(s) couldn't be checked and will be retried." if errors else "")
        + (f"\n  New: {names}{' …' if len(new_rows) > 15 else ''}" if new_rows else "")
        + ("\n  Their jobs are fetched on the next refresh." if new_rows else ""))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Nothing was saved from this run; run again any time.")
