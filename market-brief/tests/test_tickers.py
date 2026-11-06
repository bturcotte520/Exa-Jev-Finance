"""Synthetic ticker responses; no live providers or developer credentials."""

import copy
import time
from collections import Counter
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.import_run import import_recording, validate_recording
from app.models import now
from app.prices import company_from_call, validate_company
from app.store import ACTIVE, Store

with patch.object(Settings, "load", return_value=Settings()):
    from app.main import create_app


class Providers:
    def __init__(self):
        self.calls = []
        self.facts = {}
        self.snapshot = {}
        self.status = 200

    def __call__(self, request):
        self.calls.append(request)
        ticker = request.url.params.get("ticker")
        if request.url.path == "/company/facts":
            assert request.headers["X-API-KEY"] == "synthetic-fin-key"
            return httpx.Response(self.status, json={"company_facts": {
                "ticker": ticker, "name": f"Synthetic {ticker} Company", "is_active": True,
                "sector": "Synthetic sector", "industry": "Synthetic industry", "exchange": "NASDAQ",
                **self.facts}})
        if request.url.path == "/prices/snapshot":
            return httpx.Response(200, json={"snapshot": {
                "ticker": ticker, "price": 100, "day_change": 3,
                "day_change_percent": 3, "time": now(), **self.snapshot}})
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": []})
        raise AssertionError(f"Unexpected provider call: {request.url}")


@pytest.fixture
def setup(tmp_path):
    settings = Settings(data_dir=tmp_path / "live", fin_data="synthetic-fin-key",
                        exa_key="synthetic-exa-key", jev_key="synthetic-jev-key")
    providers = Providers()
    with TestClient(create_app(settings, httpx.MockTransport(providers))) as client:
        yield client, providers, settings


@pytest.mark.parametrize("ticker,normalized", [(" aapl ", "AAPL"), ("brk.b", "BRK.B"), ("brk-b", "BRK-B")])
def test_add_normalizes_and_captures_only_requested_quote(setup, ticker, normalized):
    client, providers, _ = setup
    response = client.post("/api/tickers", json={"ticker": ticker})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["company"]["ticker"] == normalized
    assert result["quote"]["valid"] is True
    assert result["quote"]["fields"]["day_change_percent"] == 3
    assert result["quote"]["fields"]["currency_source"] == "configuration"
    assert result["quote"]["fields"]["currency"] == "USD"
    assert [r.url.path for r in providers.calls] == ["/company/facts", "/prices/snapshot"]
    assert all(r.url.params["ticker"] == normalized for r in providers.calls)
    assert "synthetic-fin-key" not in response.text
    assert "headers" not in result["quote"]["company_call"]["request"]


@pytest.mark.parametrize("body", [
    {"ticker": ""}, {"ticker": "A" * 13}, {"ticker": "A/B"}, {"ticker": "A B"},
    {"ticker": "A?x=1"}, {"ticker": ".A"}, {"ticker": "A-"}, {"ticker": "A..B"},
    {"ticker": "ß"}, {"ticker": 123}, {"ticker": "AAPL", "price": 1},
    {"ticker": "AAPL", "company": "Invented"},
])
def test_invalid_input_never_calls_provider(setup, body):
    client, providers, _ = setup
    assert client.post("/api/tickers", json=body).status_code == 422
    assert providers.calls == []


@pytest.mark.parametrize("facts,status", [
    ({"ticker": "WRONG"}, 200), ({"name": " "}, 200), ({"is_active": False}, 200),
    ({}, 404), ({}, 401), ({}, 403),
])
def test_failed_facts_never_invent_identity_or_fetch_snapshot(setup, facts, status):
    client, providers, _ = setup
    providers.facts, providers.status = facts, status
    response = client.post("/api/tickers", json={"ticker": "AAPL"})
    assert response.status_code == 422
    assert len(providers.calls) == 1
    assert client.app.state.store.list("quotes") == []
    assert len(client.get("/api/config").json()["watchlist"]) == 3


def test_missing_business_labels_are_not_guessed(setup):
    client, providers, _ = setup
    providers.facts = {"sector": None, "industry": "", "exchange": None}
    company = client.post("/api/tickers", json={"ticker": "AAPL"}).json()["company"]
    assert company["sector"] == company["industry"] == company["exchange"] == "Not reported"
    assert company["context"] == "Company announcements, earnings, guidance, and filings"


def test_invalid_snapshot_saved_without_registering_company(setup):
    client, providers, _ = setup
    providers.snapshot = {"ticker": "WRONG"}
    response = client.post("/api/tickers", json={"ticker": "AAPL"})
    assert response.status_code == 200
    quote = response.json()["quote"]
    assert quote["valid"] is False
    assert "ticker" in quote["error"]
    assert client.app.state.store.get("quotes", quote["id"]) == quote
    config = client.get("/api/config").json()
    assert len(config["watchlist"]) == 3
    assert config["quotes"] == []
    assert client.post("/api/runs", json={"quote_id": quote["id"]}).status_code == 422


