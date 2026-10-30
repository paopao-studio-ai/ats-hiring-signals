"""Change tracking (new / changed / closed) and an explainable company hiring-signal score.

Pure functions, no I/O. The Actor glue loads/saves one small state record per company from an
Apify key-value store and calls these helpers. Shared verbatim by ats-hiring-signals and
workday-hiring-signals.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable

STATE_VERSION = 1
HISTORY_KEEP = 60

# ---------------------------------------------------------------- job function (department) classifier
FUNCTIONS: list[tuple[str, re.Pattern]] = [
    ("sales", re.compile(r"\b(sales|account (executive|manager|director)|business development|bdr|sdr|"
                         r"partnerships?|revenue|go[- ]to[- ]market|gtm|channel|solutions? (engineer|consultant)|pre-?sales)\b", re.I)),
    ("customer_success", re.compile(r"\b(customer (success|support|experience|service)|support engineer|"
                                    r"technical account|implementation|onboarding|client services)\b", re.I)),
    ("marketing", re.compile(r"\b(marketing|growth|brand|content|communications?|pr|demand gen|seo|social media|events?)\b", re.I)),
    ("data_ai", re.compile(r"\b(data (scientist|engineer|analyst)|machine learning|ml|ai|research scientist|"
                           r"analytics|deep learning|llm|applied scientist)\b", re.I)),
    ("engineering", re.compile(r"\b(engineer(ing)?|developer|software|sre|devops|infrastructure|security|"
                               r"architect|firmware|hardware|qa|test|platform|backend|frontend|full[- ]?stack)\b", re.I)),
    ("product_design", re.compile(r"\b(product (manager|management|owner|lead)|designer|design|ux|ui|user research)\b", re.I)),
    ("finance_legal", re.compile(r"\b(finance|accounting|accountant|controller|tax|treasury|fp&a|legal|counsel|"
                                 r"paralegal|compliance|audit|risk)\b", re.I)),
    ("people_ops", re.compile(r"\b(recruit(er|ing)|talent|people|hr|human resources|operations|office|"
                              r"workplace|facilities|procurement|supply chain|logistics)\b", re.I)),
]


def job_function(title: str | None, department: str | None = None) -> str:
    """Coarse job function from department first (when present), else title. Returns 'other' if unsure."""
    for text in (department, title):
        if not text:
            continue
        for name, rx in FUNCTIONS:
            if rx.search(text):
                return name
    return "other"


# ---------------------------------------------------------------- change tracking
def _norm(v: Any) -> Any:
    if isinstance(v, str):
        return re.sub(r"\s+", " ", v).strip() or None
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def snapshot(job: dict, fields: Iterable[str]) -> dict:
    return {f: _norm(job.get(f)) for f in fields}


def state_key(prefix: str, company_input: str) -> str:
    """Stable, KV-safe record key for one company input."""
    p = re.sub(r"[^a-zA-Z0-9!\-_.'()]", "_", prefix or "default")[:80]
    h = hashlib.sha1(company_input.strip().lower().encode()).hexdigest()[:16]
    return f"chg-{p}-{h}"


def diff_jobs(prev: dict | None, jobs: list[dict], fields: list[str], *, matched_ids: set,
              complete: bool = True, now: datetime | None = None, id_field: str = "job_id",
              fp_field: str = "fingerprint") -> dict:
    """Compare this scan with the stored state.

    prev: stored record ({"jobs": {id: {"f": {...}, "m": bool, "fs": iso, "fp": str}}, "history": [...]}) or None.
    Returns new/changed/closed sets, per-job change details and the next "jobs" map.
    A job is "changed" only when a tracked field had a value in both scans and the value differs, so
    switching optional detail fetching on/off does not create false changes.
    Incomplete scans (truncated listings) never report closures and merge into the old state instead of
    replacing it.
    """
    now = now or datetime.now(timezone.utc)
    today = now.isoformat().replace("+00:00", "Z")
    baseline = prev is None
    pj: dict = (prev or {}).get("jobs") or {}
    nxt: dict = {}
    new_ids, changed, closed = set(), {}, set()
    cur_ids = set()
    for j in jobs:
        jid = j.get(id_field)
        if not jid:
            continue
        jid = str(jid)
        cur_ids.add(jid)
        snap = snapshot(j, fields)
        old = pj.get(jid)
        rec = {"f": snap, "m": jid in matched_ids, "fs": (old or {}).get("fs") or today, "fp": j.get(fp_field)}
        if old is None:
            new_ids.add(jid)
        else:
            diffs = {f: [old["f"].get(f), snap.get(f)] for f in fields
                     if old["f"].get(f) is not None and snap.get(f) is not None and old["f"].get(f) != snap.get(f)}
            if diffs:
                changed[jid] = diffs
            rec["m"] = rec["m"] or (False if complete else old.get("m", False))
        nxt[jid] = rec
    if not baseline and complete:
        closed = set(pj) - cur_ids
    if not complete:
        for jid, old in pj.items():  # keep everything we could not see this time
            nxt.setdefault(jid, old)
    closed_fps = {pj[i].get("fp") for i in closed if pj[i].get("fp")}
    reposts = {i for i in new_ids if not baseline and nxt[i].get("fp") and nxt[i]["fp"] in closed_fps}
    return {"baseline": baseline, "complete": complete, "new": new_ids, "changed": changed, "closed": closed,
            "reposts": reposts, "closed_rows": {i: pj[i] for i in closed}, "jobs": nxt}


def finalize_jobs(next_jobs: dict, prev: dict | None, undelivered: Iterable[str]) -> dict:
    """Rows we detected but could not deliver because the max-charge limit stopped the run are rolled back so the
    next run reports them again."""
    pj = (prev or {}).get("jobs") or {}
    out = dict(next_jobs)
    for jid in undelivered:
        if jid in pj:
            out[jid] = pj[jid]
        else:
            out.pop(jid, None)
    return out


def next_record(prev: dict | None, jobs_map: dict, open_total: int | None, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    hist = list((prev or {}).get("history") or [])
    if open_total is not None:
        hist.append([now.isoformat().replace("+00:00", "Z"), int(open_total)])
    return {"v": STATE_VERSION, "jobs": jobs_map, "history": hist[-HISTORY_KEEP:],
            "updated_at": now.isoformat().replace("+00:00", "Z")}


# ---------------------------------------------------------------- scoring
LEADERSHIP = {"manager", "director", "executive"}
SENIOR_PLUS = {"senior", "lead", "manager", "director", "executive"}


def _scaled(n: float, cap_n: float, max_pts: int) -> int:
    if n <= 0:
        return 0
    return min(max_pts, round(max_pts * math.log1p(n) / math.log1p(cap_n)))


def _iso_age_days(iso: str, now: datetime) -> float | None:
    try:
        return (now - datetime.fromisoformat(iso.replace("Z", "+00:00"))).total_seconds() / 86400
    except Exception:
        return None


def score_company(jobs: list[dict], *, ages: list[int | None], open_total: int, new_since_last_run: int | None = None,
                  history: list | None = None, target_functions: Iterable[str] | None = None,
                  long_open_days: int = 30, now: datetime | None = None) -> dict:
    """Explainable 0-100 hiring-signal score.

    Components (max points): new demand 25, growth velocity 20, seniority 15, department focus 15,
    hard-to-fill (long-open) 10, volume 10, remote share 5. Every component adds one plain-English line
    to score_reasons, including the ones that scored 0, so the number can always be audited.
    """
    now = now or datetime.now(timezone.utc)
    n = len(jobs)
    reasons: list[str] = []
    comp: dict[str, int] = {}
    known = [a for a in ages if a is not None]
    last7 = sum(1 for a in known if a <= 7)
    last30 = sum(1 for a in known if a <= 30)

    # 1. new demand (date-based so the score does not depend on how often you run)
    comp["new_demand"] = _scaled(last30 + last7, 50, 25)
    extra = f"; {new_since_last_run} new since the last run" if new_since_last_run is not None else ""
    reasons.append(f"+{comp['new_demand']} new demand: {last30} roles posted in the last 30 days ({last7} in the last 7){extra}")

    # 2. growth velocity: tracked headcount trend if we have history, else posting pace
    # (only once tracking spans 7+ days, so the score doesn't depend on how often you run)
    prior = [h for h in (history or []) if (_iso_age_days(h[0], now) or 0) <= 30]
    if prior and prior[0][1] and (_iso_age_days(prior[0][0], now) or 0) >= 7:
        base = prior[0][1]
        growth = (open_total - base) / max(base, 1)
        days = max(_iso_age_days(prior[0][0], now) or 0, 0)
        comp["velocity"] = max(0, min(20, round(growth * 100)))
        trend = "growing" if growth > 0 else ("shrinking" if growth < 0 else "flat")
        reasons.append(f"+{comp['velocity']} velocity: open roles {base} -> {open_total} ({growth:+.0%}) over "
                       f"{days:.0f} days of tracking ({trend})")
    elif last30:
        weekly = last30 / 4.3
        ratio = last7 / max(weekly, 0.5)
        comp["velocity"] = max(0, min(20, round((ratio - 0.5) / 1.5 * 20)))
        reasons.append(f"+{comp['velocity']} velocity: {last7} postings this week vs {weekly:.1f}/week 30-day average "
                       f"({ratio:.1f}x pace)")
    else:
        comp["velocity"] = 0
        reasons.append("+0 velocity: no postings dated in the last 30 days")

    # 3. seniority
    sen = Counter(j.get("seniority") for j in jobs if j.get("seniority"))
    leaders = sum(sen[s] for s in LEADERSHIP)
    senior_share = (sum(sen[s] for s in SENIOR_PLUS) / n) if n else 0
    leader_share = leaders / n if n else 0
    comp["seniority"] = min(15, round(15 * min(1.0, 2 * leader_share + senior_share)))
    reasons.append(f"+{comp['seniority']} seniority: {leaders} manager/director/executive roles ({leader_share:.0%}), "
                   f"{senior_share:.0%} senior-or-above")

    # 4. department focus
    funcs = Counter(j.get("job_function") or job_function(j.get("title"), j.get("department")) for j in jobs)
    targets = [t.strip().lower() for t in (target_functions or []) if str(t).strip()]
    if targets:
        hit = sum(funcs[t] for t in targets)
        comp["department"] = _scaled(hit, 20, 15)
        reasons.append(f"+{comp['department']} department: {hit} open roles in target functions ({', '.join(targets)})")
    else:
        top = [(f, c) for f, c in funcs.most_common() if f != "other"][:1]
        if top:
            f, c = top[0]
            comp["department"] = _scaled(c, 20, 15) if c >= 2 else 0
            reasons.append(f"+{comp['department']} department: biggest build-out is {f} ({c} of {n} roles)")
        else:
            comp["department"] = 0
            reasons.append("+0 department: no clear function concentration")

    # 5. hard-to-fill
    long_n = sum(1 for j in jobs if j.get("is_long_open"))
    share = long_n / n if n else 0
    comp["hard_to_fill"] = min(10, round(10 * share * 1.5)) if long_n else 0
    reasons.append(f"+{comp['hard_to_fill']} hard-to-fill: {long_n} roles open {long_open_days}+ days ({share:.0%})")

    # 6. volume
    comp["volume"] = _scaled(open_total, 500, 10)
    reasons.append(f"+{comp['volume']} volume: {open_total} open roles")

    # 7. remote
    rshare = (sum(1 for j in jobs if j.get("remote")) / n) if n else 0
    comp["remote"] = 5 if rshare >= 0.5 else 3 if rshare >= 0.2 else 1 if rshare > 0 else 0
    reasons.append(f"+{comp['remote']} remote: {rshare:.0%} of roles are remote")

    score = max(0, min(100, sum(comp.values())))
    tier = "hot" if score >= 70 else "warm" if score >= 45 else "cool" if score >= 20 else "cold"
    order = sorted(range(len(reasons)), key=lambda i: -list(comp.values())[i])
    return {"signal_score": score, "signal_tier": tier, "score_reasons": [reasons[i] for i in order],
            "score_components": comp, "function_mix": dict(funcs.most_common())}
