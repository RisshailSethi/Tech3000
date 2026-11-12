#!/usr/bin/env python3
"""Find companies hiring through Ashby, Greenhouse and Lever, and keep their lists up to date.

Usage:
    python3 update_companies.py                          # weekly: look for companies added since the last run
    python3 update_companies.py --full                   # rescan every source's full history, add anything missing
    python3 update_companies.py --rebuild                # build the lists from scratch
    python3 update_companies.py --rebuild --platforms greenhouse,lever   # only some platforms
    python3 update_companies.py --rebuild --wayback-minutes 100          # stop scanning after 100 min per
                                                                         # platform; the next run continues

Lists (board name<TAB>company name): companies.tsv (Ashby), greenhouse.tsv, lever.tsv.

Where candidates come from (all free, no account needed):
  * the Wayback Machine's index of every archived page on each platform's job-board domain
  * Hacker News comments that link to those job boards ("Who's hiring" threads)
  * public new-grad / internship job lists on GitHub

Each new name is checked against the platform itself (Ashby's board page, Greenhouse's and Lever's
public job APIs) and kept only if a real company owns it. Names that turn out not to be companies
are remembered so they aren't checked again. Weekly runs never remove entries; closed boards are
simply skipped by ingest.py.

The discovery approach is adapted from scripts/refresh_companies.py in
github.com/AnshumanAtrey/ashby-jobs-search (MIT License, Copyright (c) 2026 Anshuman Atrey).
"""
import argparse
import gzip
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
STATE = os.path.join(HERE, "discovery_state.json")
UA = "Mozilla/5.0 (compatible; tech3000-job-index/1.0; free, non-commercial job search)"

# Overridable for testing.
WAYBACK = os.environ.get("ASHBY_WAYBACK_URL", "https://web.archive.org/cdx/search/cdx")
HN = os.environ.get("ASHBY_HN_URL", "https://hn.algolia.com/api/v1/search_by_date")
GITHUB_RAW = os.environ.get("ASHBY_GITHUB_RAW", "https://raw.githubusercontent.com")
BOARD_PAGE = os.environ.get("ASHBY_BOARD_URL", "https://jobs.ashbyhq.com")
POSTING_API = os.environ.get("ASHBY_API_BASE", "https://api.ashbyhq.com/posting-api/job-board")
GREENHOUSE_API = os.environ.get("GREENHOUSE_API_BASE", "https://boards-api.greenhouse.io/v1/boards")
LEVER_API = os.environ.get("LEVER_API_BASE", "https://api.lever.co/v0/postings")
LEVER_EU_API = os.environ.get("LEVER_EU_API_BASE", "https://api.eu.lever.co/v0/postings")
LEVER_PAGE = os.environ.get("LEVER_PAGE_URL", "https://jobs.lever.co")
LEVER_EU_PAGE = os.environ.get("LEVER_EU_PAGE_URL", "https://jobs.eu.lever.co")

GITHUB_LISTS = [
    "SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "SimplifyJobs/Summer2026-Internships/dev/.github/scripts/listings.json",
    "SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    "speedyapply/2026-SWE-College-Jobs/main/README.md",
    "speedyapply/2027-SWE-College-Jobs/main/README.md",
    "vanshb03/Summer2026-Internships/dev/.github/scripts/listings.json",
    "vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
]
FIRST_RUN_FROM = datetime(2026, 9, 1, tzinfo=timezone.utc)   # a weekly run with no saved progress
FULL_FROM = datetime(2010, 1, 1, tzinfo=timezone.utc)
NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~ &-]{0,99}")
NOT_BOARDS = {"embed", "v1", "api", "static", "assets", "includes", "favicon.ico", "robots.txt", "sitemap.xml",
              "job_app", "jobs", "search", "apply", "login", "privacy", "terms"}
APP_DATA = "window.__appData = "
MIN_REBUILD = {"ashby": 500, "greenhouse": 500, "lever": 200}   # refuse to replace a list with fewer

