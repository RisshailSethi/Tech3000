#!/usr/bin/env python3
"""Turn jobs.db (from ingest.py) into the compact data files the website loads.

Usage:
    python3 scraper/build_data.py --db jobs.db --out site/data

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
    return (dt.astimezone(timezone.utc).date() - EPOCH).days


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

    companies, company_index = [], {}
    for slug, name, n in c.execute(
            "SELECT j.slug, COALESCE(NULLIF(b.name, ''), j.slug), COUNT(*) FROM jobs j "
            "LEFT JOIN boards b ON b.slug = j.slug WHERE j.active = 1 GROUP BY j.slug"):
        if slug.lower() in excluded:
            continue
        company_index[slug] = len(companies)
        companies.append([name, slug, n])

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
        (slug, title, url, loc, loc2, dept, wt, remote, level, years, smin, smax, ccy, published, emp) = r
        if slug not in company_index:
            continue
        prefix = f"https://jobs.ashbyhq.com/{slug}/"
        link = url[len(prefix):] if url.startswith(prefix) else url
        location = "; ".join(p for p in (loc, loc2) if p)
        has_pay = smin is not None and smax is not None and ccy
        jobs.append([
            title,
            company_index[slug],
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

    if len(jobs) < args.min_jobs:
        sys.exit(f"Only {len(jobs)} jobs found; not publishing this week's data.")

    updated = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    data = {
        "updated": updated,
        "fields": ["title", "company", "location", "department", "workplace", "level", "years",
                   "salary_min", "salary_max", "currency", "posted_day", "link", "employment"],
        "companies": companies,
        "locations": list(tables["loc"]),
        "departments": list(tables["dept"]),
        "currencies": list(tables["ccy"]),
        "jobs": jobs,
    }
    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "jobs.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    with open(os.path.join(args.out, "meta.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "jobs": len(jobs), "companies": len(companies)}, f)
    size = os.path.getsize(os.path.join(args.out, "jobs.json")) / 1e6
    print(f"Wrote {len(jobs)} jobs from {len(companies)} companies ({size:.1f} MB before compression).")


if __name__ == "__main__":
    main()
