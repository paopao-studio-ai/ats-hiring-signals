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
from .signals import diff_jobs, finalize_jobs, job_function, next_record, score_company, state_key as chg_key
from .tickers import load_index, lookup, norm

STATE_STORE = "ats-hiring-signals-state"
SAMPLE = ["greenhouse:anthropic", "https://jobs.lever.co/palantir", "ashby:ramp"]
# Fields compared between runs in change-tracking mode (a change in any of them = "changed").
TRACKED_FIELDS = ["title", "location", "department", "team", "work_model", "employment_type",
                  "salary_min", "salary_max", "salary_currency", "salary_text"]


def _list(v: Any) -> list[str]:
    if not v:
        return []
    if isinstance(v, str):
        v = v.replace("\n", ",").split(",")
    return [str(x).strip() for x in v if str(x).strip()]


def _process_company(raw: str, inp: dict, seen: dict, chg_prev: dict | None = None) -> dict:
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
    track = bool(inp.get("changeTracking"))
    diff = None
    if track:
        truncated = ats == "smartrecruiters" and len(jobs) >= 2000
        diff = diff_jobs(chg_prev, jobs, TRACKED_FIELDS, matched_ids={str(j["job_id"]) for j in matched if j.get("job_id")},
                         complete=not truncated)
        rows = []
        for j in matched:
            jid = str(j.get("job_id"))
            if jid in diff["new"]:
                j["change_type"] = "new"
            elif jid in diff["changed"]:
                j["change_type"] = "changed"
                j["changed_fields"] = sorted(diff["changed"][jid])
                j["previous_values"] = {f: v[0] for f, v in diff["changed"][jid].items()}
            else:
                continue
            j["is_repost"] = jid in diff["reposts"]
            j["first_seen_at"] = diff["jobs"][jid]["fs"]
            rows.append(j)
        matched = rows
        new_ids = {str(j["job_id"]) for j in rows if j["change_type"] == "new"}
        closed_ids = diff["closed"] if diff["complete"] and not diff["baseline"] else None
    elif inp.get("onlyNewSinceLastRun"):
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
    undelivered: list[str] = []
    if cap:
        # The cap only limits output. With change tracking every scanned job is recorded as seen (the full
        # list), so capped-out rows are NOT re-reported (or re-charged) next run. Only rows lost to the
        # platform's max-charge limit are rolled back (see main()).
        matched = matched[:cap]
    if new_ids is not None and not track:
        # New jobs dropped by the cap stay "unseen" so the next run still delivers them.
        pushed = {j.get("job_id") for j in matched}
        seen[key] = sorted(cur - (new_ids - pushed))
        seen[fp_key] = {j["job_id"]: j.get("fingerprint") or job_fingerprint(fp_company, j.get("title"), j.get("location"))
                        for j in jobs if j.get("job_id") and j["job_id"] in set(seen[key])}
    for j in matched:
        j["type"] = "job"
        j["input"] = raw
        j["company_name"] = j.get("company_name") or name
        if track:
            j["is_new"] = j["change_type"] == "new"
        elif new_ids is not None:
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
    out = {"input": raw, "ats": ats, "slug": slug, "jobs": matched, "summary": summary}
    if track:
        changed_n = sum(1 for j in matched if j.get("change_type") == "changed")
        summary.update({
            "changed_jobs_since_last_run": None if diff["baseline"] else len(diff["changed"]),
            "new_jobs_since_last_run": None if diff["baseline"] else len(diff["new"]),
            "is_baseline_run": diff["baseline"], "scan_complete": diff["complete"],
            "matched_jobs": len(matched),
        })
        if summary.get("closed_jobs_since_last_run") is not None:
            summary["net_change_since_last_run"] = len(diff["new"]) - len(diff["closed"])
        closed_rows = []
        for jid, old in diff["closed_rows"].items():
            if not old.get("m"):
                continue  # only closures of jobs that matched your filters
            closed_rows.append({"type": "job_closed", "change_type": "closed", "input": raw, "ats": ats,
                                "company_slug": slug, "company_name": name, "job_id": jid, **old.get("f", {}),
                                "first_seen_at": old.get("fs"), "closed_detected_at": summary["checked_at"],
                                "fingerprint": old.get("fp")})
        out.update({"closed_rows": closed_rows, "chg_prev": chg_prev, "chg_jobs": diff["jobs"],
                    "undelivered": undelivered, "open_total": None if not diff["complete"] else len(jobs),
                    "changed_count": changed_n})
    if inp.get("signalScoring"):
        for j in jobs:
            j["job_function"] = job_function(j.get("title"), j.get("department"))
        hist = (chg_prev or {}).get("history") if track else None
        sc = score_company(jobs, ages=[j.get("days_open") for j in jobs], open_total=len(jobs),
                           new_since_last_run=(len(diff["new"]) if track and not diff["baseline"] else None),
                           history=hist, target_functions=_list(inp.get("scoreFunctions")),
                           long_open_days=long_open_days)
        summary.update(sc)
        for j in matched:
            j["company_signal_score"] = sc["signal_score"]
            j["company_signal_tier"] = sc["signal_tier"]
            j["score_reasons"] = sc["score_reasons"]
    return out


