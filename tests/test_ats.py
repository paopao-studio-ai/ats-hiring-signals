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
        ats, slug, name, jobs = resolve_and_fetch(c, parse_target("acme"), False)
    assert ats == "ashby" and len(jobs) == 2
    assert jobs[0]["salary_min"] == 150000 and jobs[0]["remote"] is True and jobs[0]["salary_text"] == "$150K - $200K"


def test_unknown_returns_none():
    with _mock(lambda req: httpx.Response(404)) as c:
        assert resolve_and_fetch(c, parse_target("zzz-none"), False) is None