def test_restart_discovery_and_refresh_each_company_once(setup):
    client, providers, settings = setup
    first = client.post("/api/tickers", json={"ticker": "AAPL"}).json()["quote"]
    latest = client.post("/api/tickers", json={"ticker": "AAPL"}).json()["quote"]
    client.post("/api/tickers", json={"ticker": "BRK.B"})
    before = len(providers.calls)
    with TestClient(create_app(settings, httpx.MockTransport(providers))) as restarted:
        config = restarted.get("/api/config").json()
        assert len(providers.calls) == before
        assert {c["ticker"] for c in config["watchlist"]} == {"NVDA", "AMD", "MSFT", "AAPL", "BRK.B"}
        refreshed = restarted.post("/api/check-prices").json()["quotes"]
        assert Counter(r.url.params["ticker"] for r in providers.calls[before:]) == {
            "NVDA": 1, "AMD": 1, "MSFT": 1, "AAPL": 1, "BRK.B": 1}
        custom = next(q for q in refreshed if q["ticker"] == "AAPL")
        assert custom["company_call"] == latest["company_call"]
        assert restarted.app.state.store.get("quotes", first["id"]) == first
        providers.snapshot = {"price": -1}
        restarted.post("/api/check-prices")
        config = restarted.get("/api/config").json()
        assert len(config["watchlist"]) == 5
        assert all(not q["valid"] for q in config["quotes"])


def exported_custom(client):
    client.post("/api/tickers", json={"ticker": "AAPL"})
    quote = next(q for q in client.post("/api/check-prices").json()["quotes"] if q["ticker"] == "AAPL")
    run = client.post("/api/runs", json={"quote_id": quote["id"]}).json()
    for _ in range(100):
        run = client.get(f"/api/runs/{run['id']}").json()
        if run["status"] not in ACTIVE:
            break
        time.sleep(0.01)
    assert run["status"] == "no_sources"
    return client.get(f"/api/runs/{run['id']}/export").json()


def test_custom_export_import_preserves_capture_and_never_registers_import(setup, tmp_path):
    client, providers, _ = setup
    exported = exported_custom(client)
    before = len(providers.calls)
    settings = Settings(data_dir=tmp_path / "import", fin_data="synthetic-fin-key")
    store = Store(settings.data_dir)
    imported = import_recording(copy.deepcopy(exported), store)
    assert imported["quote"] == exported["quote"]
    assert store.get("quotes", exported["quote"]["id"])["origin"] == "imported"
    with TestClient(create_app(settings, httpx.MockTransport(providers))) as replay:
        assert replay.get(f"/api/runs/{imported['id']}/export").json()["quote"] == exported["quote"]
        config = replay.get("/api/config").json()
        assert len(config["watchlist"]) == 3
        assert config["quotes"] == []
        assert len(providers.calls) == before
        assert replay.post("/api/runs", json={"quote_id": exported["quote"]["id"]}).status_code == 422
        replay.post("/api/check-prices")
        assert len(providers.calls) == before + 3


@pytest.mark.parametrize("case", ["missing_call", "name", "facts_ticker", "request_ticker", "inactive", "price", "snapshot_ticker"])
def test_custom_import_revalidates_captured_identity_and_quote(setup, case):
    client, _, _ = setup
    payload = exported_custom(client)
    quote = payload["quote"]
    if case == "missing_call":
        quote.pop("company_call")
    elif case == "name":
        quote["company"]["company"] = payload["company"]["company"] = "Made up"
    elif case == "facts_ticker":
        quote["company_call"]["response"]["company_facts"]["ticker"] = "WRONG"
    elif case == "request_ticker":
        quote["company_call"]["request"]["params"]["ticker"] = "WRONG"
    elif case == "inactive":
        quote["company_call"]["response"]["company_facts"]["is_active"] = False
    elif case == "price":
        quote["fields"]["price"] = 999
    else:
        quote["call"]["response"]["snapshot"]["ticker"] = "WRONG"
    with pytest.raises(ValueError):
        validate_recording(payload)


def test_missing_key_blocks_ticker_entry(tmp_path):
    providers = Providers()
    with TestClient(create_app(Settings(data_dir=tmp_path), httpx.MockTransport(providers))) as client:
        assert client.post("/api/tickers", json={"ticker": "AAPL"}).status_code == 409
    assert providers.calls == []


@pytest.mark.parametrize("response", [None, [], {}, {"company_facts": []}, {"company_facts": None}])
def test_malformed_facts_fail_validation(response):
    with pytest.raises(ValueError):
        validate_company(response, "AAPL")


def test_malformed_captured_facts_request_fails_validation():
    with pytest.raises(ValueError):
        company_from_call({"request": None}, "AAPL")
