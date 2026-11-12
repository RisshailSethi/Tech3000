# Tech3000

List of currently open roles in leading AI companies and startups.

**Live site: https://risshailsethi.github.io/Tech3000/**

Search jobs from thousands of companies that hire through **Ashby, Greenhouse and Lever**, the job-board platforms most startups and tech companies use. Filter by job board, salary, remote/hybrid/in person, experience level, years of experience, company size, department and posting date, then export your results as a CSV. There are no logins and no ads, and every listing links to the company's own job page.

Jobs and companies are updated every **Monday at 9am PST**.

## About me

I'm Risshail Sethi, a data and analytics professional based in Vancouver and a UBC grad (International Economics, minor in Data Science). Ashby, Greenhouse and Lever host some of the best tech jobs around, but you can only browse them one company at a time, so I built the search I wanted to use. It's a free side project; if it helps your job hunt, that's the whole point. If you'd like to support it, you can [buy me a coffee](https://buymeacoffee.com/sethirisshy).

## How it works

Every Monday a GitHub Actions workflow (`.github/workflows/weekly-update.yml`) runs these steps:

1. `scraper/update_companies.py` finds companies new to each platform. It looks at the Wayback Machine's archive of every page on `jobs.ashbyhq.com`, `boards.greenhouse.io` / `job-boards.greenhouse.io` and `jobs.lever.co`, plus Hacker News hiring posts and public new-grad and internship lists on GitHub. It checks each name against the platform and commits the updated lists: `scraper/companies.tsv` (Ashby), `scraper/greenhouse.tsv` and `scraper/lever.tsv`.
2. `scraper/ingest.py` fetches every company's current jobs from each platform's public jobs API. Closed jobs drop off.
3. `scraper/build_data.py` writes the site's data, one file per platform (`site/data/jobs-ashby.json`, `jobs-greenhouse.json`, `jobs-lever.json`). The site shows the first one as soon as it loads and merges in the others.
4. The static site in `site/` is published to GitHub Pages.

If a week's refresh looks broken (fewer than half the companies fetched, or under 1,000 jobs), the build stops and last week's site stays up.

To update early, go to **Actions → Weekly update → Run workflow**.

## Removing a company

If you'd like a company taken off the site, [open an issue](../../issues). To remove one yourself, add a line to `scraper/exclude.txt` and run the workflow: the Ashby board name for Ashby companies (the part after `jobs.ashbyhq.com/`), or `greenhouse:<name>` / `lever:<name>` for the others.

## Preview locally

```bash
python3 scraper/ingest.py --db jobs.db
python3 scraper/build_data.py --db jobs.db --out site/data
python3 -m http.server -d site 8000      # open http://127.0.0.1:8000
```

## Notes

- This is an unofficial index and isn't affiliated with Ashby, Greenhouse, Lever or any company listed. Job data comes from each company's public job board, and Ashby postings marked unlisted are skipped.
- Experience level is inferred from job titles, years from descriptions, and company size from the number of open roles on the company's board. For Greenhouse jobs, salary and remote/hybrid are read from the job description, so check the posting for the details.
- The company lists are built from public sources by `scraper/update_companies.py`. To rebuild them from scratch, run **Actions → Rebuild company list** (you can pick which platforms); the result lands on a `company-rebuild` branch for review before you merge it.
- The company-discovery approach is adapted from [AnshumanAtrey/ashby-jobs-search](https://github.com/AnshumanAtrey/ashby-jobs-search) (MIT License, Copyright (c) 2026 Anshuman Atrey).
