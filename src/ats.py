"""Public ATS job-board fetchers + normalizer + hiring-signal summary.

Only official, unauthenticated job-board endpoints that the ATS vendors publish
for embedding job lists on career sites. No logins, no proxies, no scraping of
protected pages.
"""
from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import httpx

ATS_ORDER = ["greenhouse", "lever", "ashby", "workable", "smartrecruiters", "recruitee"]

URL_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(r"(?:job-boards|boards)(?:\.eu)?\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board/js\?for=([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_.-]+)", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([A-Za-z0-9_-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"([A-Za-z0-9-]+)\.recruitee\.com", re.I)),
]
_BAD_SLUGS = {"api", "www", "embed", "jobs", "v1", "careers", "static", "assets", "j", "o"}

UA = "Mozilla/5.0 (compatible; ATSHiringSignals/1.0; +https://paopao-studio.pages.dev)"


@dataclass
class Target:
    raw: str
    ats: str | None
    slug: str | None
    careers_url: str | None = None


def parse_target(raw: str) -> Target:
    s = (raw or "").strip()
    if not s:
        return Target(raw, None, None)
    m = re.match(r"^(greenhouse|lever|ashby|workable|smartrecruiters|recruitee)\s*[:/]\s*([A-Za-z0-9_.%-]+)$", s, re.I)
    if m:
        return Target(raw, m.group(1).lower(), m.group(2))
    for ats, pat in URL_PATTERNS:
        mm = pat.search(s)
        if mm and mm.group(1).lower() not in _BAD_SLUGS:
            return Target(raw, ats, mm.group(1).rstrip("."), s if s.startswith("http") else None)
    if re.match(r"^https?://", s, re.I) or "." in s:
        url = s if s.startswith("http") else "https://" + s
        return Target(raw, None, None, url)
    return Target(raw, None, s)  # bare slug -> auto-detect


def detect_from_html(text: str) -> tuple[str, str] | None:
    for ats, pat in URL_PATTERNS:
        for mm in pat.finditer(text):
            slug = mm.group(1).rstrip(".")
            if slug.lower() not in _BAD_SLUGS:
                return ats, slug
    return None


def strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(s)  # greenhouse double-escapes
    s = re.sub(r"<(br|/p|/li|/h\d|/div)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\xa0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def _iso(v: Any) -> str | None:
    if v is None or v == "":
        return None
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v / 1000 if v > 1e11 else v, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        d = datetime.fromisoformat(re.sub(r"\s*UTC$", "+00:00", str(v).strip()).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except Exception:
        return str(v)


REMOTE_RE = re.compile(r"\b(remote|anywhere|distributed|work from home|wfh)\b", re.I)


def _remote(flag: Any, *texts: Any) -> bool:
    if flag is True:
        return True
    return any(isinstance(t, str) and REMOTE_RE.search(t) for t in texts)


def _job(**kw: Any) -> dict[str, Any]:
    base = dict(
        ats=None, company_slug=None, company_name=None, job_id=None, title=None, department=None,
        team=None, location=None, country=None, remote=False, workplace_type=None,
        employment_type=None, posted_at=None, updated_at=None, url=None, apply_url=None,
        salary_min=None, salary_max=None, salary_currency=None, salary_text=None, description=None,
    )
    base.update(kw)
    return base


# ---------------- fetchers (return (company_name, [normalized jobs])) ----------------

def fetch_greenhouse(c: httpx.Client, slug: str, desc: bool):
    r = c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", params={"content": "true" if desc else "false"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    jobs = r.json().get("jobs", [])
    name = None
    dept_of: dict = {}
    try:
        b = c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
        if b.status_code == 200:
            name = b.json().get("name")
        dr = c.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/departments")
        if dr.status_code == 200:
            for d in dr.json().get("departments", []):
                for dj in d.get("jobs", []):
                    dept_of.setdefault(dj.get("id"), d.get("name"))
    except (httpx.HTTPError, ValueError):
        pass
    out = []
    for j in jobs:
        loc = (j.get("location") or {}).get("name")
        depts = ", ".join(d.get("name", "") for d in j.get("departments") or [] if d.get("name")) or dept_of.get(j.get("id"))
        name = name or j.get("company_name")
        out.append(_job(
            ats="greenhouse", company_slug=slug, company_name=j.get("company_name") or name, job_id=str(j.get("id")),
            title=j.get("title"), department=depts, location=loc, remote=_remote(None, loc, j.get("title")),
            posted_at=_iso(j.get("first_published")), updated_at=_iso(j.get("updated_at")),
            url=j.get("absolute_url"), apply_url=j.get("absolute_url"),
            description=strip_html(j.get("content")) if desc else None,
        ))
    return name or slug, out


def fetch_lever(c: httpx.Client, slug: str, desc: bool):
    for host in ("api.lever.co", "api.eu.lever.co"):
        r = c.get(f"https://{host}/v0/postings/{slug}", params={"mode": "json"})
        if r.status_code == 404:
            continue
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):  # {"ok":false,...}
            continue
        out = []
        for j in data:
            cat = j.get("categories") or {}
            sal = j.get("salaryRange") or {}
            locs = cat.get("allLocations") or ([cat.get("location")] if cat.get("location") else [])
            loc = " | ".join(locs) or None
            d = None
            if desc:
                d = "\n".join(x for x in [j.get("openingPlain") or j.get("descriptionPlain"), j.get("descriptionBodyPlain"),
                                          *[(l.get("text", "") + "\n" + strip_html(l.get("content"))) for l in j.get("lists") or []],
                                          j.get("additionalPlain")] if x)
            out.append(_job(
                ats="lever", company_slug=slug, company_name=slug, job_id=j.get("id"), title=j.get("text"),
                department=cat.get("department"), team=cat.get("team"), location=loc, country=j.get("country"),
                remote=_remote(j.get("workplaceType") == "remote", loc), workplace_type=j.get("workplaceType"),
                employment_type=cat.get("commitment"), posted_at=_iso(j.get("createdAt")),
                url=j.get("hostedUrl"), apply_url=j.get("applyUrl"),
                salary_min=sal.get("min"), salary_max=sal.get("max"), salary_currency=sal.get("currency"),
                salary_text=(f"{sal.get('min')}-{sal.get('max')} {sal.get('currency')} {sal.get('interval') or ''}".strip() if sal else None),
                description=d,
            ))
        return slug, out
    return None


def fetch_ashby(c: httpx.Client, slug: str, desc: bool):
    r = c.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", params={"includeCompensation": "true"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    if not isinstance(data, dict) or "jobs" not in data:
        return None
    out = []
    for j in data["jobs"]:
        if j.get("isListed") is False:
            continue
        comp = j.get("compensation") or {}
        smin = smax = cur = None
        for comp_t in comp.get("summaryComponents") or []:
            if comp_t.get("compensationType") == "Salary":
                smin, smax, cur = comp_t.get("minValue"), comp_t.get("maxValue"), comp_t.get("currencyCode")
                break
        addr = ((j.get("address") or {}).get("postalAddress") or {})
        out.append(_job(
            ats="ashby", company_slug=slug, company_name=slug, job_id=j.get("id"), title=j.get("title"),
            department=j.get("department"), team=j.get("team"), location=j.get("location"),
            country=addr.get("addressCountry"), remote=_remote(j.get("isRemote"), j.get("location")),
            workplace_type=j.get("workplaceType"), employment_type=j.get("employmentType"),
            posted_at=_iso(j.get("publishedAt")), url=j.get("jobUrl"), apply_url=j.get("applyUrl"),
            salary_min=smin, salary_max=smax, salary_currency=cur,
            salary_text=comp.get("compensationTierSummary") or comp.get("scrapeableCompensationSalarySummary"),
            description=(j.get("descriptionPlain") if desc else None),
        ))
    return slug, out


def fetch_workable(c: httpx.Client, slug: str, desc: bool):
    r = c.get(f"https://apply.workable.com/api/v1/widget/accounts/{slug}", params={"details": "true" if desc else "false"})
    if r.status_code == 404:
        return None
    r.raise_for_status()
    data = r.json()
    out = []
    for j in data.get("jobs", []):
        loc = ", ".join(x for x in [j.get("city"), j.get("state"), j.get("country")] if x) or None
        out.append(_job(
            ats="workable", company_slug=slug, company_name=data.get("name") or slug, job_id=j.get("shortcode"),
            title=j.get("title"), department=j.get("department") or None, location=loc, country=j.get("country"),
            remote=_remote(j.get("telecommuting"), loc), employment_type=j.get("employment_type") or None,
            posted_at=_iso(j.get("published_on") or j.get("created_at")), url=j.get("url") or j.get("shortlink"),
            apply_url=j.get("application_url"), description=strip_html(j.get("description")) if desc else None,
        ))
    return data.get("name") or slug, out


def fetch_smartrecruiters(c: httpx.Client, slug: str, desc: bool, cap: int = 2000):
    out, offset, name = [], 0, slug
    while True:
        r = c.get(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings", params={"limit": 100, "offset": offset})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        data = r.json()
        content = data.get("content") or []
        if offset == 0 and not content:
            return None  # unknown company returns empty list
        for j in content:
            loc = j.get("location") or {}
            name = (j.get("company") or {}).get("name") or name
            jid = j.get("id")
            out.append(_job(
                ats="smartrecruiters", company_slug=slug, company_name=name, job_id=jid, title=j.get("name"),
                department=(j.get("department") or {}).get("label") or (j.get("function") or {}).get("label"),
                location=loc.get("fullLocation") or ", ".join(x for x in [loc.get("city"), loc.get("country")] if x),
                country=(loc.get("country") or "").upper() or None, remote=_remote(loc.get("remote"), loc.get("fullLocation")),
                workplace_type="remote" if loc.get("remote") else ("hybrid" if loc.get("hybrid") else "onsite"),
                employment_type=(j.get("typeOfEmployment") or {}).get("label"), posted_at=_iso(j.get("releasedDate")),
                url=f"https://jobs.smartrecruiters.com/{slug}/{jid}", apply_url=f"https://jobs.smartrecruiters.com/{slug}/{jid}",
            ))
        offset += len(content)
        if not content or offset >= (data.get("totalFound") or 0) or offset >= cap:
            break
    return name, out


def fetch_recruitee(c: httpx.Client, slug: str, desc: bool):
    r = c.get(f"https://{slug}.recruitee.com/api/offers/", timeout=12)
    if r.status_code in (404, 403) or "json" not in r.headers.get("content-type", ""):
        return None
    r.raise_for_status()
    out, name = [], slug
    for j in r.json().get("offers", []):
        name = j.get("company_name") or name
        sal = j.get("salary") or {}
        out.append(_job(
            ats="recruitee", company_slug=slug, company_name=name, job_id=str(j.get("id")), title=j.get("title") or j.get("position"),
            department=j.get("department"), location=j.get("location"), country=j.get("country_code"),
            remote=_remote(j.get("remote"), j.get("location")), employment_type=j.get("employment_type_code"),
            posted_at=_iso(j.get("published_at") or j.get("created_at")), url=j.get("careers_url"), apply_url=j.get("careers_apply_url"),
            salary_min=sal.get("min"), salary_max=sal.get("max"), salary_currency=sal.get("currency"),
            description=strip_html((j.get("description") or "") + "\n" + (j.get("requirements") or "")) if desc else None,
        ))
    return name, out


FETCHERS = {
    "greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
    "workable": fetch_workable, "smartrecruiters": fetch_smartrecruiters, "recruitee": fetch_recruitee,
}


def resolve_and_fetch(c: httpx.Client, t: Target, desc: bool) -> tuple[str, str, str, list[dict]] | None:
    """Return (ats, slug, company_name, jobs) or None if not found."""
    if t.ats is None and t.careers_url:
        try:
            r = c.get(t.careers_url, follow_redirects=True)
            found = detect_from_html(str(r.url)) or detect_from_html(r.text)
            if found:
                t.ats, t.slug = found
        except httpx.HTTPError:
            pass
        if t.ats is None:
            # JS-rendered careers pages hide the board; fall back to the domain name as a slug guess.
            host = re.sub(r"^https?://", "", t.careers_url).split("/")[0].lower()
            parts = [p for p in host.split(".") if p not in ("www", "careers", "jobs")]
            if len(parts) >= 2:
                t.slug = parts[-2]
            else:
                return None
    if t.ats:
        res = FETCHERS[t.ats](c, t.slug, desc)
        return (t.ats, t.slug, res[0], res[1]) if res is not None else None
    # Bare slug: the same slug can exist on several ATS (often an old, near-empty board).
    # Try all six and keep the board with the most open jobs.
    best = None
    for ats in ATS_ORDER:
        try:
            res = FETCHERS[ats](c, t.slug, desc)
        except (httpx.HTTPError, ValueError):
            continue
        if res is not None and (best is None or len(res[1]) > len(best[3])):
            best = (ats, t.slug, res[0], res[1])
    return best


# ---------------- filtering + summary ----------------

def _any_kw(text: str, kws: Iterable[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in kws)


def filter_jobs(jobs: list[dict], keywords=None, exclude=None, locations=None, departments=None,
                remote_only=False, posted_within_days=None, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    out = []
    for j in jobs:
        hay = " ".join(str(j.get(k) or "") for k in ("title", "department", "team"))
        if keywords and not _any_kw(hay + " " + (j.get("description") or ""), keywords):
            continue
        if exclude and _any_kw(j.get("title") or "", exclude):
            continue
        if locations and not (_any_kw(" ".join(str(j.get(k) or "") for k in ("location", "country")), locations)
                              or (j.get("remote") and _any_kw("remote", locations))):
            continue
        if departments and not _any_kw(" ".join(str(j.get(k) or "") for k in ("department", "team")), departments):
            continue
        if remote_only and not j.get("remote"):
            continue
        if posted_within_days:
            p = j.get("posted_at")
            try:
                if not p or datetime.fromisoformat(p.replace("Z", "+00:00")) < now - timedelta(days=posted_within_days):
                    continue
            except ValueError:
                continue
        out.append(j)
    return out


def summarize(ats: str, slug: str, name: str, all_jobs: list[dict], matched: list[dict], new_ids: set | None,
              now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)

    def age_days(j):
        try:
            return (now - datetime.fromisoformat(j["posted_at"].replace("Z", "+00:00"))).days
        except Exception:
            return None

    ages = [a for a in (age_days(j) for j in all_jobs) if a is not None]
    last7 = sum(1 for a in ages if a <= 7)
    last30 = sum(1 for a in ages if a <= 30)
    total = len(all_jobs)
    remote_share = round(sum(1 for j in all_jobs if j.get("remote")) / total, 2) if total else 0
    depts = Counter(j.get("department") for j in all_jobs if j.get("department"))
    locs = Counter((j.get("location") or "").split(" | ")[0] for j in all_jobs if j.get("location"))
    # Hiring-signal score 0-100: volume (log-ish), freshness, and matched relevance.
    vol = min(40, total * 2 if total < 10 else 20 + min(20, total // 5))
    fresh = min(40, last30 * 2 + last7 * 3)
    rel = min(20, len(matched) * 4)
    return {
        "type": "company_summary", "ats": ats, "company_slug": slug, "company_name": name,
        "total_open_jobs": total, "matched_jobs": len(matched),
        "new_jobs_since_last_run": (len(new_ids) if new_ids is not None else None),
        "posted_last_7_days": last7, "posted_last_30_days": last30, "remote_share": remote_share,
        "top_departments": [d for d, _ in depts.most_common(5)], "top_locations": [l for l, _ in locs.most_common(5)],
        "sample_matched_titles": [j["title"] for j in matched[:5]],
        "hiring_signal_score": int(vol + fresh + rel),
        "checked_at": now.isoformat().replace("+00:00", "Z"),
    }


def make_client(timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.8"},
                        follow_redirects=True, transport=transport)
