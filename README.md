# ATS Jobs & Hiring Signals: Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee

Get **every open job** from a company's job board, plus a **hiring-signal summary**, in one run, across **6 applicant tracking systems**:

| ATS | What you can paste |
|---|---|
| Greenhouse | `boards.greenhouse.io/stripe`, `job-boards.greenhouse.io/anthropic`, `greenhouse:stripe` |
| Lever | `jobs.lever.co/palantir`, `lever:palantir` |
| Ashby | `jobs.ashbyhq.com/ramp`, `ashby:ramp` |
| Workable | `apply.workable.com/huggingface`, `workable:huggingface` |
| SmartRecruiters | `jobs.smartrecruiters.com/Ubisoft2`, `smartrecruiters:Ubisoft2` |
| Recruitee | `bunq.recruitee.com`, `recruitee:bunq` |

You don't need to know the ATS. Paste a **bare slug** (`ramp`) and the Actor tries all six, keeping the board with the most open jobs. You can also paste a **company careers page** (`https://www.anthropic.com/careers`). The Actor finds the embedded board, and if the page is JS-rendered it falls back to the domain name.

Every row carries `match_confidence`. It is `exact` when you gave an ATS URL or `ats:slug`, and `guessed` for a bare slug or domain fallback. A bare slug can collide with another company that has the same slug on a different ATS (for example `clay` or `gong`), so use `ats:slug` or the board URL for accounts that matter. Empty boards are ignored while guessing.

**No login, no cookies, no proxies.** It reads only the public job-board endpoints that each ATS publishes so companies can show their jobs on their own sites. That makes runs fast (a few seconds for dozens of companies) and cheap.

## Who uses this
- **Sales and lead generation.** A company that opens 10 sales roles this month has budget. Run a list of target accounts weekly and sort by `hiring_signal_score` or `posted_last_30_days`.
- **Recruiters and staffing agencies.** Watch client and prospect boards in monitor mode and get only the roles that opened since the last run.
- **Job boards and aggregators.** Normalized fields across 6 ATS, with salary where the ATS exposes it (Ashby and Lever often do).
- **Job seekers and researchers.** Filter by keywords, location, remote, department and recency.

## Output
**Job rows** (`type: "job"`): `company_name, title, seniority, department, team, location, country, remote, workplace_type, work_model, employment_type, posted_at, updated_at, url, apply_url, salary_min, salary_max, salary_currency, salary_text, description (optional), ats, company_slug, is_new`

**Company summary rows** (`type: "company_summary"`): `total_open_jobs, matched_jobs, new_jobs_since_last_run, closed_jobs_since_last_run, closed_job_ids, net_change_since_last_run, posted_last_7_days, posted_last_30_days, remote_share, seniority_mix, work_model_mix, top_departments, top_locations, sample_matched_titles, hiring_signal_score (0-100), checked_at`

**Error rows** (`type: "error"`, never charged): an input where no public board was found.

### Seniority and work model on every row
- `seniority` is one of `intern, entry, mid, senior, lead, manager, director, executive`, inferred from the title with transparent rules (e.g. "Sr." → senior, "Staff/Principal/Architect" → lead, "Head of" → director, "VP/Chief" → executive). "Product/Account/Program/Project Manager" count as `mid` because they are usually not people managers. It's a rule, not AI, so it is fast, free and repeatable; check edge cases before relying on it.
- `work_model` is `remote`, `hybrid`, `onsite`, or `null` when the board doesn't say. It comes from the ATS workplace flag (Lever, Ashby, SmartRecruiters) or the location text.
- Filter with **Seniority levels** (e.g. `senior, lead, director`) and **Work model** (e.g. `remote, hybrid`). You pay only for rows that pass the filters.

Example (real run, Oct 29 2026):
```json
{"type":"job","company_name":"ramp","title":"Machine Learning Engineer","department":"Data","location":"New York, NY (HQ)","salary_text":"$200K – $330K • Offers Equity","ats":"ashby","url":"https://jobs.ashbyhq.com/ramp/..."}
{"type":"company_summary","company_name":"Stripe","ats":"greenhouse","total_open_jobs":724,"posted_last_30_days":235,"hiring_signal_score":92}
```

## For investors and equity research (alternative data)

Job postings are one of the most-used alternative datasets on the buy side: in Exabel's 2026 buy-side survey, 57% of fund managers named employment data as the category most likely to give an informational edge.
This Actor gives you the raw, first-party version of that signal at pay-per-event prices:

- **`ticker`, `cik`, `sec_title`** on every company summary (and `ticker` on every job), matched against SEC's public `company_tickers.json`. Matching is conservative: exact normalized name only; ambiguous or private companies stay `null`. Use `tickerMap` to set your own mapping.
- **Monitor mode** gives new jobs, closed jobs and net change per run - schedule it daily to build your own point-in-time hiring series for a watchlist.
- Typical uses: hiring momentum vs. peers before earnings, expansion into new countries (e.g. GCC hiring), AI/engineering hiring mix, hiring freezes (net change turning negative).

Data comes only from employers' own public career sites. No personal data about candidates is collected. Not investment advice.

## Monitor mode (only new jobs)
Turn on **Only new jobs since last run** and schedule the Actor (daily or weekly). Job IDs are stored per company in the named key-value store `ats-hiring-signals-state` under your **Monitor state key**. Later runs output, and charge for, only jobs that weren't there before. New jobs cut by *Max jobs per company* are kept for the next run.

From the second run on, each company summary also tells you what **closed**: `closed_jobs_since_last_run` (jobs that disappeared from the board since the previous run, i.e. filled or pulled), up to 50 `closed_job_ids`, and `net_change_since_last_run` (new minus closed). A board that is growing fast is a stronger buying signal than one that is just reposting. Closed counts are free (they live in the summary row) and are skipped when a board listing may be truncated (SmartRecruiters boards above 2,000 jobs). Connect the dataset to Slack, Google Sheets, n8n, Make or Zapier through Apify integrations.

## Hiring-signal score
The score runs from 0 to 100 and adds three parts:
- volume (up to 40, from the number of open jobs)
- freshness (up to 40, from jobs posted in the last 7 and 30 days)
- relevance (up to 20, from jobs matching your filters)

It is a ranking aid, not a prediction.

## Pricing (pay per event)
- **$0.001 per job row** ($1 per 1,000 jobs)
- **$0.005 per company summary**
- Error rows and the empty-input demo are free.

You can set a max charge per run, and the Actor stops output cleanly when it is reached.

## Limits and notes
- Only companies that use one of the 6 supported ATS are covered. Workday, iCIMS, Taleo and custom boards are not. For Workday career sites use our sister Actor [Workday Jobs & Hiring Signals](https://apify.com/goldenrod_serrano/workday-hiring-signals).
- SmartRecruiters doesn't return descriptions in its list endpoint, so `description` stays empty there.
- `posted_at` is the ATS's publish or first-published date. Greenhouse's `updated_at` changes on any edit.
- Data belongs to the companies that published it. Respect each ATS's terms and applicable law when you reuse it.

## Support
Report issues at https://github.com/paopao-studio-ai/ats-hiring-signals/issues · https://paopao-studio.pages.dev
