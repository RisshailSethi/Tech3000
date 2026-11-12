#!/usr/bin/env python3
"""Turn jobs.db (from ingest.py) into the compact data files the website loads.

Usage:
    python3 scraper/build_data.py --db jobs.db --out site/data

Writes one file per platform (jobs-ashby.json, jobs-greenhouse.json, jobs-lever.json) plus meta.json,
so the site can show the first platform's jobs while the rest are still loading.

Refuses to write (exit code 1) if the refresh looks broken, so a bad week never
replaces a good site: too few jobs, or fewer than half the companies fetched.
"""
import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from ingest import SOURCES, split_key  # noqa: E402

LABELS = {"ashby": "Ashby", "greenhouse": "Greenhouse", "lever": "Lever"}
WORK = {"Remote": 1, "Hybrid": 2, "OnSite": 3}
LEVELS = {"intern": 0, "entry": 1, "mid": 2, "senior": 3, "staff": 4, "director": 5}
EMPLOYMENT = {"FullTime": 0, "PartTime": 1, "Intern": 2, "Contract": 3, "Temporary": 4}
EPOCH = date(1970, 1, 1)


def posted_day(value):
    """Days since 1970-01-01, which keeps dates small in the data file."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt.astimezone(timezone.utc).date() - EPOCH).days


def link_prefix(source, slug):
    """Common start of a platform's job links; the site rebuilds full links from it (see app.js)."""
    if source == "ashby":
        return f"https://jobs.ashbyhq.com/{slug}/"
    if source == "greenhouse":
        return f"https://job-boards.greenhouse.io/{slug}/jobs/"
    if slug.startswith("eu/"):
        return f"https://jobs.eu.lever.co/{slug[3:]}/"
    return f"https://jobs.lever.co/{slug}/"


def build_source(c, source, excluded):
    companies, company_index = [], {}
    for key, name, n in c.execute(
            "SELECT j.slug, COALESCE(NULLIF(b.name, ''), j.slug), COUNT(*) FROM jobs j "
            "LEFT JOIN boards b ON b.slug = j.slug WHERE j.active = 1 GROUP BY j.slug"):
        src, slug = split_key(key)
        if src != source or key.lower() in excluded or (src == "ashby" and slug.lower() in excluded):
            continue
        company_index[key] = len(companies)
        companies.append([name if name != key else slug, slug, n])

    tables = {"loc": {}, "dept": {}, "ccy": {}}

    def idx(table, value):
        t = tables[table]
        if value not in t:
            t[value] = len(t)
        return t[value]

    jobs = []
    for r in c.execute(
            "SELECT slug, title, url, location, secondary_locations, department, workplace_type, is_remote, "
            "level, years_min, salary_min, salary_max, salary_currency, published_at, employment_type "
            "FROM jobs WHERE active = 1 ORDER BY published_at DESC"):
        (key, title, url, loc, loc2, dept, wt, remote, level, years, smin, smax, ccy, published, emp) = r
        if key not in company_index:
            continue
        prefix = link_prefix(source, split_key(key)[1])
        link = url[len(prefix):] if url.startswith(prefix) else url
        location = "; ".join(p for p in (loc, loc2) if p)
        has_pay = smin is not None and smax is not None and ccy
        jobs.append([
            title,
            company_index[key],
            idx("loc", location),
            idx("dept", dept or ""),
            WORK.get(wt, 1 if remote else 0),          # 0 = not stated
            LEVELS.get(level or "mid", 2),
            years,
            round(smin) if has_pay else None,
            round(smax) if has_pay else None,
            idx("ccy", ccy) if has_pay else None,
            posted_day(published),
            link,
            EMPLOYMENT.get(emp, 0),
        ])
    return {
        "source": source,
        "label": LABELS[source],
        "fields": ["title", "company", "location", "department", "workplace", "level", "years",
                   "salary_min", "salary_max", "currency", "posted_day", "link", "employment"],
        "companies": companies,
        "locations": list(tables["loc"]),
        "departments": list(tables["dept"]),
        "currencies": list(tables["ccy"]),
        "jobs": jobs,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--exclude", default=os.path.join(HERE, "exclude.txt"))
    ap.add_argument("--min-jobs", type=int, default=1000)
    args = ap.parse_args()

    c = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    run = c.execute("SELECT boards_ok, boards_total FROM runs ORDER BY started DESC LIMIT 1").fetchone()
    if not run:
        sys.exit("No refresh has run on this database.")
    if run[1] and run[0] < 0.5 * run[1]:
        sys.exit(f"Only {run[0]} of {run[1]} companies could be fetched; not publishing this week's data.")

    excluded = set()
    if os.path.exists(args.exclude):
        with open(args.exclude, encoding="utf-8") as f:
            excluded = {l.split("#", 1)[0].strip().lower() for l in f} - {""}

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [p for p in (build_source(c, s, excluded) for s in SOURCES) if p["jobs"]]
    total = sum(len(p["jobs"]) for p in parts)
    if total < args.min_jobs:
        sys.exit(f"Only {total} jobs found; not publishing this week's data.")

    os.makedirs(args.out, exist_ok=True)
    files = []
    for p in parts:
        p["updated"] = updated
        name = f"jobs-{p['source']}.json"
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, separators=(",", ":"))
        size = os.path.getsize(os.path.join(args.out, name)) / 1e6
        files.append({"source": p["source"], "label": p["label"], "file": name,
                      "jobs": len(p["jobs"]), "companies": len(p["companies"])})
        print(f"  {p['label']}: {len(p['jobs']):,} jobs from {len(p['companies']):,} companies ({size:.1f} MB)")
    meta = {"updated": updated, "jobs": total, "companies": sum(f["companies"] for f in files), "files": files}
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)
    print(f"Wrote {total:,} jobs from {meta['companies']:,} companies.")


if __name__ == "__main__":
    main()
