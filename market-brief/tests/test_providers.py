"""Synthetic fixtures only. Every HTTP request uses an in-memory transport."""

import asyncio
import copy
import json

import httpx
import pytest

from app.briefing import build_briefing, route_source
from app.config import Settings, WATCHLIST
from app.judge import CONTEXT_BYTE_BUDGET, QUESTIONS, judge_source, make_state
from app.prices import check_prices, validate_snapshot
from app.provider import call_api
from app.research import build_queries, check_date, expand_source, normalize_url, search_sources


SETTINGS = Settings(fin_data="synthetic-fin-key", exa_key="synthetic-exa-key", jev_key="synthetic-jev-key")
RETRIEVED = "2026-11-06T18:00:00+00:00"


def snapshot(**changes):
    return {"snapshot": {"ticker": "NVDA", "price": 100, "day_change": -3,
                         "day_change_percent": -2.91, "time": "2026-11-06T16:00:00Z", **changes}}


def run_data():
    return {"company": WATCHLIST[0], "window_start": "2026-11-03T18:00:00+00:00",
            "cutoff": RETRIEVED, "quote": {"fields": validate_snapshot(snapshot(), "NVDA", RETRIEVED)}}


def answers(scope="company", substance="concrete_development"):
    return {name: {"type": "choice", "choice": choice, "confidence": 1.0,
                   "probabilities": {key: float(key == choice) for key in QUESTIONS[name]["criteria"]}}
            for name, choice in (("scope", scope), ("substance", substance))}


def source_data(scope="company", substance="concrete_development", **changes):
    return {"id": "S1", "title": "Synthetic development", "url": "https://example.test/news",
            "published_date": "2026-11-06T17:00:00Z", "passages": ["Synthetic original passage."],
            "date_check": {"outside_window": False, "uncertain": False, "timing": "after_quote"},
            "judgment": {"valid": True, "answers": answers(scope, substance)}, **changes}


def invoke(handler, function, *args):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await function(SETTINGS, client, *args)
    return asyncio.run(execute())


def test_snapshot_sign_units_currency_and_prior_date():
    fields = validate_snapshot(snapshot(day_change_percent=3, day_change=2), "NVDA", RETRIEVED)
    assert fields["day_change_percent"] == 3
    assert fields["currency_source"] == "configuration"
    assert not fields["prior_date"]
    fields = validate_snapshot(snapshot(time="2026-11-05T16:00:00Z"), "NVDA", RETRIEVED)
    assert fields["prior_date"] and fields["day_change"] == -3
    assert fields["quote_time"] != RETRIEVED


@pytest.mark.parametrize("changes", [
    {"ticker": "AMD"}, {"price": 0}, {"price": -1}, {"price": True}, {"price": float("nan")},
    {"day_change": float("inf")}, {"day_change_percent": "3"}, {"time": None},
    {"time": "bad-date"}, {"time": "2026-11-06"}, {"time": 123}, {"time": "2027-01-01T00:00:00Z"},
    {"time_milliseconds": 0}, {"time_milliseconds": True},
])
def test_invalid_snapshots(changes):
    with pytest.raises(ValueError):
        validate_snapshot(snapshot(**changes), "NVDA", RETRIEVED)


def test_snapshot_timestamp_pair_and_milliseconds_only():
    from datetime import datetime
    millis = datetime.fromisoformat("2026-11-06T16:00:00+00:00").timestamp() * 1000
    assert validate_snapshot(snapshot(time_milliseconds=millis), "NVDA", RETRIEVED)["quote_time"].startswith("2026-11-06T16")
    assert validate_snapshot(snapshot(time=None, time_milliseconds=millis), "NVDA", RETRIEVED)["quote_time"].startswith("2026-11-06T16")


def test_check_prices_isolates_payment_error_and_fetches_each_once():
    seen = []
    def handler(request):
        ticker = request.url.params["ticker"]
        seen.append(ticker)
        assert request.headers["X-API-KEY"] == SETTINGS.fin_data
        if ticker == "AMD":
            return httpx.Response(402, json={"error": "Subscription required"})
        return httpx.Response(200, json=snapshot(ticker=ticker, time="2020-01-01T16:00:00Z"))
    quotes = invoke(handler, check_prices)
    assert seen == [item["ticker"] for item in WATCHLIST]
    assert [q["valid"] for q in quotes] == [True, False, True]
    assert quotes[1]["fields"]["price"] is None
    assert len({q["id"] for q in quotes}) == 3