async def main() -> None:
    async with Actor:
        inp: dict[str, Any] = await Actor.get_input() or {}
        companies = _list(inp.get("companies"))
        sample = False
        if not companies:
            companies, sample = SAMPLE, True
            inp = {**inp, "maxJobsPerCompany": 5, "onlyNewSinceLastRun": False, "changeTracking": False}
            Actor.log.info("No companies given - running the built-in sample (not charged).")
        companies = list(dict.fromkeys(companies))[:500]

        track = bool(inp.get("changeTracking"))
        if track and inp.get("onlyNewSinceLastRun"):
            Actor.log.info("changeTracking is on - it replaces onlyNewSinceLastRun for this run (legacy state untouched).")
            inp = {**inp, "onlyNewSinceLastRun": False}
        state_kv = await Actor.open_key_value_store(name=STATE_STORE) if (inp.get("onlyNewSinceLastRun") or track) else None
        state_key = (inp.get("stateKey") or "default").strip() or "default"
        seen: dict = ((await state_kv.get_value(state_key)) or {}) if state_kv and not track else {}
        chg_prev: dict = {}
        if track:
            for raw in companies:
                chg_prev[raw] = await state_kv.get_value(chg_key(state_key, raw))

        sem = asyncio.Semaphore(int(inp.get("maxConcurrency") or 5))

        async def run_one(raw: str):
            async with sem:
                try:
                    return await asyncio.to_thread(_process_company, raw, inp, seen, chg_prev.get(raw))
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
        if track:
            totals.update({"new_rows": 0, "changed_rows": 0, "closed_rows": 0})
        stop = False
        for r in results:
            r["reached"] = not stop
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
                        r.setdefault("undelivered", []).append(str(j.get("job_id")))
                        continue
                    if sample:
                        await Actor.push_data(j)
                    else:
                        cr = await Actor.push_data(j, charged_event_name="job")
                        stop = cr.event_charge_limit_reached
                    totals["jobs_pushed"] += 1
                    if track:
                        totals[f"{j['change_type']}_rows"] += 1
                if track and not stop:
                    for c in r.get("closed_rows") or []:
                        await Actor.push_data(c)  # closure notices are free
                        totals["closed_rows"] += 1
                elif track:  # not delivered -> keep them in state so the next run reports them
                    r.setdefault("undelivered", []).extend(c["job_id"] for c in r.get("closed_rows") or [])
            if stop:
                Actor.log.info("Max charge per run reached - stopping output.")
                break

        if state_kv and track:
            for r in results:
                if "error" in r or not r.get("reached") or "chg_jobs" not in r:
                    continue  # never reached (charge limit) -> keep old state so nothing is lost
                jobs_map = finalize_jobs(r["chg_jobs"], r["chg_prev"], r.get("undelivered") or [])
                await state_kv.set_value(chg_key(state_key, r["input"]),
                                         next_record(r["chg_prev"], jobs_map, r.get("open_total")))
        elif state_kv:
            await state_kv.set_value(state_key, seen)
        await Actor.set_value("RUN_SUMMARY", {**totals, "sample": sample})
        Actor.log.info(f"Done: {totals}")
        if totals["found"] == 0:
            await Actor.fail(status_message="No job boards found for any input. Check the slugs/URLs.")
