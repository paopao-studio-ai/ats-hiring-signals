"""Regression: with changeTracking + maxJobsPerCompany, a second identical run must output 0 job rows
and report 0 new / 0 changed / 0 closed. Runs the real Actor main() twice against local storage."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RUNNER = textwrap.dedent('''
    import asyncio, sys
    sys.path.insert(0, %r)
    from contextlib import nullcontext
    import src.main as m
    board = [{"ats": "greenhouse", "company_slug": "acme", "company_name": "Acme", "job_id": str(i),
              "title": f"Account Executive {i}", "location": "NYC", "posted_at": "2026-10-%%02dT00:00:00Z" %% (1 + i %% 28),
              "url": f"https://x/{i}", "department": "Sales"} for i in range(25)]
    m.resolve_and_fetch = lambda c, t, d: ("greenhouse", "acme", "Acme", [dict(x) for x in board], None)
    m.make_client = lambda: nullcontext()
    asyncio.run(m.main())
''') % str(ROOT)


PRICING = json.dumps({"pricingModel": "PAY_PER_EVENT", "pricingPerEvent": {"actorChargeEvents": {
    "apify-default-dataset-item": {"eventPriceUsd": 0, "eventTitle": "Dataset item"},
    "job": {"eventPriceUsd": 0.001, "eventTitle": "Job"},
    "company-summary": {"eventPriceUsd": 0.005, "eventTitle": "Company summary"}}}})


def _run(storage: Path, inp: dict, max_charge: float | None = None) -> list[dict]:
    kv = storage / "key_value_stores" / "default"
    kv.mkdir(parents=True, exist_ok=True)
    (kv / "INPUT.json").write_text(json.dumps(inp))
    ds = storage / "datasets" / "default"
    if ds.exists():
        for f in ds.glob("0*.json"):
            f.unlink()
    env = {**os.environ, "APIFY_LOCAL_STORAGE_DIR": str(storage), "CRAWLEE_STORAGE_DIR": str(storage),
           "CRAWLEE_PURGE_ON_START": "0"}
    if max_charge is not None:  # local pay-per-event simulation (nothing is billed)
        env.update({"ACTOR_TEST_PAY_PER_EVENT": "1", "APIFY_ACTOR_PRICING_INFO": PRICING,
                    "APIFY_CHARGED_ACTOR_EVENT_COUNTS": json.dumps({"job": 0}), "ACTOR_MAX_TOTAL_CHARGE_USD": str(max_charge)})
    subprocess.run([sys.executable, "-c", RUNNER], env=env, check=True, capture_output=True, timeout=120)
    return [json.loads(f.read_text()) for f in sorted(ds.glob("0*.json"))]


def test_capped_change_tracking_second_run_is_empty(tmp_path):
    inp = {"companies": ["greenhouse:acme"], "changeTracking": True, "maxJobsPerCompany": 10, "stateKey": "reg"}
    first = _run(tmp_path, inp)
    assert sum(r["type"] == "job" for r in first) == 10
    second = _run(tmp_path, inp)
    assert [r for r in second if r["type"] in ("job", "job_closed")] == []
    s = next(r for r in second if r["type"] == "company_summary")
    assert (s["new_jobs_since_last_run"], s["changed_jobs_since_last_run"], s["closed_jobs_since_last_run"]) == (0, 0, 0)


def test_charge_limit_rows_are_carried_once_and_never_double_charged(tmp_path):
    inp = {"companies": ["greenhouse:acme"], "changeTracking": True, "maxJobsPerCompany": 10, "stateKey": "reg2", "outputCompanySummary": False}
    first = _run(tmp_path, inp, max_charge=0.004)  # only 4 jobs fit
    got1 = [r["job_id"] for r in first if r["type"] == "job"]
    assert len(got1) == 4
    second = _run(tmp_path, inp, max_charge=1.0)
    got2 = [r["job_id"] for r in second if r["type"] == "job"]
    assert len(got2) == 10 - len(got1) and not set(got1) & set(got2)  # only the rows the limit cut
    third = _run(tmp_path, inp, max_charge=1.0)
    assert [r for r in third if r["type"] == "job"] == []
