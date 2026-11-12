# Tech3000

List of currently open roles in leading AI companies and startups.

**Live site: https://risshailsethi.github.io/Tech3000/**

Search tens of thousands of jobs from the ~3,800 companies that hire through Ashby. Filter by salary, remote/hybrid/in person, experience level, years of experience, company size, department and posting date, then export your results as a CSV. There are no logins and no ads, and every listing links to the company's own job page.

Jobs and companies are updated every **Monday at 9am PST**.

## About me

I'm Risshail Sethi, a data and analytics professional based in Vancouver and a UBC grad (International Economics, minor in Data Science). Ashby hosts some of the best tech jobs around, but you can only browse them one company at a time, so I built the search I wanted to use. It's a free side project; if it helps your job hunt, that's the whole point. If you'd like to support it, you can [buy me a coffee](https://buymeacoffee.com/sethirisshy).

## How it works

Every Monday a GitHub Actions workflow (`.github/workflows/weekly-update.yml`) runs these steps:

1. `scraper/update_companies.py` finds companies new to Ashby. It looks at the Wayback Machine's archive of every `jobs.ashbyhq.com` page, Hacker News hiring posts, and public new-grad and internship lists on GitHub. It checks each one against Ashby and commits the updated `scraper/companies.tsv`.
2. `scraper/ingest.py` fetches every company's current jobs. Closed jobs drop off.
3. `scraper/build_data.py` writes the site's data (`site/data/jobs.json`).
4. The static site in `site/` is published to GitHub Pages.

If a week's refresh looks broken (fewer than half the companies fetched, or under 1,000 jobs), the build stops and last week's site stays up.

To update early, go to **Actions → Weekly update → Run workflow**.

## Removing a company

If you'd like a company taken off the site, [open an issue](../../issues). To remove one yourself, add its Ashby board name (the part after `jobs.ashbyhq.com/`) to `scraper/exclude.txt` and run the workflow.

## Preview locally

```bash
python3 scraper/ingest.py --db jobs.db
python3 scraper/build_data.py --db jobs.db --out site/data
python3 -m http.server -d site 8000      # open http://127.0.0.1:8000
```

## Notes

- This is an unofficial index and isn't affiliated with Ashby or any company listed. Job data comes from each company's public Ashby job board, and postings marked unlisted are skipped.
- Experience level is inferred from job titles, years from descriptions, and company size from the number of open roles on Ashby. Check the posting for the details.
- The company list is built from public sources by `scraper/update_companies.py`. To rebuild it from scratch, run **Actions → Rebuild company list**; the result lands on a `company-rebuild` branch for review before you merge it.
- The company-discovery approach is adapted from [AnshumanAtrey/ashby-jobs-search](https://github.com/AnshumanAtrey/ashby-jobs-search) (MIT License, Copyright (c) 2026 Anshuman Atrey).