PLATFORMS = {
    "ashby": {
        "label": "Ashby", "file": "companies.tsv",
        "wayback": ["jobs.ashbyhq.com/"], "hn": "ashbyhq",
        "link": re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9][A-Za-z0-9._%~&-]{0,99})", re.I),
    },
    "greenhouse": {
        "label": "Greenhouse", "file": "greenhouse.tsv",
        "wayback": ["boards.greenhouse.io/", "job-boards.greenhouse.io/", "job-boards.eu.greenhouse.io/"],
        "hn": "greenhouse.io",
        "link": re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/(?:embed/job_(?:board|app)\?(?:[^\s\"'<>]*?&)?for=)?"
                           r"([A-Za-z0-9][A-Za-z0-9._-]{0,99})", re.I),
    },
    "lever": {
        "label": "Lever", "file": "lever.tsv",
        "wayback": ["jobs.lever.co/", "jobs.eu.lever.co/"], "hn": "lever.co",
        "link": re.compile(r"jobs\.(eu\.)?lever\.co/([A-Za-z0-9][A-Za-z0-9._-]{0,99})", re.I),
    },
}
HEADER = (
    "# {label} job boards: board name<TAB>company name.\n"
    "# Built by update_companies.py from the Wayback Machine, Hacker News and public job lists;\n"
    "# every entry was checked against {label}.\n"
)


def log(msg):
    print(msg, flush=True)


def get(url, params=None, timeout=60, tries=3, backoff=5):
    """GET with retries and gzip. Returns (status, text); status 0 means the request failed."""
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    body = gzip.decompress(body)
                return r.status, body.decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(backoff * (attempt + 1))
                continue
            return e.code, ""
        except Exception:
            if attempt < tries - 1:
                time.sleep(max(3, backoff // 2) * (attempt + 1))
                continue
            return 0, ""
    return 0, ""


def clean(name):
    name = urllib.parse.unquote(name or "").strip().rstrip(".")
    return name if name and NAME_OK.fullmatch(name) and name.lower() not in NOT_BOARDS else None


def name_from_url(platform, url):
    """Board name from an archived URL, e.g. 'https://boards.greenhouse.io/stripe/jobs/1' -> 'stripe'."""
    u = urllib.parse.urlparse(url.strip() if "://" in url else "https://" + url.strip())
    seg = u.path.strip("/").split("/")[0].split("?")[0].split("#")[0]
    if platform == "greenhouse" and seg == "embed":
        seg = (urllib.parse.parse_qs(u.query).get("for") or [""])[0]
    name = clean(seg)
    if name and platform == "lever" and u.netloc.lower().split(":")[0].startswith("jobs.eu."):
        name = "eu/" + name
    return name


def links_in(platform, text):
    out = set()
    for m in PLATFORMS[platform]["link"].finditer(html.unescape(text or "")):
        if platform == "lever":
            name = clean(m.group(2))
            if name and m.group(1):
                name = "eu/" + name
        else:
            name = clean(m.group(1))
        if name:
            out.add(name)
    return out


# ---------------------------------------------------------------- sources

SHARDS = "0123456789abcdefghijklmnopqrstuvwxyz"   # first character of the board name


WAYBACK_CACHE = os.path.join(HERE, "wayback_cache")
WAYBACK_DEADLINE = [None]   # time.time() after which no new Wayback pages are requested (--wayback-minutes)


def load_cache(platform, since):
    """Saved progress from earlier runs over the same history, so a long scan can continue where it stopped."""
    try:
        with open(os.path.join(WAYBACK_CACHE, f"{platform}.json"), encoding="utf-8") as f:
            cache = json.load(f)
        if cache.get("since") == since.strftime("%Y%m%d"):
            return cache
    except (OSError, ValueError):
        pass
    return {"since": since.strftime("%Y%m%d"), "shards": {}}


def save_cache(platform, cache):
    os.makedirs(WAYBACK_CACHE, exist_ok=True)
    path = os.path.join(WAYBACK_CACHE, f"{platform}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump(cache, f, separators=(",", ":"))
    os.replace(path + ".tmp", path)


def out_of_time():
    return WAYBACK_DEADLINE[0] is not None and time.time() > WAYBACK_DEADLINE[0]


def wayback_shard(platform, shard_prefix, since, entry, save):
    """Reads board names under one shard of one domain, e.g. 'boards.greenhouse.io/a', into entry
    ({"names", "resume", "done"}), saving after every page. Stops early when time runs out; raises on failure."""
    names = set(entry.get("names", []))
    while not entry.get("done"):
        if out_of_time():
            return
        params = {"url": shard_prefix, "matchType": "prefix", "fl": "original", "collapse": "urlkey",
                  "limit": "50000", "showResumeKey": "true", "from": since.strftime("%Y%m%d%H%M%S")}
        if entry.get("resume"):
            params["resumeKey"] = entry["resume"]
        status, body = get(WAYBACK, params, timeout=300, tries=5, backoff=30)
        if status != 200:
            raise RuntimeError(f"HTTP {status}" if status else "couldn't connect")
        rows, _, rest = body.rstrip("\n").partition("\n\n")
        for line in rows.splitlines():
            name = name_from_url(platform, line)
            if name:
                names.add(name)
        entry["resume"] = rest.strip() or None
        entry["pages"] = entry.get("pages", 0) + 1
        entry["done"] = not entry["resume"] or not rows.strip()
        entry["names"] = sorted(names)
        save()
        time.sleep(1)


def from_wayback(platform, since):
    """Returns (names, complete). The archive is read in small pieces (one per first character),
    because one query over a whole domain is too heavy for the Wayback Machine. Progress is saved
    to wayback_cache/, so a scan cut short (failures or --wayback-minutes) continues next run."""
    cache = load_cache(platform, since)
    save = lambda: save_cache(platform, cache)   # noqa: E731
    found, failed, unread, total = set(), [], 0, 0
    for prefix in PLATFORMS[platform]["wayback"]:
        t = time.time()
        for ch in SHARDS:
            total += 1
            entry = cache["shards"].setdefault(prefix + ch, {})
            try:
                wayback_shard(platform, prefix + ch, since, entry, save)
                if not entry.get("done"):
                    unread += 1
            except RuntimeError as e:
                failed.append(f"{prefix}{ch} ({e})")
            found |= set(entry.get("names", []))
        pages = sum(cache["shards"].get(prefix + ch, {}).get("pages", 0) for ch in SHARDS)
        log(f"    {prefix}: {len(found):,} names so far ({pages:,} pages read in total, "
            f"{round(time.time() - t)}s this run)")
    if not found and failed:
        raise RuntimeError(f"{len(failed)} of {total} pieces failed, e.g. {failed[0]}")
    if failed:
        log(f"    {len(failed)} of {total} pieces couldn't be read: {', '.join(failed[:5])}")
    if unread:
        log(f"    ran out of time with {unread} of {total} pieces unfinished; the next run continues from here")
    return found, not failed and not unread


def from_hacker_news(platform, since):
    found = set()
    start, step, end = int(since.timestamp()), 86400 * 60, int(time.time())
    while start < end:
        for page in range(5):
            params = {"query": PLATFORMS[platform]["hn"], "tags": "comment", "hitsPerPage": 1000, "page": page,
                      "numericFilters": f"created_at_i>{start},created_at_i<={start + step}"}
            status, body = get(HN, params)
            if status != 200:
                raise RuntimeError(f"HTTP {status}" if status else "couldn't connect")
            data = json.loads(body)
            for hit in data.get("hits", []):
                found |= links_in(platform, hit.get("comment_text"))
            if page >= data.get("nbPages", 1) - 1:
                break
        start += step
    return found


_github_cache = {}


def from_github_lists(platform, _since=None):
    if "texts" not in _github_cache:
        texts = []
        for path in GITHUB_LISTS:
            status, body = get(f"{GITHUB_RAW}/{path}", tries=2)
            if status == 200:
                texts.append(body)
        _github_cache["texts"] = texts
    if not _github_cache["texts"]:
        raise RuntimeError("no list could be downloaded")
    found = set()
    for body in _github_cache["texts"]:
        found |= links_in(platform, body)
    return found


# ---------------------------------------------------------------- verification

def verify_ashby(candidate):
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


def verify_greenhouse(candidate):
    token = candidate.lower()
    status, body = get(f"{GREENHOUSE_API}/{urllib.parse.quote(token, safe='')}", timeout=30)
    if status == 404:
        return "no", candidate, None
    if status == 200:
        try:
            name = (json.loads(body).get("name") or "").strip()
        except ValueError:
            return "error", candidate, None
        return "ok", token, name or token
    return "error", candidate, None


TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


def verify_lever(candidate):
    eu = candidate.startswith("eu/")
    site = (candidate[3:] if eu else candidate).lower()
    status, _ = get(f"{LEVER_EU_API if eu else LEVER_API}/{urllib.parse.quote(site, safe='')}",
                    {"mode": "json", "limit": 1}, timeout=30)
    if status == 404:
        return "no", candidate, None
    if status != 200:
        return "error", candidate, None
    name = ""
    page_status, page = get(f"{LEVER_EU_PAGE if eu else LEVER_PAGE}/{site}", timeout=30, tries=2)
    if page_status == 200:
        m = TITLE.search(page)
        if m:
            name = " ".join(html.unescape(m.group(1)).split())
            name = re.sub(r"\s*[-|–]\s*(jobs|careers).*$|^(jobs|careers) at\s+|\s+jobs$", "", name, flags=re.I).strip()
    if not name or len(name) > 80:
        name = site.replace("-", " ").title()
    return "ok", ("eu/" if eu else "") + site, name


VERIFY = {"ashby": verify_ashby, "greenhouse": verify_greenhouse, "lever": verify_lever}


# ---------------------------------------------------------------- state and files

def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    if "wayback_from" in state or "rejected" in state:      # older single-platform format
        state = {"ashby": {k: state.pop(k) for k in ("wayback_from", "hn_from", "rejected", "aliases", "retry")
                           if k in state}, **state}
    for p in PLATFORMS:
        state.setdefault(p, {})
    return state


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE)


def list_path(platform):
    return os.path.join(HERE, PLATFORMS[platform]["file"])


def known_slugs(platform):
    sys.path.insert(0, HERE)
    from ingest import load_slugs, split_key
    out = {s.lower() for s, _ in load_slugs(list_path(platform))}
    for s, _ in load_slugs(os.path.join(HERE, "slugs.txt")):    # your own additions, any platform
        src, slug = split_key(s)
        if src == platform:
            out.add(slug.lower())
    return out


def parse_time(value, default):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return default


def check_all(platform, names, workers):
    """Verify names. Returns (ok: {lower: (slug, name)}, aliases, rejected, retry)."""
    ok, aliases, rejected, retry = {}, set(), set(), []
    if not names:
        return ok, aliases, rejected, retry
    log(f"  checking {len(names):,} {PLATFORMS[platform]['label']} name(s)...")
    verify = VERIFY[platform]

    def one(c):
        time.sleep(0.15)
        return verify(c)

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for cand, (result, slug, cname) in zip(names, pool.map(one, names)):
            done += 1
            if done % 2000 == 0:
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


def write_rows(platform, rows, mode, stamp):
    with open(list_path(platform), mode, encoding="utf-8") as f:
        if mode == "w":
            f.write(HEADER.format(label=PLATFORMS[platform]["label"]))
        else:
            f.write(f"# added {stamp} by update_companies.py\n")
        for slug, cname in sorted(rows, key=lambda r: r[0].lower()):
            f.write(f"{slug}\t{cname.replace(chr(9), ' ')}\n")


def run_platform(platform, args, st, stamp, run_started):
    """Discover and verify one platform. Returns the number of companies added."""
    label = PLATFORMS[platform]["label"]
    has_list = os.path.exists(list_path(platform))
    if not has_list and not args.rebuild:
        log(f"{label}: no list yet; run with --rebuild --platforms {platform} first. Skipping.")
        return 0
    fresh_start = args.full or args.rebuild
    log(f"{label}: " + ("rebuilding from scratch..." if args.rebuild else "looking for new companies..."))
    old = known_slugs(platform) if has_list else set()
    rejected = set() if fresh_start else {r.lower() for r in st.get("rejected", [])}
    aliases = set() if args.rebuild else {a.lower() for a in st.get("aliases", [])}
    known = set() if args.rebuild else old | aliases

    candidates, worked, usable = {}, set(), set()
    for name, key, fn in [("Wayback Machine", "wayback_from", from_wayback),
                          ("Hacker News", "hn_from", from_hacker_news),
                          ("GitHub job lists", None, from_github_lists)]:
        since = (FULL_FROM if fresh_start else parse_time(st.get(key), FIRST_RUN_FROM)) if key else None
        try:
            found = fn(platform, since)
            complete = True
            if isinstance(found, tuple):
                found, complete = found
            fresh = {c for c in found if c.lower() not in known and c.lower() not in rejected}
            for c in fresh:
                candidates.setdefault(c.lower(), c)
            if key:
                usable.add(key)
                if complete:
                    worked.add(key)   # the "checked up to" point only moves when nothing was missed
            log(f"  {name}: {len(found):,} names seen, {len(fresh):,} not yet known"
                + (f" (since {since:%Y-%m-%d})" if since else ""))
        except Exception as e:
            log(f"  {name}: skipped this time ({e}); will retry from the same point next run")
    if not args.rebuild:
        for c in st.get("retry", []):
            if c.lower() not in known and c.lower() not in rejected:
                candidates.setdefault(c.lower(), c)

    ok, new_aliases, new_rejected, retry = check_all(platform, sorted(candidates.values()), args.workers)
    aliases |= new_aliases
    rejected |= new_rejected

    if args.rebuild:
        if "wayback_from" not in usable:
            log(f"  The Wayback Machine couldn't be read, so the {label} list was not rebuilt.")
            return 0
        if len(ok) < MIN_REBUILD[platform]:
            if "wayback_from" not in worked:
                log(f"  Only {len(ok)} {label} companies confirmed so far, from an unfinished scan; the list "
                    f"will be written once a later run gets further (progress is saved).")
            else:
                log(f"  Only {len(ok)} {label} companies confirmed; something went wrong, so the list was not replaced.")
            return 0
        write_rows(platform, ok.values(), "w", stamp)
        found_keys = set(ok) | aliases
        missed = sorted(old - found_keys)
        still_live = {}
        if missed:
            log(f"  checking whether the {len(missed):,} previous entries we didn't find are still live...")
            still_live, _, _, _ = check_all(platform, missed, args.workers)
        log(f"  {label}: {len(ok):,} companies confirmed. Previous list: {len(old):,}; found again: "
            f"{len(old & found_keys):,}; not found: {len(missed):,} ({len(still_live):,} still live); "
            f"new: {len(set(ok) - old):,}.")
        if still_live:
            with open(os.path.join(HERE, f"rebuild_report_{platform}.txt"), "w", encoding="utf-8") as f:
                f.write(f"Previous {label} entries our sources didn't find but that are still live:\n")
                f.write("".join(f"{s}\t{n}\n" for s, n in sorted(still_live.values())))
        added = ok
    else:
        added = {k: v for k, v in ok.items() if k not in known}
        if added:
            write_rows(platform, added.values(), "a", stamp)

    for key in worked:
        st[key] = run_started.isoformat()
    st["rejected"] = sorted(rejected)
    st["retry"] = sorted(retry)
    st["aliases"] = sorted(aliases)
    total = len(ok) if args.rebuild else len(old) + len(added)
    names = ", ".join(r[1] for r in sorted(added.values(), key=lambda r: r[1].lower())[:15])
    log(f"  {label}: " + (f"list rebuilt, {total:,} companies" if args.rebuild else
                          f"{len(added)} new compan{'y' if len(added) == 1 else 'ies'} added ({total:,} total)")
        + (f"; {len(retry)} name(s) couldn't be checked and will be retried" if retry else "") + "."
        + (f"\n  New: {names}{' …' if len(added) > 15 else ''}" if added and not args.rebuild else ""))
    return len(added)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--full", action="store_true", help="rescan all history and add anything missing")
    mode.add_argument("--rebuild", action="store_true", help="build the lists from scratch")
    ap.add_argument("--platforms", default=",".join(PLATFORMS), help="e.g. greenhouse,lever")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--wayback-minutes", type=float, default=None,
                    help="stop reading the Wayback Machine after this many minutes per platform "
                         "(progress is saved, and the next run continues)")
    ap.add_argument("--wayback-total-minutes", type=float, default=None,
                    help="and after this many minutes across all platforms together")
    args = ap.parse_args()
    wanted = {p.strip() for p in args.platforms.split(",")}
    platforms = [p for p in ("ashby", "lever", "greenhouse") if p in wanted]   # smallest first

    t0 = time.time()
    run_started = datetime.now(timezone.utc)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    log(f"[{stamp}] Updating company lists: {', '.join(PLATFORMS[p]['label'] for p in platforms)}")
    state = load_state()
    added = 0
    for platform in platforms:
        limits = [time.time() + 60 * args.wayback_minutes if args.wayback_minutes else None,
                  t0 + 60 * args.wayback_total_minutes if args.wayback_total_minutes else None]
        WAYBACK_DEADLINE[0] = min((x for x in limits if x), default=None)
        added += run_platform(platform, args, state[platform], stamp, run_started)
        save_state(state)   # after each platform, so a later failure keeps earlier progress
    state["last_run"] = run_started.isoformat()
    state["last_added"] = added
    save_state(state)
    log(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Done in {round(time.time() - t0)}s; "
        f"{added:,} compan{'y' if added == 1 else 'ies'} added.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. Lists finished before the stop were saved; run again any time.")
