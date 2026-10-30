from contextlib import nullcontext
from datetime import datetime, timezone

from src.ats import _job
from src.signals import diff_jobs, finalize_jobs, job_function, next_record, score_company, state_key

NOW = datetime(2026, 10, 30, tzinfo=timezone.utc)
F = ["title", "location", "salary_max"]


def _j(i, title="Eng", loc="NYC", sal=None, **kw):
    return {"job_id": i, "title": title, "location": loc, "salary_max": sal, "fingerprint": f"fp-{title}-{loc}", **kw}


def test_diff_new_changed_closed_and_repost():
    d1 = diff_jobs(None, [_j("1"), _j("2", "PM")], F, matched_ids={"1", "2"}, now=NOW)
    assert d1["baseline"] and d1["new"] == {"1", "2"} and not d1["closed"]
    rec = next_record(None, d1["jobs"], 2, now=NOW)
    # 1 changes location, 2 closes, 3 is a repost of 2 (same title+location), 4 new; salary None->value is NOT a change
    d2 = diff_jobs(rec, [_j("1", loc="Remote", sal=150000), _j("3", "PM"), _j("4", "Designer")], F,
                   matched_ids={"1", "3", "4"}, now=NOW)
    assert d2["new"] == {"3", "4"} and d2["closed"] == {"2"} and d2["reposts"] == {"3"}
    assert d2["changed"] == {"1": {"location": ["NYC", "Remote"]}}


def test_incomplete_scan_merges_and_never_closes():
    rec = next_record(None, diff_jobs(None, [_j("1"), _j("2")], F, matched_ids={"1", "2"})["jobs"], 2)
    d = diff_jobs(rec, [_j("3")], F, matched_ids={"3"}, complete=False)
    assert not d["closed"] and set(d["jobs"]) == {"1", "2", "3"}


def test_finalize_rolls_back_undelivered():
    prev = next_record(None, diff_jobs(None, [_j("1")], F, matched_ids={"1"})["jobs"], 1)
    d = diff_jobs(prev, [_j("1", title="Staff Eng"), _j("2")], F, matched_ids={"1", "2"})
    out = finalize_jobs(d["jobs"], prev, ["1", "2"])
    assert "2" not in out and out["1"]["f"]["title"] == "Eng"


def test_state_key_is_kv_safe_and_stable():
    k = state_key("my key/1", "Greenhouse:Anthropic ")
    assert k == state_key("my key/1", "greenhouse:anthropic") and "/" not in k and " " not in k


def test_job_function():
    assert job_function("Enterprise Account Executive") == "sales"
    assert job_function("Senior Backend Engineer") == "engineering"
    assert job_function("Anything", "Customer Success") == "customer_success"
    assert job_function("Machine Learning Engineer") == "data_ai"


def test_score_is_bounded_and_explained():
    jobs = [{"title": "VP Sales", "seniority": "executive", "remote": True, "is_long_open": True},
            {"title": "Account Executive", "seniority": "mid", "remote": True, "is_long_open": False},
            {"title": "Senior Account Executive", "seniority": "senior", "remote": False, "is_long_open": False}]
    s = score_company(jobs, ages=[40, 2, 5], open_total=3, target_functions=["sales"], now=NOW)
    assert 0 <= s["signal_score"] <= 100 and len(s["score_reasons"]) == 7
    assert sum(s["score_components"].values()) == s["signal_score"]
    assert any("target functions (sales)" in r for r in s["score_reasons"])
    empty = score_company([], ages=[], open_total=0, now=NOW)
    assert empty["signal_score"] == 0 and empty["signal_tier"] == "cold"
    grown = score_company(jobs, ages=[1, 1, 1], open_total=30, history=[["2026-10-20T00:00:00Z", 20]], now=NOW)
    assert grown["score_components"]["velocity"] == 20 and any("20 -> 30" in r for r in grown["score_reasons"])
    # history younger than 7 days falls back to posting pace, so run frequency doesn't move the score
    recent = score_company(jobs, ages=[1, 1, 1], open_total=30, history=[["2026-10-29T00:00:00Z", 30]], now=NOW)
    assert not any("tracking" in r for r in recent["score_reasons"])


def test_process_company_change_tracking_and_scoring(monkeypatch):
    import src.main as m
    board = [
        _job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="1", title="Senior Engineer",
             location="NYC", posted_at="2026-09-01T00:00:00Z", url="https://x/1"),
        _job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="2", title="Account Executive",
             location="NYC", posted_at="2026-10-28T00:00:00Z", url="https://x/2"),
    ]
    monkeypatch.setattr(m, "resolve_and_fetch", lambda c, t, d: ("greenhouse", "acme", "Acme", [dict(x) for x in board], None))
    monkeypatch.setattr(m, "make_client", lambda: nullcontext())
    inp = {"changeTracking": True, "signalScoring": True, "scoreFunctions": ["sales"]}
    r1 = m._process_company("greenhouse:acme", inp, {}, None)
    assert [j["change_type"] for j in r1["jobs"]] == ["new", "new"] and r1["summary"]["is_baseline_run"] is True
    assert r1["summary"]["new_jobs_since_last_run"] is None and r1["closed_rows"] == []
    assert r1["summary"]["score_reasons"] and r1["jobs"][0]["score_reasons"] == r1["summary"]["score_reasons"]
    state = next_record(None, finalize_jobs(r1["chg_jobs"], None, r1["undelivered"]), r1["open_total"])
    # run 2: job 1 retitled (changed), job 2 closed, job 3 new
    board[0] = dict(board[0], title="Staff Engineer")
    board[1] = _job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="3", title="Designer",
                    location="NYC", posted_at="2026-10-29T00:00:00Z", url="https://x/3")
    r2 = m._process_company("greenhouse:acme", inp, {}, state)
    by = {j["job_id"]: j for j in r2["jobs"]}
    assert by["1"]["change_type"] == "changed" and by["1"]["previous_values"] == {"title": "Senior Engineer"}
    assert by["3"]["change_type"] == "new" and by["3"]["is_new"] is True
    assert [c["job_id"] for c in r2["closed_rows"]] == ["2"] and r2["closed_rows"][0]["change_type"] == "closed"
    s = r2["summary"]
    assert (s["new_jobs_since_last_run"], s["changed_jobs_since_last_run"], s["closed_jobs_since_last_run"]) == (1, 1, 1)
    # run 3 with nothing changed -> no rows
    state2 = next_record(state, finalize_jobs(r2["chg_jobs"], state, r2["undelivered"]), r2["open_total"])
    r3 = m._process_company("greenhouse:acme", inp, {}, state2)
    assert r3["jobs"] == [] and r3["closed_rows"] == []


def test_defaults_unchanged(monkeypatch):
    import src.main as m
    board = [_job(ats="greenhouse", company_slug="acme", company_name="Acme", job_id="1", title="Engineer",
                  location="NYC", posted_at="2026-10-01T00:00:00Z", url="https://x/1")]
    monkeypatch.setattr(m, "resolve_and_fetch", lambda c, t, d: ("greenhouse", "acme", "Acme", [dict(x) for x in board], None))
    monkeypatch.setattr(m, "make_client", lambda: nullcontext())
    r = m._process_company("greenhouse:acme", {}, {})
    assert "signal_score" not in r["summary"] and "change_type" not in r["jobs"][0] and "closed_rows" not in r
    assert "score_reasons" not in r["jobs"][0] and "hiring_signal_score" in r["summary"]
