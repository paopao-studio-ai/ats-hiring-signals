# ATS Jobs Monitor for Recruiter BD — Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee

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
- **Recruiters and staffing-agency BD (primary).** Watch 100–500 target companies without relying on slow LinkedIn alerts. Deduped rows, long-open / repost flags, and "JD names an internal recruiter" help you pick where to pitch. Attach `company_domain` for outreach sequences.
- **Sales and lead generation.** A company that opens 10 sales roles this month has budget. Run a list of target accounts weekly and sort by `hiring_signal_score` or `posted_last_30_days`.
- **Job boards and aggregators.** Normalized fields across 6 ATS, with salary where the ATS exposes it (Ashby and Lever often do).
- **Job seekers and researchers.** Filter by keywords, location, remote, department and recency.

## Output
**Job rows** (`type: "job"`): `company_name, company_domain, title, seniority, department, team, location, country, remote, workplace_type, work_model, employment_type, posted_at, updated_at, days_open, is_long_open, is_repost, fingerprint, names_internal_recruiter, recruiter_contact, duplicate_count, duplicate_urls, url, apply_url, salary_*, description (optional), ats, company_slug, is_new`

**Company summary rows** (`type: "company_summary"`): `company_domain, total_open_jobs, matched_jobs, new_jobs_since_last_run, closed_jobs_since_last_run, closed_job_ids, net_change_since_last_run, long_open_jobs, repost_jobs_in_output, jobs_naming_recruiter, posted_last_7_days, posted_last_30_days, remote_share, seniority_mix, work_model_mix, top_departments, top_locations, sample_matched_titles, hiring_signal_score (0-100), checked_at`

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

## Built for recruiter BD (what buyers asked for)
Public demand from recruitment-agency BD threads (e.g. r/RecruitmentAgencies) and GTM engineers who want a daily company + job + domain feed (r/gtmengineering). Competitors already ship monitor mode (panda_studio, webdata_labs) and composite dedupe keys (bovi, fantastic-jobs). This Actor adds the BD-specific flags in one schema:

1. **Cross-post dedupe.** Every row gets a `fingerprint` (normalized company + title + location). With *Merge duplicate jobs* on (default), the run keeps one row per fingerprint and sets `duplicate_count` / `duplicate_urls` on the keeper — so one role across several boards stops looking like eleven.
2. **Long-open and repost flags.** `days_open` and `is_long_open` (default threshold 30 days, configurable). In monitor mode, a new ATS id whose fingerprint matches a role that just disappeared is marked `is_repost: true` — the classic "they've been trying to fill this" entry point for agencies.
3. **JD names an internal recruiter.** When *Include description* is on, we look for labeled contacts (`Recruiter: Jane Doe`), "reach out to Name", or a non-generic mailto. Sets `names_internal_recruiter`, `recruiter_contact`, `recruiter_signal`. No description → fields stay `null` (we never guess). A named internal recruiter is a hint the company may already have TA coverage.
4. **Company domain.** `company_domain` comes from a non-ATS careers URL you pasted (e.g. `https://www.anthropic.com/careers` → `anthropic.com`) or from `companyDomainMap`. We never invent `stripe.com` from the slug `stripe`.

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
Every company summary has `hiring_signal_score` (0-100), which adds three parts:
- volume (up to 40, from the number of open jobs)
- freshness (up to 40, from jobs posted in the last 7 and 30 days)
- relevance (up to 20, from jobs matching your filters)

It is a ranking aid, not a prediction.

## Change tracking: new, changed and closed jobs (optional)
Turn on **Change tracking** (`changeTracking: true`) and schedule the Actor. The first run is a baseline: every matching job is output as `change_type: "new"`. Later runs output only:
- **new** jobs (`change_type: "new"`, charged as a normal job row)
- **changed** jobs (`change_type: "changed"`, charged as a job row). A job counts as changed when its title, location, department, team, work model, employment type or salary changed. `changed_fields` lists what changed and `previous_values` holds the old values.
- **closed** jobs (`type: "job_closed"`, `change_type: "closed"`, **free**). These are jobs you got before that are gone from the board now.

State is kept per company in the named key-value store `ats-hiring-signals-state` under your **Monitor state key**. If a scan is incomplete (a SmartRecruiters board above 2,000 jobs), the new results are merged into the saved state and nothing is reported as closed. Jobs that were not delivered (cut by *Max jobs per company* or by your max charge) stay "new" for the next run. The company summary adds `new_jobs_since_last_run`, `changed_jobs_since_last_run`, `closed_jobs_since_last_run`, `net_change_since_last_run`, `is_baseline_run` and `scan_complete`. This setting is off by default, and when it is on it replaces *Only new jobs since last run* for that run.

## Explainable signal score (optional)
Turn on **Signal scoring** (`signalScoring: true`) to add a company-level `signal_score` (0-100), a `signal_tier` (hot ≥70, warm ≥45, cool ≥20, cold) and `score_reasons` (one plain-English line per component) to each summary. Job rows get `job_function`, `company_signal_score`, `company_signal_tier` and `score_reasons`.

| Component | Max | What it measures |
|---|---|---|
| new demand | 25 | roles posted in the last 30 / 7 days, plus "N new since the last run" when tracking |
| velocity | 20 | growth of open roles once tracking spans 7+ days (before that, this week's posting pace vs the 30-day average) |
| seniority | 15 | share of manager/director/executive and senior roles |
| department | 15 | open roles in your `scoreFunctions` (sales, customer_success, marketing, data_ai, engineering, product_design, finance_legal, people_ops), or in the largest function |
| hard to fill | 10 | share of roles open 30+ days |
| volume | 10 | total open roles |
| remote | 5 | share of remote roles |

Example from a real run (Oct 30 2026, `greenhouse:anthropic`, `scoreFunctions: ["sales"]`, a later tracked run with no changes):
```json
{"type":"company_summary","total_open_jobs":669,"new_jobs_since_last_run":0,"closed_jobs_since_last_run":0,"signal_score":81,"signal_tier":"hot",
 "score_reasons":["+25 new demand: 188 roles posted in the last 30 days (45 in the last 7); 0 new since the last run",
  "+15 department: 123 open roles in target functions (sales)",
  "+13 seniority: 126 manager/director/executive roles (19%), 48% senior-or-above",
  "+10 hard-to-fill: 491 roles open 30+ days (73%)","+10 volume: 669 open roles",
  "+7 velocity: 45 postings this week vs 43.7/week 30-day average (1.0x pace)","+1 remote: 9% of roles are remote"]}
```
A changed row looks like this: `{"type":"job","change_type":"changed","changed_fields":["title"],"previous_values":{"title":"..."},"first_seen_at":"2026-10-30T..."}`.

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
