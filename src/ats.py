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


# ---------------- enrichment (title -> seniority, flags -> work model) ----------------

SENIORITY_LEVELS = ["intern", "entry", "mid", "senior", "lead", "manager", "director", "executive"]
_SEN_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("intern", re.compile(r"\b(intern(ship)?|co-?op|apprentice(ship)?|trainee|working student|werkstudent|stagiaire|praktikum)\b", re.I)),
    ("executive", re.compile(r"\b(chief|c[etfom]o|cro|cpo|ciso|president|vp|svp|evp|vice[- ]president|general manager|managing director|founder)\b", re.I)),
    ("director", re.compile(r"\b(director|head of|head,)\b", re.I)),
    ("lead", re.compile(r"\b(lead|principal|staff(?! accountant| nurse| assistant)|architect|distinguished)\b", re.I)),
    # "Product/Account/Program/Project Manager" are usually individual-contributor roles, not people managers.
    ("manager", re.compile(r"\b(?<!product )(?<!account )(?<!program )(?<!project )(?<!success )(?<!marketing )manager\b|\bmgr\b|\bsupervisor\b", re.I)),
    ("senior", re.compile(r"\b(senior|sr\.?|snr|iii|iv)(?=\W|$)", re.I)),
    ("entry", re.compile(r"\b(junior|jr\.?|entry[- ]level|graduate|new grad|associate|assistant)(?=\W|$)|\b(i|1)$", re.I)),
]


def seniority(title: str | None) -> str | None:
    """Rule-based seniority from the job title. One of SENIORITY_LEVELS, or None for an empty title."""
    t = (title or "").strip()
    if not t:
        return None
    for level, pat in _SEN_RULES:
        if pat.search(t):
            return level
    return "mid"


HYBRID_RE = re.compile(r"\bhybrid\b", re.I)
ONSITE_RE = re.compile(r"\b(on-?site|in[- ]office|in[- ]person)\b", re.I)


def work_model(workplace_type: Any, remote: bool, *texts: Any) -> str | None:
    """Normalize to 'remote' | 'hybrid' | 'onsite', or None when the board doesn't say."""
    w = re.sub(r"[^a-z]", "", str(workplace_type or "").lower())
    if w in ("remote", "hybrid"):
        return w
    if w in ("onsite", "inoffice", "office"):
        return "onsite"
    blob = " ".join(t for t in texts if isinstance(t, str))
    if HYBRID_RE.search(blob):
        return "hybrid"
    if remote or REMOTE_RE.search(blob):
        return "remote"
    if ONSITE_RE.search(blob):
        return "onsite"
    return None



# ---------------- BD signals: fingerprint, long-open, recruiter, domain ----------------
# Evidence: r/RecruitmentAgencies BD flow (dupes across sites, reposts = best entry, JD names
# internal recruiter); r/gtmengineering wants company + job + domain daily. Competitors
# (bovi composite id, fantastic-jobs title+org+location dedupe, panda_studio/webdata_labs monitor)
# cover monitor/dedupe; named-recruiter flag is rarer in public READMEs.

ATS_HOST_RE = re.compile(
    r"(?:^|\.)((?:job-boards|boards)(?:\.eu)?\.greenhouse\.io|boards-api\.greenhouse\.io|"
    r"jobs\.(?:eu\.)?lever\.co|jobs\.ashbyhq\.com|apply\.workable\.com|"
    r"(?:jobs|careers)\.smartrecruiters\.com|recruitee\.com)$",
    re.I,
)
_PUBLIC_SUFFIX_2 = {"co.uk", "com.au", "co.jp", "com.sg", "com.hk", "co.in", "com.br", "co.nz", "com.mx", "co.kr", "com.tr"}
_GENERIC_MAIL = re.compile(r"^(jobs?|careers?|apply|recruiting|talent|hr|hello|info|contact|noreply)@", re.I)
# Require a Person-Name after the label so "hiring manager will discuss" does not fire.
_RECRUITER_LABEL = re.compile(
    r"(?i)(?:^|\n|[.;])\s*(?:recruiter|talent acquisition(?:\s+(?:partner|specialist|manager))?|"
    r"ta partner|recruiting(?:\s+contact)?|sourcer)\s*[:\-–]\s*"
    r"([A-Z][a-z]+(?:[ \t]+[A-Z][a-z]+){0,2})",
)
_CONTACT_NAME = re.compile(
    r"(?i)(?:please\s+contact|reach\s+out\s+to|speak\s+(?:with|to)|email)\s+"
    r"(?-i:([A-Z][a-z]{1,}(?:[ \t]+[A-Z][a-z]{1,}){0,2}))(?=\s+(?:at|via|on)\b|\s*<|\s*\()",
)
_BAD_CONTACT = re.compile(
    r"^(?:us|me|him|her|them|our|the|a|an|hr|ta|team|recruiting|talent|careers|jobs|staff|someone|anyone)$",
    re.I,
)
_MAILTO = re.compile(r"mailto:([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})", re.I)





