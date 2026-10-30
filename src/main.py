"""Apify Actor: ATS Jobs & Hiring Signals (Greenhouse, Lever, Ashby, Workable, SmartRecruiters, Recruitee).

Pure logic lives in src/ats.py; this file is only the Actor glue (input, state, charging, output).
Charged events (pay per event):
  job              one normalized job posting pushed to the dataset
  company-summary  one per-company hiring-signal summary row
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

from apify import Actor

from .ats import (apply_bd_fields, filter_jobs, job_fingerprint, make_client, merge_duplicate_jobs,
                 parse_target, resolve_and_fetch, summarize)
from .tickers import load_index, lookup, norm

STATE_STORE = "ats-hiring-signals-state"
SAMPLE = ["greenhouse:anthropic", "https://jobs.lever.co/palantir", "ashby:ramp"]


def _list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    return [str(x).strip() for x in v if str(x).strip()]


def _process_company(raw: str, inp: dict, seen: dict) -> dict:
    """Sync work for one company: resolve, fetch, filter, diff. Returns a result dict."""
    t = parse_target(raw)
    desc = bool(inp.get("includeDescription"))
    with make_client() as c:
        res = resolve_and_fetch(c, t, desc)
    if res is None:
        return {"input": raw, "error": "No public Greenhouse/Lever/Ashby/Workable/SmartRecruiters/Recruitee board found for this input."}
    ats, slug, name, jobs, domain = res
    # Optional user map wins over careers-URL domain; never invent from the slug alone.
    domain_map = {str(k).lower(): str(v).lower().lstrip(".") for k, v in (inp.get("companyDomainMap") or {}).items() if v}
    domain = domain_map.get((name or "").lower()) or domain_map.get(slug.lower()) or domain_map.get(raw.lower()) or domain
    long_open_days = int(inp.get("longOpenDays") or 30)
    key = f"{ats}:{slug}".lower()
    # Fingerprint company identity: name/domain so the same role on Greenhouse+Lever (or slug+careers URL) merges.
    fp_company = (name or domain or slug or key)
    for j in jobs:
        apply_bd_fields(j, fp_company, long_open_days=long_open_days, company_domain=domain)
    kws = _list(inp.get("keywords"))
    matched = filter_jobs(
        jobs, keywords=kws, exclude=_list(inp.get("excludeKeywords")), locations=_list(inp.get("locations")),
        departments=_list(inp.get("departments")), remote_only=bool(inp.get("remoteOnly")),
        posted_within_days=inp.get("postedWithinDays") or None,
        seniority_levels=_list(inp.get("seniorityLevels")), work_models=_list(inp.get("workModels")),
    )
    new_ids = None
    closed_ids = None
    fp_key = f"{key}::fp"
    prev_fps: dict = seen.get(fp_key) or {}
    if inp.get("onlyNewSinceLastRun"):
        prev = set(seen.get(key, []))
        cur = {j["job_id"] for j in jobs if j.get("job_id")}
        new_ids = {j["job_id"] for j in matched if j.get("job_id") not in prev} if key in seen else {j["job_id"] for j in matched}
        # Jobs seen last run but gone now = closed/filled. Skipped when the board listing may be truncated.
        truncated = ats == "smartrecruiters" and len(jobs) >= 2000
        if key in seen and not truncated:
            closed_ids = prev - cur
        closed_fps = {prev_fps[i] for i in (closed_ids or []) if i in prev_fps}
        matched = [j for j in matched if j.get("job_id") in new_ids]
        for j in matched:
            # Same title+location reappears under a new ATS id after vanishing = classic repost.
            j["is_repost"] = bool(key in seen and j.get("fingerprint") in closed_fps)
    cap = inp.get("maxJobsPerCompany") or 0
    if cap:
        matched = matched[:cap]
    if new_ids is not None:
        # New jobs dropped by the cap stay "unseen" so the next run still delivers them.
        pushed = {j.get("job_id") for j in matched}
        seen[key] = sorted(cur - (new_ids - pushed))
        seen[fp_key] = {j["job_id"]: j.get("fingerprint") or job_fingerprint(fp_company, j.get("title"), j.get("location"))
                        for j in jobs if j.get("job_id") and j["job_id"] in set(seen[key])}
    for j in matched:
        j["type"] = "job"
        j["input"] = raw
        j["company_name"] = j.get("company_name") or name
        if new_ids is not None:
            j["is_new"] = True
        elif "is_repost" not in j:
            j["is_repost"] = None
    explicit = bool(re.match(r"^(https?://)?[^/]*(greenhouse|lever|ashbyhq|workable|smartrecruiters|recruitee)", raw, re.I)
                    or re.match(r"^(greenhouse|lever|ashby|workable|smartrecruiters|recruitee)\s*:", raw, re.I))
    confidence = "exact" if explicit else "guessed"
    for j in matched:
        j["match_confidence"] = confidence
    summary = summarize(ats, slug, name, jobs, matched, new_ids, closed_ids=closed_ids)
    summary["match_confidence"] = confidence
    summary["input"] = raw
    summary["company_domain"] = domain
    return {"input": raw, "ats": ats, "slug": slug, "jobs": matched, "summary": summary}


async def main() -> None:
    async with Actor:
        inp: dict[str, Any] = await Actor.get_input() or {}
        companies = _list(inp.get("companies"))
        sample = False
        if not companies:
            companies, sample = SAMPLE, True
            inp = {**inp, "maxJobsPerCompany": 5, "onlyNewSinceLastRun": False}
            Actor.log.info("No companies given - running the built-in sample (not charged).")
        companies = list(dict.fromkeys(companies))[:500]

        state_kv = await Actor.open_key_value_store(name=STATE_STORE) if inp.get("onlyNewSinceLastRun") else None
        state_key = (inp.get("stateKey") or "default").strip() or "default"
        seen: dict = (await state_kv.get_value(state_key)) or {} if state_kv else {}

        sem = asyncio.Semaphore(int(inp.get("maxConcurrency") or 5))

        async def run_one(raw: str):
            async with sem:
                try:
                    return await asyncio.to_thread(_process_company, raw, inp, seen)
                except Exception as e:  # noqa: BLE001
                    return {"input": raw, "error": f"{type(e).__name__}: {e}"[:300]}

        results = await asyncio.gather(*(run_one(r) for r in companies))

        # Cross-company / cross-board dedupe: same fingerprint kept once (buyer pain: 1 role counted 11 times).
        if inp.get("dedupeJobs", True):
            flat = [j for r in results if "error" not in r for j in r["jobs"]]
            merged = {j.get("fingerprint"): j for j in merge_duplicate_jobs(flat)}
            for r in results:
                if "error" in r:
                    continue
                r["jobs"] = [j for j in r["jobs"] if merged.get(j.get("fingerprint")) is j]
                if r["jobs"] is not None and r.get("summary"):
                    r["summary"]["matched_jobs"] = len(r["jobs"])

        if inp.get("addTickers", True) and not sample:
            idx = await asyncio.to_thread(load_index)
            user_map = {norm(k): str(v).upper() for k, v in (inp.get("tickerMap") or {}).items() if v}
            for r in results:
                if "error" in r:
                    continue
                names = [r["summary"].get("company_name"), r["summary"].get("company_slug")]
                hit = next(({"ticker": user_map[norm(n)], "cik": None, "sec_title": None, "ticker_match": "user"}
                            for n in names if n and norm(n) in user_map), None) or lookup(idx, *names)
                r["summary"].update(hit)
                for j in r["jobs"]:
                    j["ticker"] = hit["ticker"]
        want_jobs = inp.get("outputJobs", True)
        want_summary = inp.get("outputCompanySummary", True)
        totals = {"companies": len(companies), "found": 0, "jobs_pushed": 0, "errors": 0}
        stop = False
        for r in results:
            if "error" in r:
                totals["errors"] += 1
                Actor.log.warning(f"{r['input']}: {r['error']}")
                await Actor.push_data({"type": "error", "input": r["input"], "error": r["error"]})  # never charged
                continue
            totals["found"] += 1
            if want_summary and not stop:
                if sample:
                    await Actor.push_data(r["summary"])
                else:
                    cr = await Actor.push_data(r["summary"], charged_event_name="company-summary")
                    stop = stop or cr.event_charge_limit_reached
            if want_jobs:
                for j in r["jobs"]:
                    if stop:
                        break
                    if sample:
                        await Actor.push_data(j)
                    else:
                        cr = await Actor.push_data(j, charged_event_name="job")
                        stop = cr.event_charge_limit_reached
                    totals["jobs_pushed"] += 1
            if stop:
                Actor.log.info("Max charge per run reached - stopping output.")
                break

        if state_kv:
            await state_kv.set_value(state_key, seen)
        await Actor.set_value("RUN_SUMMARY", {**totals, "sample": sample})
        Actor.log.info(f"Done: {totals}")
        if totals["found"] == 0:
            await Actor.fail(status_message="No job boards found for any input. Check the slugs/URLs.")