@pytest.mark.parametrize("status,attempts", [(401, 1), (402, 1), (400, 1), (422, 1), (429, 3), (503, 3), (529, 3)])
def test_retry_policy_and_no_stored_credentials(status, attempts, monkeypatch):
    delays = []
    async def sleep(delay):
        delays.append(delay)
    monkeypatch.setattr("app.provider.asyncio.sleep", sleep)
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(status, json={"error": SETTINGS.jev_key}, headers={"Retry-After": "2"}))) as client:
            return await call_api(client, "POST", "https://api.typesafe.ai/v1/systemone", SETTINGS.jev_key, "Jev", body={"state": "test"})
    call = asyncio.run(execute())
    assert len(call["attempts"]) == attempts
    assert SETTINGS.jev_key not in json.dumps(call)
    assert "headers" not in call["request"]
    assert delays == [2] * (attempts - 1)
    assert call["cost"]["label"] == "Not reported"


def test_missing_credentials_do_not_call():
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: pytest.fail("Unexpected network call"))) as client:
            return await call_api(client, "POST", "https://api.exa.ai/search", "", "Exa")
    assert asyncio.run(execute())["attempts"] == []


def test_temporary_failure_recovers_and_records_attempts(monkeypatch):
    attempts = []
    async def sleep(delay):
        assert delay == 1
    monkeypatch.setattr("app.provider.asyncio.sleep", sleep)
    def handler(request):
        attempts.append(request)
        assert request.extensions["timeout"]["read"] == 30
        if len(attempts) == 1:
            raise httpx.ReadTimeout("Synthetic timeout", request=request)
        return httpx.Response(200, json={"results": [], "costDollars": {"total": 0.007}})
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await call_api(client, "POST", "https://api.exa.ai/search", SETTINGS.exa_key, "Exa")
    call = asyncio.run(execute())
    assert call["error"] is None and len(call["attempts"]) == 2
    assert call["attempts"][0]["error"] and call["attempts"][1]["error"] is None
    assert call["cost"] == {"label": "Reported", "value": {"total": 0.007}, "currency": "USD"}


@pytest.mark.parametrize("response", [httpx.Response(200, text="<html>Unexpected</html>"), httpx.Response(200, json=[])])
def test_invalid_provider_response_is_error(response):
    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: response)) as client:
            return await call_api(client, "POST", "https://api.exa.ai/search", SETTINGS.exa_key, "Exa")
    assert asyncio.run(execute())["error"]


def test_query_contract():
    run = run_data()
    queries = build_queries(run["company"], run["window_start"], run["cutoff"])
    assert [q["id"] for q in queries] == ["Q1", "Q2", "Q3"]
    for query in queries:
        body = query["request"]
        assert body["type"] == "auto" and body["numResults"] == 4
        assert body["contents"] == {"highlights": True}
        assert body["startPublishedDate"] == run["window_start"]


def test_url_normalization_preserves_semantic_parameters():
    assert normalize_url("https://EXAMPLE.test/a?id=1&utm_source=x&gclid=y#fragment") == "https://example.test/a?id=1"
    assert normalize_url("https://example.test/a?id=2") != normalize_url("https://example.test/a?id=1")
    assert normalize_url("javascript:alert(1)") is None
    assert normalize_url("https://user:secret@example.test/a") is None


@pytest.mark.parametrize("published,outside,uncertain,timing", [
    (None, False, True, "uncertain"), ("bad-date", False, True, "uncertain"),
    ("2026-11-06T00:00:00Z", False, True, "uncertain"),
    ("2026-11-04", False, True, "before_quote"),
    ("2026-11-03", False, True, "before_quote"),
    ("2026-11-02", True, True, "before_quote"),
    ("2026-11-06T17:00:00Z", False, False, "after_quote"),
    ("2026-11-06T15:00:00Z", False, False, "before_quote"),
    ("2026-11-06T19:00:00Z", True, False, "after_quote"),
    ("2026-11-06T17:00:00", False, True, "uncertain"),
])
def test_date_checks(published, outside, uncertain, timing):
    checked = check_date(published, run_data())
    assert (checked["outside_window"], checked["uncertain"], checked["timing"]) == (outside, uncertain, timing)


