"""Best-effort public-company identifiers (ticker, SEC CIK) for hiring-signal rows.

Source: SEC's public company_tickers.json (https://www.sec.gov/files/company_tickers.json),
fetched once per run with a descriptive User-Agent per SEC fair-access rules.
Matching is deliberately conservative: a normalized exact name match only, and any
name that maps to more than one CIK is left unmatched (null) rather than guessed.
"""
from __future__ import annotations

import re
from typing import Any

import httpx

SEC_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_UA = "PaoPao Studio hiring-signals actor paopaomusic@mail.grokbot.com"
_SUFFIX = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|plc|ltd|limited|llc|lp|l p|sa|s a|ag|nv|n v|se|ab|asa|oyj|spa|"
    r"bv|holdings?|group|the|class [a-c]|de|new)\b"
)
_TAIL = re.compile(r"\b(technologies|technology|systems|international|global|software|labs|financial|services)$")


def norm(name: str | None) -> str:
    s = (name or "").lower().replace("&", " and ")
    s = re.sub(r"/[a-z]{2}/?", " ", s)          # SEC state tags like /DE/
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = _SUFFIX.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _keys(name: str | None) -> set[str]:
    k = norm(name)
    out = {k} if k else set()
    short = _TAIL.sub("", k).strip()
    if short and short != k:
        out.add(short)
    return out


def build_index(rows: list[dict]) -> dict[str, dict | None]:
    """Map normalized name -> {ticker, cik, sec_title}; ambiguous keys map to None."""
    idx: dict[str, dict | None] = {}
    for r in rows:
        rec = {"ticker": r.get("ticker"), "cik": str(r.get("cik_str")).zfill(10), "sec_title": r.get("title")}
        for k in _keys(r.get("title")):
            if k in idx and idx[k] is not None and idx[k]["cik"] != rec["cik"]:
                idx[k] = None
            elif k in idx and idx[k] is None:
                continue
            elif k not in idx:
                idx[k] = rec   # first row per CIK in SEC file is the primary share class
    return idx


def load_index(client: httpx.Client | None = None) -> dict[str, dict | None]:
    c = client or httpx.Client(timeout=30.0)
    try:
        r = c.get(SEC_URL, headers={"User-Agent": SEC_UA, "Accept": "application/json"})
        r.raise_for_status()
        return build_index(list(r.json().values()))
    except Exception:  # noqa: BLE001 - identifiers are optional enrichment
        return {}
    finally:
        if client is None:
            c.close()


def lookup(idx: dict[str, dict | None], *names: Any) -> dict[str, Any]:
    for n in names:
        if not n:
            continue
        k = norm(str(n))
        if k in idx:
            hit = idx[k]
            if hit:
                return {**hit, "ticker_match": "exact_name"}
            return {"ticker": None, "cik": None, "sec_title": None, "ticker_match": "ambiguous"}
    return {"ticker": None, "cik": None, "sec_title": None, "ticker_match": None}
