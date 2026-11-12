#!/usr/bin/env python3
"""Find companies that use Ashby and add new ones to companies.tsv (runs weekly via schedule.py).

Usage:
    python3 update_companies.py             # look for companies added since the last run (weekly)
    python3 update_companies.py --full      # rescan every source's full history, add anything missing
    python3 update_companies.py --rebuild   # build companies.tsv from scratch from the sources below

Where candidates come from (all free, no account needed):
  * the Wayback Machine's index of every archived jobs.ashbyhq.com page
  * Hacker News comments that link to jobs.ashbyhq.com ("Who's hiring" threads)
  * public new-grad / internship job lists on GitHub that link to Ashby boards

Each new name is checked against its live Ashby board page: it is kept only if a real (non-demo)
company owns it, under the board name Ashby itself uses. Names that turn out not to be companies
are remembered, so they aren't checked again. Weekly runs never remove entries; closed boards
are simply skipped by ingest.py.

The discovery approach is adapted from scripts/refresh_companies.py in
github.com/AnshumanAtrey/ashby-jobs-search (MIT License, Copyright (c) 2026 Anshuman Atrey).
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
# Where a weekly run starts if it has no saved progress yet.
FIRST_RUN_FROM = datetime(2026, 9, 1, tzinfo=timezone.utc)
FULL_FROM = datetime(2019, 1, 1, tzinfo=timezone.utc)   # before Ashby's first job boards
LINK = re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9._%~&-]{0,99})", re.I)
NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~ &-]{0,99}")
MIN_REBUILD = 500   # refuse to replace the list with fewer confirmed companies than this
APP_DATA = "window.__appData = "
HEADER = (
    "# Ashby job boards: board name<TAB>company name.\n"
    "# Built by update_companies.py from the Wayback Machine, Hacker News and public job lists;\n"
    "# every entry was checked against its live Ashby board.\n"
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

def from_wayback(since):
    found, key, pages = set(), None, 0
    while True:
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
        pages += 1
        if pages % 10 == 0:
            log(f"    Wayback: {pages * 150000:,} archived pages read, {len(found):,} names so far")
        if not key or not rows.strip() or pages >= 2000:
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


def check_all(names, workers):
    """Verify names against Ashby. Returns (ok: {lower: (slug, name)}, aliases, rejected, retry)."""
    ok, aliases, rejected, retry = {}, set(), set(), []
    if not names:
        return ok, aliases, rejected, retry
    log(f"  checking {len(names):,} name(s) against Ashby...")
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cand, (result, slug, cname) in zip(names, pool.map(verify, names)):
            done += 1
            if done % 1000 == 0:
                log(f"    {done:,}/{len(names):,} checked, {len(ok):,} real companies so far")
            if result == "ok":
                if cand.lower() != slug.lower():
                    aliases.add(cand.lower())
                ok.setdefault(slug.lower(), (slug, cname))
            elif result == "no":
                rejected.add(cand.lower())
            else:
                retry.append(cand)
    return ok, aliases, rejected, retry


def gather(sources_since, known, rejected, state_cursors):
    """Collect names from every source. Returns {lower: name} not in known/rejected."""
    candidates = {}
    for name, key, fn in [("Wayback Machine", "wayback_from", from_wayback),
                          ("Hacker News", "hn_from", from_hacker_news),
                          ("GitHub job lists", None, lambda _since: from_github_lists())]:
        since = sources_since(key)
        try:
            found = fn(since)
            fresh = {c for c in found if c.lower() not in known and c.lower() not in rejected}
            for c in fresh:
                candidates.setdefault(c.lower(), c)
            if key:
                state_cursors[key] = True   # only advanced when the source worked
            log(f"  {name}: {len(found):,} names seen, {len(fresh):,} not yet known"
                + (f" (since {since:%Y-%m-%d})" if since else ""))
        except Exception as e:
            log(f"  {name}: skipped this time ({e}); will retry from the same point next run")
    return candidates


def write_rows(path, rows, mode, stamp):
    with open(path, mode, encoding="utf-8") as f:
        if mode == "w":
            f.write(HEADER)
        else:
            f.write(f"# added {stamp} by update_companies.py\n")
        for slug, cname in sorted(rows, key=lambda r: r[0].lower()):
            f.write(f"{slug}\t{cname.replace(chr(9), ' ')}\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="rescan all history and add anything missing")
    mode.add_argument("--rebuild", action="store_true", help="build companies.tsv from scratch")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    t0 = time.time()
    run_started = datetime.now(timezone.utc)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    state = load_state()
    fresh_start = args.full or args.rebuild
    log(f"[{stamp}] " + ("Rebuilding the company list from scratch..." if args.rebuild
                         else "Looking for new Ashby companies..."))

    old = known_slugs() if os.path.exists(COMPANIES) else set()
    rejected = set() if fresh_start else {r.lower() for r in state.get("rejected", [])}
    aliases = set() if args.rebuild else {a.lower() for a in state.get("aliases", [])}
    known = set() if args.rebuild else old | aliases

    def since(key):
        if not key:
            return None
        return FULL_FROM if fresh_start else parse_time(state.get(key), FIRST_RUN_FROM)

    worked = {}
    candidates = gather(since, known, rejected, worked)
    if not args.rebuild:
        # Names that couldn't be checked last time (network trouble) get another go.
        for c in state.get("retry", []):
            if c.lower() not in known and c.lower() not in rejected:
                candidates.setdefault(c.lower(), c)

    ok, new_aliases, new_rejected, retry = check_all(sorted(candidates.values()), args.workers)
    aliases |= new_aliases
    rejected |= new_rejected

    if args.rebuild:
        if "wayback_from" not in worked:
            sys.exit("The Wayback Machine couldn't be read, so the list was not rebuilt. Try again later.")
        if len(ok) < MIN_REBUILD:
            sys.exit(f"Only {len(ok)} companies confirmed; something went wrong, so the list was not replaced.")
        write_rows(COMPANIES, ok.values(), "w", stamp)
        found_keys = set(ok) | aliases
        missed = sorted(old - found_keys)
        log(f"  checking whether the {len(missed):,} previous entries we didn't find are still live...")
        still_live, _, _, _ = check_all(missed, args.workers)
        report = os.path.join(HERE, "rebuild_report.txt")
        with open(report, "w", encoding="utf-8") as f:
            f.write(f"Rebuild {stamp}: {len(ok)} companies confirmed from our own sources.\n")
            f.write(f"Previous list: {len(old)}; found again: {len(old & found_keys)}; "
                    f"not found by our sources: {len(missed)} ({len(still_live)} of them still live); "
                    f"new: {len(set(ok) - old)}.\n\n")
            f.write("Previous entries our sources didn't find but that are still live:\n")
            f.write("".join(f"{slug}\t{name}\n" for slug, name in sorted(still_live.values())))
        log(f"  previous list: {len(old):,}; found again: {len(old & found_keys):,}; "
            f"not found by our sources: {len(missed):,} ({len(still_live):,} still live); "
            f"new: {len(set(ok) - old):,} (details in rebuild_report.txt)")
        added = ok
    else:
        added = {k: v for k, v in ok.items() if k not in known}
        if added:
            write_rows(COMPANIES, added.values(), "a" if os.path.exists(COMPANIES) else "w", stamp)

    for key in worked:
        state[key] = run_started.isoformat()
    state["rejected"] = sorted(rejected)
    state["retry"] = sorted(retry)
    state["aliases"] = sorted(aliases)
    state["last_run"] = run_started.isoformat()
    state["last_added"] = len(added)
    save_state(state)

    total = len(ok) if args.rebuild else len(old) + len(added)
    names = ", ".join(r[1] for r in sorted(added.values(), key=lambda r: r[1].lower())[:15])
    log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] "
        + (f"Company list rebuilt: {total:,} companies" if args.rebuild else
           f"{len(added)} new compan{'y' if len(added) == 1 else 'ies'} added ({total:,} total)")
        + f" in {round(time.time() - t0)}s."
        + (f" {len(retry)} name(s) couldn't be checked and will be retried." if retry else "")
        + (f"\n  New: {names}{' …' if len(added) > 15 else ''}" if added and not args.rebuild else ""))

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Nothing was saved from this run; run again any time.")