def test_search_merges_duplicates_and_preserves_provenance():
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        index = len(calls)
        return httpx.Response(200, json={"results": [{"title": "Synthetic", "url": f"https://example.test/news?utm_source=q{index}",
            "publishedDate": "2026-11-06T17:00:00Z", "highlights": [f"Original passage {index}."]}]})
    result = invoke(handler, search_sources, run_data())
    assert result["status"] == "complete"
    assert len(result["sources"]) == 1
    source = result["sources"][0]
    assert source["query_ids"] == ["Q1", "Q2", "Q3"]
    assert len(source["original_urls"]) == len(source["raw_refs"]) == 3
    assert source["passages"] == ["Original passage 1.", "Original passage 2.", "Original passage 3."]


def test_search_runs_concurrently_caps_sources_and_keeps_stable_ids():
    active = 0
    async def handler(request):
        nonlocal active
        active += 1
        position = active
        await asyncio.sleep(0)
        assert active == 3
        return httpx.Response(200, json={"results": [
            {"url": f"https://example.test/{position}/{index}", "highlights": ["Synthetic"]}
            for index in range(5)]})
    result = invoke(handler, search_sources, run_data())
    assert len(result["sources"]) == 12
    assert [source["id"] for source in result["sources"]] == [f"S{i}" for i in range(1, 13)]
    assert result["sources"][0]["url"] == "https://example.test/1/0"


@pytest.mark.parametrize("failures,expected", [(0, "no_sources"), (1, "partial"), (3, "failed")])
def test_search_empty_and_failure_states(failures, expected):
    seen = []
    def handler(request):
        seen.append(request)
        return httpx.Response(401, json={"error": "denied"}) if len(seen) <= failures else httpx.Response(200, json={"results": []})
    result = invoke(handler, search_sources, run_data())
    assert result["status"] == expected
    assert result["sources"] == []
    assert len(result["warnings"]) == failures


@pytest.mark.parametrize("status,passages,success", [("error", ["New"], False), ("success", [], False), ("success", ["  Original text\n"], True)])
def test_expansion_status_and_evidence_preservation(status, passages, success):
    source = source_data()
    original = copy.deepcopy(source)
    def handler(request):
        body = json.loads(request.content)
        assert body["ids"] == [source["url"]]
        assert "contents" not in body
        return httpx.Response(200, json={"statuses": [{"id": source["url"], "status": status}],
                                      "results": [{"url": source["url"], "highlights": passages}]})
    result = invoke(handler, expand_source, run_data(), source)
    assert result["success"] is success
    assert result["passages"] == (passages if success else [])
    assert source == original


def test_judge_exact_state_answers_and_model():
    source = source_data(passages=["  Exact text\nunchanged. "])
    def handler(request):
        body = json.loads(request.content)
        assert body["state"] == make_state(run_data(), source)
        assert body["questions"] == QUESTIONS
        assert request.headers["Authorization"] == f"Bearer {SETTINGS.jev_key}"
        return httpx.Response(200, json={"answers": answers(), "model": "jev-1.14.0", "usage": {"input_tokens": 500, "output_tokens": 40}})
    result = invoke(handler, judge_source, run_data(), source)
    assert result["valid"] and result["requested_model"] == "jev-latest"
    assert result["returned_model"] == "jev-1.14.0"
    assert result["call"]["usage"]["input_tokens"] == 500


