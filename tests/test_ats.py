from datetime import datetime, timezone

import httpx

from src.ats import _iso, detect_from_html, filter_jobs, parse_target, resolve_and_fetch, strip_html, summarize, make_client

NOW = datetime(2026, 10, 29, tzinfo=timezone.utc)


def test_parse_targets():
    assert (parse_target("greenhouse:stripe").ats, parse_target("greenhouse:stripe").slug) == ("greenhouse", "stripe")
    t = parse_target("https://job-boards.greenhouse.io/anthropic/jobs/123")
    assert (t.ats, t.slug) == ("greenhouse", "anthropic")
    assert parse_target("https://jobs.lever.co/palantir").slug == "palantir"
    assert parse_target("https://jobs.ashbyhq.com/ramp").ats == "ashby"
    assert parse_target("https://apply.workable.com/huggingface/").ats == "workable"
    assert parse_target("https://jobs.smartrecruiters.com/Ubisoft2").slug == "Ubisoft2"
    assert parse_target("https://bunq.recruitee.com/").slug == "bunq"
    bare = parse_target("ramp")
    assert bare.ats is None and bare.slug == "ramp"
    page = parse_target("https://example.com/careers")
    assert page.ats is None and page.careers_url == "https://example.com/careers"


def test_detect_from_html():
    html = '<script src="https://boards.greenhouse.io/embed/job_board/js?for=acme"></script>'
    assert detect_from_html(html) == ("greenhouse", "acme")
    assert detect_from_html('<a href="https://jobs.lever.co/foo/abc">') == ("lever", "foo")


def test_iso_and_html():
    assert _iso("2026-10-27 12:11:34 UTC") == "2026-10-27T12:11:34Z"
    assert _iso(1700000000000).startswith("2023-11-14")
    assert strip_html("&lt;p&gt;Hi &amp;amp; bye&lt;/p&gt;") == "Hi & bye"


JOBS = [
    {"job_id": "1", "title": "Senior Sales Engineer", "department": "Sales", "location": "London", "remote": False, "posted_at": "2026-10-25T00:00:00Z"},
    {"job_id": "2", "title": "Backend Engineer", "department": "Engineering", "location": "Remote - US", "remote": True, "posted_at": "2026-08-01T00:00:00Z"},
    {"job_id": "3", "title": "Account Executive", "department": "Sales", "location": "New York", "remote": False, "posted_at": None},
]


def test_filters():
    assert [j["job_id"] for j in filter_jobs(JOBS, keywords=["engineer"], now=NOW)] == ["1", "2"]
    assert [j["job_id"] for j in filter_jobs(JOBS, exclude=["senior"], now=NOW)] == ["2", "3"]
    assert [j["job_id"] for j in filter_jobs(JOBS, remote_only=True, now=NOW)] == ["2"]
    assert [j["job_id"] for j in filter_jobs(JOBS, posted_within_days=30, now=NOW)] == ["1"]
    assert [j["job_id"] for j in filter_jobs(JOBS, departments=["sales"], locations=["york"], now=NOW)] == ["3"]


def test_summary():
    s = summarize("lever", "acme", "Acme", JOBS, JOBS[:1], {"1"}, now=NOW)
    assert s["total_open_jobs"] == 3 and s["posted_last_7_days"] == 1 and s["new_jobs_since_last_run"] == 1
    assert s["top_departments"][0] == "Sales" and 0 <= s["hiring_signal_score"] <= 100


def _mock(handler):
    return make_client(transport=httpx.MockTransport(handler))


def test_bare_slug_picks_biggest_board():
    def handler(req: httpx.Request):
        h, p = req.url.host, req.url.path
        if h == "boards-api.greenhouse.io":
            if p.endswith("/departments"):
                return httpx.Response(200, json={"departments": []})
            if p == "/v1/boards/acme":
                return httpx.Response(200, json={"name": "ACME"})
            return httpx.Response(200, json={"jobs": [{"id": 9, "title": "Old job", "location": {"name": "X"}}]})
        if h == "api.ashbyhq.com":
            return httpx.Response(200, json={"jobs": [
                {"id": "a", "title": "Eng", "isListed": True, "location": "Remote", "isRemote": True, "publishedAt": "2026-10-20T00:00:00Z",
                 "compensation": {"compensationTierSummary": "$150K - $200K", "summaryComponents": [{"compensationType": "Salary", "minValue": 150000, "maxValue": 200000, "currencyCode": "USD"}]}},
                {"id": "b", "title": "PM", "isListed": True, "location": "NYC"}]})
        if h == "api.smartrecruiters.com":
            return httpx.Response(200, json={"content": [], "totalFound": 0})
        return httpx.Response(404)
    with _mock(handler) as c:
        ats, slug, name, jobs, domain = resolve_and_fetch(c, parse_target("acme"), False)
    assert ats == "ashby" and len(jobs) == 2
    assert jobs[0]["salary_min"] == 150000 and jobs[0]["remote"] is True and jobs[0]["salary_text"] == "$150K - $200K"


