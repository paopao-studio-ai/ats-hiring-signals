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

from .ats import filter_jobs, make_client, parse_target, resolve_and_fetch, summarize

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
    ats, slug, name, jobs = res
    kws = _list(inp.get("keywords"))
    matched = filter_jobs(
        jobs, keywords=kws, exclude=_list(inp.get("excludeKeywords")), locations=_list(inp.get("locations")),
        departments=_list(inp.get("departments")), remote_only=bool(inp.get("remoteOnly")),
        posted_within_days=inp.get("postedWithinDays") or None,
        seniority_levels=_list(inp.get("seniorityLevels")), work_models=_list(inp.get("workModels")),
    )
    key = f"{ats}:{slug}".lower()
    new_ids = None
    closed_ids = None
    if inp.get("onlyNewSinceLastRun"):
        prev = set(seen.get(key, []))
        cur = {j["job_id"] for j in jobs if j.get("job_id")}
        new_ids = {j["job_id"] for j in matched if j.get("job_id") not in prev} if key in seen else {j["job_id"] for j in matched}
        # Jobs seen last run but gone now = closed/filled. Skipped when the board listing may be truncated.
        truncated = ats == "smartrecruiters" and len(jobs) >= 2000
        if key in seen and not truncated:
            closed_ids = prev - cur
        matched = [j for j in matched if j.get("job_id") in new_ids]
    cap = inp.get("maxJobsPerCompany") or 0
    if cap:
        matched = matched[:cap]
    if new_ids is not None:
        # New jobs dropped by the cap stay "unseen" so the next run still delivers them.
        pushed = {j.get("job_id") for j in matched}
        seen[key] = sorted(cur - (new_ids - pushed))
    for j in matched:
        j["type"] = "job"
        j["input"] = raw
        j["company_name"] = j.get("company_name") or name
        if new_ids is not None:
            j["is_new"] = True
    explicit = bool(re.match(r"^(https?://)?[^/]*(greenhouse|lever|ashbyhq|workable|smartrecruiters|recruitee)", raw, re.I)
                    or re.match(r"^(greenhouse|lever|ashby|workable|smartrecruiters|recruitee)\s*:", raw, re.I))
    confidence = "exact" if explicit else "guessed"
    for j in matched:
        j["match_confidence"] = confidence
    summary = summarize(ats, slug, name, jobs, matched, new_ids, closed_ids=closed_ids)
    summary["match_confidence"] = confidence
    summary["input"] = raw
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