def registrable_domain(host: str | None) -> str | None:
    if not host:
        return None
    host = host.lower().split(":")[0].strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or ATS_HOST_RE.search(host):
        return None
    parts = host.split(".")
    if len(parts) < 2:
        return None
    last2 = ".".join(parts[-2:])
    if last2 in _PUBLIC_SUFFIX_2 and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last2


def company_domain_from_url(url: str | None) -> str | None:
    if not url:
        return None
    m = re.match(r"^https?://([^/]+)", url.strip(), re.I)
    return registrable_domain(m.group(1) if m else None)


def job_fingerprint(company_key: str | None, title: str | None, location: str | None) -> str:
    """Stable key for cross-post / re-list detection. Lowercased title+location under a company."""
    def norm_part(x: str | None) -> str:
        t = re.sub(r"[^a-z0-9]+", " ", (x or "").lower())
        return re.sub(r"\s+", " ", t).strip()
    return "|".join([norm_part(company_key), norm_part(title), norm_part(location)])


def days_open(posted_at: str | None, now: datetime | None = None) -> int | None:
    if not posted_at:
        return None
    now = now or datetime.now(timezone.utc)
    try:
        d = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return max(0, (now - d).days)
    except Exception:
        return None


def find_recruiter(description: str | None) -> dict:
    """Detect whether the JD names an internal recruiter / TA contact.

    Returns names_internal_recruiter (bool|None), recruiter_contact, recruiter_signal.
    None when there is no description to inspect.
    """
    if description is None:
        return {"names_internal_recruiter": None, "recruiter_contact": None, "recruiter_signal": None}
    text = description.strip()
    if not text:
        return {"names_internal_recruiter": False, "recruiter_contact": None, "recruiter_signal": None}
    m = _RECRUITER_LABEL.search(text)
    if m and not _BAD_CONTACT.match(m.group(1).strip()):
        return {"names_internal_recruiter": True, "recruiter_contact": m.group(1).strip(), "recruiter_signal": "labeled_name"}
    m = _CONTACT_NAME.search(text)
    if m and not _BAD_CONTACT.match(m.group(1).strip()):
        return {"names_internal_recruiter": True, "recruiter_contact": m.group(1).strip(), "recruiter_signal": "contact_name"}
    for em in _MAILTO.findall(text):
        if not _GENERIC_MAIL.match(em):
            return {"names_internal_recruiter": True, "recruiter_contact": em.lower(), "recruiter_signal": "mailto"}
    # Plain email next to recruiter/talent words (no mailto:)
    if re.search(r"\b(recruiter|talent acquisition|ta partner)\b.{0,60}[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", text, re.I):
        em = re.search(r"([a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,})", text, re.I)
        if em and not _GENERIC_MAIL.match(em.group(1)):
            return {"names_internal_recruiter": True, "recruiter_contact": em.group(1).lower(), "recruiter_signal": "email"}
    return {"names_internal_recruiter": False, "recruiter_contact": None, "recruiter_signal": None}


