import json

import pytest
from conftest import FakeESearch, relevance_order

from pmcfetch.errors import HTTPStatusError, SearchError
from pmcfetch.ratelimit import MinIntervalLimiter, NullLimiter
from pmcfetch.search import OA_FILTER, PMCSearch

# Spread across the id space so splitting has to recurse on both sides.
UIDS = [3, 17, 4_000, 250_000, 999_999, 1_000_001] + [13_000_000 + 37 * i for i in range(17)]


def make(fake, **kw):
    return PMCSearch(http_get=fake, limiter=NullLimiter(), sleep=lambda s: None, **kw)


def test_summary_counts_open_access_matches_without_fetching_ids():
    fake = FakeESearch(UIDS)
    summary = make(fake).summarize("burn scar")
    assert summary.count == len(UIDS)
    assert summary.query == "burn scar"
    assert summary.translation == f"(burn scar) AND {OA_FILTER}"
    assert len(fake.calls) == 1
    call = fake.calls[0]
    assert call["term"] == f"(burn scar) AND {OA_FILTER}"
    assert (call["db"], call["retmax"], call["tool"]) == ("pmc", "0", "pmcfetch")
    assert "api_key" not in call and "email" not in call


def test_api_key_and_email_are_sent_and_raise_the_default_rate():
    fake = FakeESearch(UIDS)
    search = PMCSearch(http_get=fake, api_key="k3y", email="me@example.org", sleep=lambda s: None)
    search.count("burn")
    assert (fake.calls[0]["api_key"], fake.calls[0]["email"]) == ("k3y", "me@example.org")
    assert isinstance(search.limiter, MinIntervalLimiter)
    assert search.limiter.min_interval == 0.1
    assert PMCSearch().limiter.min_interval == 0.34


def test_ncbi_warnings_are_surfaced():
    body = {
        "esearchresult": {
            "count": "0",
            "idlist": [],
            "errorlist": {"phrasesnotfound": ["xqzv"], "fieldsnotfound": ["autor"]},
            "warninglist": {
                "phrasesignored": ["of"],
                "quotedphrasesnotfound": ['"a b"'],
                "outputmessages": ["No items found.", "AND"],
            },
        }
    }
    summary = make(lambda url, timeout: json.dumps(body).encode()).summarize("xqzv")
    assert summary.count == 0
    assert summary.warnings == (
        "phrase not found: xqzv",
        "unknown field: autor",
        "phrase ignored: of",
        'quoted phrase not found: "a b"',
        "NCBI: AND",
    )


def test_small_search_is_one_request_in_ncbi_order():
    fake = FakeESearch(UIDS)
    assert make(fake).ids("burn", limit=3) == [f"PMC{u}" for u in relevance_order(UIDS)[:3]]
    assert len(fake.calls) == 1
    assert (fake.calls[0]["sort"], fake.calls[0]["retmax"]) == ("relevance", "3")

    fake = FakeESearch(UIDS)
    assert make(fake).ids("burn", limit=3, sort="newest") == [f"PMC{u}" for u in sorted(UIDS)[-1:-4:-1]]
    assert "sort" not in fake.calls[0]


def test_limit_above_the_count_returns_every_match_once():
    fake = FakeESearch(UIDS)
    out = make(fake).ids("burn", limit=1_000)
    assert sorted(out) == sorted(f"PMC{u}" for u in UIDS)
    assert len(fake.calls) == 1


def test_large_search_splits_by_id_range_and_returns_every_match():
    fake = FakeESearch(UIDS, cap=5)
    seen_progress = []
    out = make(fake, max_ids_per_query=5).ids("burn", progress=lambda c, w: seen_progress.append((c, w)))

    assert len(out) == len(set(out)) == len(UIDS)
    assert set(out) == {f"PMC{u}" for u in UIDS}
    # NCBI's ranking leads; the rest follow newest first.
    assert out[:5] == [f"PMC{u}" for u in relevance_order(UIDS)[:5]]
    rest = [int(p[3:]) for p in out[5:]]
    assert rest == sorted(rest, reverse=True)
    # Never asks for more than one query can return, and never pages with retstart.
    assert all(int(c["retmax"]) <= 5 and "retstart" not in c for c in fake.calls)
    assert seen_progress[-1] == (len(UIDS), len(UIDS))


def test_newest_first_holds_across_ranges():
    fake = FakeESearch(UIDS, cap=4)
    out = make(fake, max_ids_per_query=4).ids("burn", sort="newest")
    assert out == [f"PMC{u}" for u in sorted(UIDS, reverse=True)]


def test_large_search_stops_at_the_limit():
    fake = FakeESearch(UIDS, cap=5)
    out = make(fake, max_ids_per_query=5).ids("burn", limit=12)
    assert len(out) == len(set(out)) == 12
    assert out[:5] == [f"PMC{u}" for u in relevance_order(UIDS)[:5]]


def test_zero_limit_makes_no_request():
    fake = FakeESearch(UIDS)
    assert make(fake).ids("burn", limit=0) == []
    assert fake.calls == []


def test_records_carry_the_namespace():
    recs = make(FakeESearch(UIDS)).records("burn", limit=2, namespace="burns")
    assert [r.namespace for r in recs] == ["burns", "burns"]
    assert all(r.pmcid.startswith("PMC") for r in recs)


def test_ncbi_rejection_is_a_search_error_and_not_retried():
    calls = []

    def http(url, timeout):
        calls.append(url)
        return json.dumps({"esearchresult": {"ERROR": "Search Backend failed"}}).encode()

    with pytest.raises(SearchError, match="Search Backend failed"):
        make(http).count("burn")
    assert len(calls) == 1


def test_garbled_bodies_and_429s_are_retried_with_retry_after():
    sleeps = []
    fake = FakeESearch(UIDS, fail_first=[b"<html>busy</html>", HTTPStatusError(429, retry_after=2.0)])
    search = PMCSearch(http_get=fake, limiter=NullLimiter(), sleep=sleeps.append, retries=3)
    assert search.count("burn") == len(UIDS)
    assert len(fake.calls) == 3
    assert sleeps[-1] == 2.0


def test_exhausted_retries_raise_a_search_error():
    fake = FakeESearch(UIDS, fail_first=[HTTPStatusError(503)] * 3)
    with pytest.raises(SearchError, match="HTTP 503"):
        make(fake, retries=2).count("burn")


def test_bad_api_key_is_explained():
    fake = FakeESearch(UIDS, fail_first=[HTTPStatusError(400)])
    with pytest.raises(SearchError, match="API key"):
        make(fake).count("burn")


def test_empty_query_and_unknown_sort_are_rejected():
    with pytest.raises(SearchError, match="empty"):
        make(FakeESearch(UIDS)).count("   ")
    with pytest.raises(ValueError, match="sort"):
        make(FakeESearch(UIDS)).ids("burn", sort="oldest")
