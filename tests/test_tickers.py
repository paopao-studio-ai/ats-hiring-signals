from src.tickers import build_index, lookup

ROWS = [
    {"cik_str": 1045810, "ticker": "NVDA", "title": "NVIDIA CORP"},
    {"cik_str": 1321655, "ticker": "PLTR", "title": "Palantir Technologies Inc."},
    {"cik_str": 1141391, "ticker": "MA", "title": "Mastercard Inc"},
    {"cik_str": 1, "ticker": "AAA", "title": "Target Corp"},
    {"cik_str": 2, "ticker": "BBB", "title": "Target Group Inc"},
    {"cik_str": 1045810, "ticker": "NVDA.X", "title": "NVIDIA CORP"},
]


def test_exact_and_suffix_match():
    idx = build_index(ROWS)
    assert lookup(idx, "nvidia")["ticker"] == "NVDA"
    assert lookup(idx, "Mastercard")["cik"] == "0001141391"
    assert lookup(idx, "palantir")["ticker"] == "PLTR"  # 'Technologies' tail dropped


def test_ambiguous_and_unknown_stay_null():
    idx = build_index(ROWS)
    assert lookup(idx, "target") == {"ticker": None, "cik": None, "sec_title": None, "ticker_match": "ambiguous"}
    assert lookup(idx, "anthropic")["ticker"] is None
    assert lookup(idx, None, "", "nvidia")["ticker"] == "NVDA"