def apply_bd_fields(job: dict, company_key: str, *, long_open_days: int = 30, now: datetime | None = None,
                    company_domain: str | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    fp = job_fingerprint(company_key, job.get("title"), job.get("location"))
    age = days_open(job.get("posted_at"), now)
    job["fingerprint"] = fp
    job["days_open"] = age
    job["is_long_open"] = (age is not None and age >= long_open_days)
    job["company_domain"] = company_domain
    job.update(find_recruiter(job.get("description")))
    return job


def merge_duplicate_jobs(jobs: list[dict]) -> list[dict]:
    """Keep the first row per fingerprint; attach duplicate_urls / duplicate_count on the keeper."""
    kept: dict[str, dict] = {}
    order: list[str] = []
    for j in jobs:
        fp = j.get("fingerprint") or job_fingerprint(j.get("company_slug") or j.get("company_name"), j.get("title"), j.get("location"))
        j["fingerprint"] = fp
        if fp not in kept:
            kept[fp] = j
            j["duplicate_count"] = 0
            j["duplicate_urls"] = []
            order.append(fp)
        else:
            kept[fp]["duplicate_count"] += 1
            url = j.get("url")
            if url and url not in kept[fp]["duplicate_urls"] and url != kept[fp].get("url"):
                kept[fp]["duplicate_urls"].append(url)
    return [kept[fp] for fp in order]


def _job(**kw: Any) -> dict[str, Any]:
    base = dict(
        ats=None, company_slug=None, company_name=None, job_id=None, title=None, department=None,
        team=None, location=None, country=None, remote=False, workplace_type=None,
        employment_type=None, posted_at=None, updated_at=None, url=None, apply_url=None,
        salary_min=None, salary_max=None, salary_currency=None, salary_text=None, description=None,
    )
    base.update(kw)
    base["seniority"] = seniority(base.get("title"))
    base["work_model"] = work_model(base.get("workplace_type"), bool(base.get("remote")), base.get("location"), base.get("title"))
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


def resolve_and_fetch(c: httpx.Client, t: Target, desc: bool) -> tuple[str, str, str, list[dict], str | None] | None:
    """Return (ats, slug, company_name, jobs, company_domain) or None if not found.

    company_domain comes from a non-ATS careers URL the user pasted (e.g. anthropic.com/careers),
    never invented from the board slug.
    """
    domain = company_domain_from_url(t.careers_url)
    if t.ats is None and t.careers_url:
        try:
            r = c.get(t.careers_url, follow_redirects=True)
            found = detect_from_html(str(r.url)) or detect_from_html(r.text)
            if found:
                t.ats, t.slug = found
            # If the careers URL redirected through the company site, prefer that host.
            domain = company_domain_from_url(str(r.url)) or domain
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
        return (t.ats, t.slug, res[0], res[1], domain) if res is not None else None
    # Bare slug: the same slug can exist on several ATS (often an old, near-empty board).
    # Try all six and keep the board with the most open jobs.
    best = None
    for ats in ATS_ORDER:
        try:
            res = FETCHERS[ats](c, t.slug, desc)
        except (httpx.HTTPError, ValueError):
            continue
        # Some ATS (e.g. Workable) answer any slug with an empty board, so ignore empty ones here.
        if res is not None and res[1] and (best is None or len(res[1]) > len(best[3])):
            best = (ats, t.slug, res[0], res[1], domain)
    return best


# ---------------- filtering + summary ----------------

def _any_kw(text: str, kws: Iterable[str]) -> bool:
    t = text.lower()
    return any(k.lower() in t for k in kws)


def filter_jobs(jobs: list[dict], keywords=None, exclude=None, locations=None, departments=None,
                remote_only=False, posted_within_days=None, now: datetime | None = None,
                seniority_levels=None, work_models=None) -> list[dict]:
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
        if seniority_levels and j.get("seniority") not in {x.lower() for x in seniority_levels}:
            continue
        if work_models and j.get("work_model") not in {x.lower().replace("-", "") for x in work_models}:
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
              now: datetime | None = None, closed_ids: set | None = None) -> dict:
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
    long_open_n = sum(1 for j in all_jobs if j.get("is_long_open"))
    repost_n = sum(1 for j in matched if j.get("is_repost"))
    named_n = sum(1 for j in matched if j.get("names_internal_recruiter") is True)
    domain = next((j.get("company_domain") for j in (matched or all_jobs) if j.get("company_domain")), None)
    return {
        "type": "company_summary", "ats": ats, "company_slug": slug, "company_name": name,
        "company_domain": domain,
        "total_open_jobs": total, "matched_jobs": len(matched),
        "new_jobs_since_last_run": (len(new_ids) if new_ids is not None else None),
        "closed_jobs_since_last_run": (len(closed_ids) if closed_ids is not None else None),
        "closed_job_ids": (sorted(closed_ids)[:50] if closed_ids is not None else None),
        "net_change_since_last_run": (len(new_ids) - len(closed_ids) if new_ids is not None and closed_ids is not None else None),
        "long_open_jobs": long_open_n,
        "repost_jobs_in_output": (repost_n if new_ids is not None else None),
        "jobs_naming_recruiter": named_n if any(j.get("description") is not None for j in matched) else None,
        "posted_last_7_days": last7, "posted_last_30_days": last30, "remote_share": remote_share,
        "seniority_mix": dict(Counter(j.get("seniority") for j in all_jobs if j.get("seniority")).most_common()),
        "work_model_mix": dict(Counter(j.get("work_model") or "unspecified" for j in all_jobs).most_common()),
        "top_departments": [d for d, _ in depts.most_common(5)], "top_locations": [l for l, _ in locs.most_common(5)],
        "sample_matched_titles": [j["title"] for j in matched[:5]],
        "hiring_signal_score": int(vol + fresh + rel),
        "checked_at": now.isoformat().replace("+00:00", "Z"),
    }


def make_client(timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> httpx.Client:
    return httpx.Client(timeout=timeout, headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.8"},
                        follow_redirects=True, transport=transport)