def test_unknown_returns_none():
    with _mock(lambda req: httpx.Response(404)) as c:
        assert resolve_and_fetch(c, parse_target("zzz-none"), False) is None


def test_seniority_and_work_model():
    from src.ats import seniority, work_model
    cases = {"Software Engineer Intern": "intern", "Junior Designer": "entry", "Account Executive": "mid",
             "Product Manager": "mid", "Senior Sales Engineer": "senior", "Sr. Backend Engineer": "senior",
             "Staff Engineer": "lead", "Engineering Manager": "manager", "Head of Growth": "director",
             "Associate Director, Finance": "director", "VP, Marketing": "executive", "CTO": "executive"}
    for title, level in cases.items():
        assert seniority(title) == level, title
    assert seniority(None) is None
    assert work_model("Remote", False) == "remote"
    assert work_model("OnSite", False) == "onsite"
    assert work_model(None, False, "London (Hybrid)") == "hybrid"
    assert work_model(None, True, "Remote - US") == "remote"
    assert work_model(None, False, "NYC") is None


def test_filters_by_seniority_and_work_model():
    from src.ats import _job
    jobs = [_job(job_id="1", title="Senior Engineer", location="Remote"), _job(job_id="2", title="Engineering Intern", location="Berlin (Hybrid)")]
    assert jobs[0]["work_model"] == "remote" and jobs[1]["seniority"] == "intern"
    assert [j["job_id"] for j in filter_jobs(jobs, seniority_levels=["senior"], now=NOW)] == ["1"]
    assert [j["job_id"] for j in filter_jobs(jobs, work_models=["hybrid"], now=NOW)] == ["2"]


def test_monitor_reports_closed_jobs(monkeypatch):
    import src.main as m
    board = {"jobs": [{"id": "a", "title": "Eng", "isListed": True}, {"id": "b", "title": "PM", "isListed": True}]}
    monkeypatch.setattr(m, "make_client", lambda: _mock(lambda req: httpx.Response(200, json=board)))
    seen: dict = {}
    inp = {"onlyNewSinceLastRun": True}
    r1 = m._process_company("ashby:acme", inp, seen)
    assert len(r1["jobs"]) == 2 and r1["summary"]["closed_jobs_since_last_run"] is None
    board["jobs"] = [{"id": "b", "title": "PM", "isListed": True}, {"id": "c", "title": "Senior Designer", "isListed": True}]
    r2 = m._process_company("ashby:acme", inp, seen)
    s = r2["summary"]
    assert [j["job_id"] for j in r2["jobs"]] == ["c"]
    assert s["closed_jobs_since_last_run"] == 1 and s["closed_job_ids"] == ["a"] and s["net_change_since_last_run"] == 0
    assert s["seniority_mix"] == {"mid": 1, "senior": 1}