@pytest.mark.parametrize("mutation", ["missing_question", "extra_question", "unknown_choice", "missing_probability", "negative", "sum", "confidence", "wrong_type", "not_max", "malformed"])
def test_invalid_judgments(mutation):
    data = answers()
    if mutation == "missing_question":
        del data["scope"]
    elif mutation == "extra_question":
        data["extra"] = data["scope"]
    elif mutation == "unknown_choice":
        data["scope"]["choice"] = "invented"
    elif mutation == "missing_probability":
        del data["scope"]["probabilities"]["unclear"]
    elif mutation == "negative":
        data["scope"]["probabilities"]["unclear"] = -0.1
    elif mutation == "sum":
        data["scope"]["probabilities"]["unclear"] = 0.2
    elif mutation == "confidence":
        data["scope"]["confidence"] = 1.1
    elif mutation == "wrong_type":
        data["scope"]["type"] = "noul"
    elif mutation == "not_max":
        data["scope"]["choice"] = "unclear"
    else:
        data["scope"] = "bad"
    result = invoke(lambda request: httpx.Response(200, json={"answers": data, "model": "jev-1.14.0"}), judge_source, run_data(), source_data())
    assert not result["valid"] and result["error"]
    assert route_source(source_data(judgment=result))["final"] == "review"


def test_no_passages_or_no_fitting_passage_skips_judge():
    for passages in ([], [" "], ["x" * CONTEXT_BYTE_BUDGET]):
        result = invoke(lambda request: pytest.fail("Must not call Jev"), judge_source, run_data(), source_data(passages=passages))
        assert not result["valid"] and result["call"] is None


def test_context_budget_omits_whole_passage_and_preserves_other_text():
    source = source_data(passages=["x" * CONTEXT_BYTE_BUDGET, " Original short passage. "])
    def handler(request):
        body = json.loads(request.content)
        assert body["state"]["source"]["passages"] == [" Original short passage. "]
        assert len(request.content) < CONTEXT_BYTE_BUDGET
        return httpx.Response(200, json={"answers": answers(), "model": "jev-1.14.0"})
    result = invoke(handler, judge_source, run_data(), source)
    assert result["valid"] and result["omitted_passages"][0]["index"] == 0
    assert len(source["passages"]) == 2


@pytest.mark.parametrize("scope,substance,route", [
    ("company", "concrete_development", "company"),
    ("business_connection", "concrete_development", "context"),
    ("sector_macro", "concrete_development", "context"),
    ("unrelated", "concrete_development", "out"),
    ("company", "price_recap_only", "out"),
    ("company", "commentary", "review"),
    ("unclear", "price_recap_only", "review"),
    ("unrelated", "insufficient", "review"),
])
def test_routing_order(scope, substance, route):
    assert route_source(source_data(scope, substance))["final"] == route


def test_overrides_preserve_uncertainty_commentary_and_outside_exclusion():
    review = {"decision": "keep", "note": "Synthetic review", "reviewed_at": RETRIEVED}
    source = source_data(substance="commentary", review=review,
                         date_check={"outside_window": False, "uncertain": True, "timing": "after_quote"})
    route = route_source(source)
    assert route["automated"] == "review" and route["final"] == "commentary"
    assert route["uncertain"] and route["commentary"] and route["timing"] == "after_quote"
    source["date_check"]["outside_window"] = True
    assert route_source(source)["final"] == "out"
    source = source_data(review={**review, "decision": "out"})
    assert route_source(source)["automated"] == "company" and route_source(source)["final"] == "out"


def test_briefing_sorts_and_includes_all_qualifying_sources():
    run = run_data()
    run["sources"] = [source_data(id=f"S{i}", published_date=f"2026-11-0{i}T17:00:00Z") for i in range(1, 5)]
    run["sources"] += [source_data("sector_macro", id=f"S{i}", published_date=None if i == 5 else "2026-11-06T17:00:00Z") for i in range(5, 8)]
    run["sources"].append(source_data("unclear", id="S8"))
    result = build_briefing(run)
    assert result["sections"]["company"] == ["S4", "S3", "S2", "S1"]
    assert result["sections"]["context"] == ["S6", "S7", "S5"]
    assert result["unresolved"] == ["S8"]
    assert len(run["sources"]) == 8
    assert "do not establish what caused it" in result["disclaimer"]
    run["sources"].append(source_data(substance="commentary", id="S9", review={"decision": "keep", "note": "Approved"}))
    assert build_briefing(run)["sections"]["commentary"] == ["S9"]