def test_fingerprint_dedupe_and_long_open():
    from src.ats import apply_bd_fields, find_recruiter, job_fingerprint, merge_duplicate_jobs, company_domain_from_url
    assert job_fingerprint("greenhouse:acme", "Senior Engineer", "NYC") == job_fingerprint("greenhouse:acme", "senior  engineer", "nyc")
    assert job_fingerprint("a", "X", "NY") != job_fingerprint("b", "X", "NY")
    j1 = apply_bd_fields({"title": "Engineer", "location": "NYC", "posted_at": "2026-09-01T00:00:00Z", "url": "u1", "description": None}, "greenhouse:acme", long_open_days=30, now=NOW)
    j2 = apply_bd_fields({"title": "Engineer", "location": "NYC", "posted_at": "2026-10-20T00:00:00Z", "url": "u2", "description": None}, "greenhouse:acme", long_open_days=30, now=NOW)
    assert j1["is_long_open"] is True and j2["is_long_open"] is False
    merged = merge_duplicate_jobs([j1, j2])
    assert len(merged) == 1 and merged[0]["duplicate_count"] == 1 and merged[0]["duplicate_urls"] == ["u2"]
    assert company_domain_from_url("https://www.anthropic.com/careers") == "anthropic.com"
    assert company_domain_from_url("https://boards.greenhouse.io/anthropic") is None
    assert find_recruiter(None)["names_internal_recruiter"] is None
    assert find_recruiter("We are growing.")["names_internal_recruiter"] is False
    # Must NOT fire on interview boilerplate without a name
    assert find_recruiter("The hiring manager will discuss this.")["names_internal_recruiter"] is False
    hit = find_recruiter("Recruiter: Jane Doe\nApply today.")
    assert hit == {"names_internal_recruiter": True, "recruiter_contact": "Jane Doe", "recruiter_signal": "labeled_name"}
    hit = find_recruiter("Questions? Reach out to Sam Lee at talent@acme.com for help.")
    assert hit["names_internal_recruiter"] is True and hit["recruiter_contact"] == "Sam Lee"
    hit = find_recruiter('Email <a href="mailto:jane.doe@acme.com">Jane</a>')
    assert hit["recruiter_signal"] == "mailto" and hit["recruiter_contact"] == "jane.doe@acme.com"
    assert find_recruiter('mailto:jobs@acme.com')["names_internal_recruiter"] is False
    assert find_recruiter("Please email us at talent@acme.com")["names_internal_recruiter"] is False
    # Same company name across boards must share a fingerprint for cross-site dedupe.
    a = apply_bd_fields({"title": "Engineer", "location": "NYC", "posted_at": "2026-10-01T00:00:00Z", "url": "gh", "description": None}, "Acme", now=NOW)
    b = apply_bd_fields({"title": "Engineer", "location": "NYC", "posted_at": "2026-10-01T00:00:00Z", "url": "lv", "description": None}, "Acme", now=NOW)
    assert a["fingerprint"] == b["fingerprint"] and len(merge_duplicate_jobs([a, b])) == 1


def test_monitor_marks_repost(monkeypatch):
    import src.main as m
    from src.ats import _job

    def fake_resolve(c, t, desc):
        jobs = [
            _job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="1", title="Senior Engineer",
                 location="NYC", posted_at="2026-09-01T00:00:00Z", url="https://x/1"),
            _job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="2", title="PM",
                 location="NYC", posted_at="2026-10-01T00:00:00Z", url="https://x/2"),
        ]
        return ("greenhouse", "acme", "Acme", jobs, "acme.com")

    monkeypatch.setattr(m, "resolve_and_fetch", fake_resolve)
    monkeypatch.setattr(m, "make_client", lambda: __import__("contextlib").nullcontext())
    seen = {}
    r1 = m._process_company("greenhouse:acme", {"onlyNewSinceLastRun": True}, seen)
    assert len(r1["jobs"]) == 2 and r1["summary"]["company_domain"] == "acme.com"
    assert all(j.get("is_repost") is False for j in r1["jobs"])  # first run: all new, none are reposts of a prior close

    def fake_resolve2(c, t, desc):
        # job 1 gone; same title+location returns as new id 3 (=repost); job 2 stays; brand-new 4
        jobs = [
            _job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="2", title="PM",
                 location="NYC", posted_at="2026-10-01T00:00:00Z", url="https://x/2"),
            _job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="3", title="Senior Engineer",
                 location="NYC", posted_at="2026-10-28T00:00:00Z", url="https://x/3"),
            _job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="4", title="Designer",
                 location="NYC", posted_at="2026-10-28T00:00:00Z", url="https://x/4"),
        ]
        return ("greenhouse", "acme", "Acme", jobs, "acme.com")

    monkeypatch.setattr(m, "resolve_and_fetch", fake_resolve2)
    r2 = m._process_company("greenhouse:acme", {"onlyNewSinceLastRun": True}, seen)
    by_id = {j["job_id"]: j for j in r2["jobs"]}
    assert set(by_id) == {"3", "4"}
    assert by_id["3"]["is_repost"] is True and by_id["4"]["is_repost"] is False
    assert r2["summary"]["repost_jobs_in_output"] == 1
