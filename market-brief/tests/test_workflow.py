"""Synthetic integration fixtures only; these are not news or recorded live results."""

import copy
import json
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.import_run import import_recording
from app.judge import QUESTIONS
from app.store import ACTIVE, Store

# main creates its ASGI entry point on import. Never load a developer's .env.
with patch.object(Settings, "load", return_value=Settings()):
    from app.main import create_app


class SyntheticProviders:
    def __init__(self):
        self.calls = []
        self.timestamp = datetime.now(timezone.utc) - timedelta(hours=2)
        self.search_failures = 0
        self.empty = False
        self.bad_quote = None
        self.expansion_failure = None
        self.invalid_judgment = False

    def count(self, path):
        return sum(request.url.path == path for request in self.calls)

    def __call__(self, request):
        self.calls.append(request)
        path = request.url.path
        if path == "/prices/snapshot":
            assert request.headers["X-API-KEY"] == "secret-fin"
            ticker = request.url.params["ticker"]
            snapshot = dict(ticker=ticker, price=100, day_change=-3,
                            day_change_percent=-3, time=self.timestamp.isoformat(),
                            time_milliseconds=int(self.timestamp.timestamp() * 1000))
            if ticker == "NVDA" and self.bad_quote:
                snapshot.update(self.bad_quote)
            return httpx.Response(200, json={"snapshot": snapshot})
        body = json.loads(request.content)
        if path == "/search":
            assert request.headers["x-api-key"] == "secret-exa"
            if self.count(path) <= self.search_failures:
                return httpx.Response(401, json={"error": "Synthetic search denial"})
            published = (self.timestamp + timedelta(hours=1)).isoformat()
            results = [{"id": "synthetic-article", "url": "https://synthetic.invalid/story?utm_source=test#section",
                        "title": "Synthetic company announcement", "publishedDate": published,
                        "highlights": ["Synthetic fixture: NVIDIA announced a test-only development."]}]
            return httpx.Response(200, json={"results": [] if self.empty else results})
        if path == "/v1/systemone":
            assert request.headers["authorization"] == "Bearer secret-jev"
            choices = {"scope": "company", "substance": "concrete_development"}
            answers = {name: {"type": "choice", "choice": choices[name], "confidence": 1.0,
                              "probabilities": {key: float(key == choices[name]) for key in question["criteria"]}}
                       for name, question in QUESTIONS.items()}
            if self.invalid_judgment:
                answers["scope"]["probabilities"].pop("unclear")
            return httpx.Response(200, json={"model": "synthetic-jev-model", "answers": answers})
        if path == "/contents":
            assert request.headers["x-api-key"] == "secret-exa"
            url = body["ids"][0]
            if self.expansion_failure == "http":
                return httpx.Response(403, json={"error": "Synthetic contents denial"})
            return httpx.Response(200, json={
                "statuses": [{"id": url, "status": "error" if self.expansion_failure == "url" else "success"}],
                "results": [{"url": url, "highlights": [] if self.expansion_failure == "empty" else
                             ["Synthetic expanded passage, preserved verbatim."]}]})
        raise AssertionError(f"Unexpected provider request: {request.method} {request.url}")


@pytest.fixture
def providers():
    return SyntheticProviders()


@pytest.fixture
def settings(tmp_path):
    return Settings(data_dir=tmp_path, fin_data="secret-fin", exa_key="secret-exa", jev_key="secret-jev")


@pytest.fixture
def client(settings, providers):
    with TestClient(create_app(settings, httpx.MockTransport(providers))) as client:
        yield client


def poll(client, identity):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        response = client.get(f"/api/runs/{identity}")
        assert response.status_code == 200, response.text
        run = response.json()
        if run["status"] not in ACTIVE:
            return run
        time.sleep(0.01)
    pytest.fail(f"Pipeline did not finish within five seconds: {run}")


def start_run(client):
    response = client.post("/api/check-prices")
    assert response.status_code == 200, response.text
    quotes = response.json()["quotes"]
    quote = next(quote for quote in quotes if quote["ticker"] == "NVDA")
    response = client.post("/api/runs", json={"quote_id": quote["id"]})
    assert response.status_code == 200, response.text
    return poll(client, response.json()["id"])


def test_markdown_display_preserves_stored_evidence_and_exports(client):
    run = start_run(client)
    base = f"/api/runs/{run['id']}"
    stored = client.app.state.store.get("runs", run["id"])
    passage = "**Synthetic evidence**\n\n- First\n- Second\n\n[Source](https://example.com/report)"
    stored["sources"][0]["passages"] = [passage]
    client.app.state.store.save("runs", stored)
    replay = client.get(base + "?replay=true").json()
    assert "<strong>Synthetic evidence</strong>" in replay["sources"][0]["passages_html"][0]
    assert replay["sources"][0]["passages"] == [passage]
    updated = client.post(base + "/sources/S1/review", json={"decision": "keep", "note": "Synthetic review"}).json()
    assert "<ul>" in updated["sources"][0]["passages_html"][0]
    exported = client.get(base + "/export?format=json").json()
    assert exported["sources"][0]["passages"] == [passage]
    assert "passages_html" not in exported["sources"][0]


def test_reclassification_is_new_idempotent_jev_only_run(client, providers):
    original = start_run(client)
    identity = original["id"]
    original = client.post(f"/api/runs/{identity}/sources/S1/review", json={"decision": "keep", "note": "Prior review"}).json()
    before = client.app.state.store.get("runs", identity)
    before["readonly"] = True
    client.app.state.store.save("runs", before)
    call_counts = {path: providers.count(path) for path in ("/search", "/prices/snapshot", "/v1/systemone")}
    first = client.post(f"/api/runs/{identity}/reclassify")
    assert first.status_code == 200, first.text
    new = poll(client, first.json()["id"])
    assert new["id"] != identity and new["parent_run_id"] == identity
    assert new["kind"] == "reclassification" and new["settings"]["judge_version"] == 2
    assert new["quote"] == original["quote"]
    assert new["cutoff"] == original["cutoff"] and new["window_start"] == original["window_start"]
    assert new["sources"][0]["passages"] == original["sources"][0]["passages"]
    assert new["sources"][0]["previous_judgment"] == original["sources"][0]["judgment"]
    assert new["sources"][0]["previous_review"] == original["sources"][0]["review"]
    assert "review" not in new["sources"][0]
    assert providers.count("/v1/systemone") == call_counts["/v1/systemone"] + len(new["sources"])
    assert providers.count("/search") == call_counts["/search"]
    assert providers.count("/prices/snapshot") == call_counts["/prices/snapshot"]
    assert client.app.state.store.get("runs", identity) == before
    assert client.post(f"/api/runs/{identity}/reclassify").json()["id"] == new["id"]
    assert providers.count("/v1/systemone") == call_counts["/v1/systemone"] + len(new["sources"])


def test_reclassification_does_not_need_exa_and_rejects_missing_jev(client, settings):
    run = start_run(client)
    settings.exa_key = ""
    response = client.post(f"/api/runs/{run['id']}/reclassify")
    assert response.status_code == 200
    poll(client, response.json()["id"])
    settings.jev_key = ""
    assert client.post(f"/api/runs/{run['id']}/reclassify").status_code == 409


def test_new_rubric_does_not_reuse_an_old_run(client):
    first = start_run(client)
    from app.pipeline import JUDGE_VERSION
    with patch("app.pipeline.JUDGE_VERSION", JUDGE_VERSION + 1):
        second = client.post("/api/runs", json={"quote_id": first["quote"]["id"]})
        assert second.status_code == 200
        assert second.json()["id"] != first["id"]
        poll(client, second.json()["id"])


def test_saved_briefing_and_exports_include_all_sources_without_mutating_snapshot(client, providers):
    run = start_run(client)
    stored = client.app.state.store.get("runs", run["id"])
    template = stored["sources"][0]
    stored["sources"] = [{**copy.deepcopy(template), "id": f"S{i}"} for i in range(1, 9)]
    stored["briefing"]["sections"]["company"] = ["S1", "S2", "S3"]
    client.app.state.store.save("runs", stored)
    before = len(providers.calls)
    base = f"/api/runs/{run['id']}"
    expected = [f"S{i}" for i in range(1, 9)]
    assert client.get(base + "?replay=true").json()["briefing"]["sections"]["company"] == expected
    assert client.get(base + "/export?format=json").json()["briefing"]["sections"]["company"] == expected
    assert "### S8:" in client.get(base + "/export?format=md").text
    assert client.app.state.store.get("runs", run["id"]) == stored
    assert len(providers.calls) == before


def test_complete_workflow_review_expand_export_import_and_replay(client, providers, tmp_path):
    assert client.get("/api/config").json()["credentials"] == {"prices": True, "research": True}
    run = start_run(client)
    identity = run["id"]
    base = f"/api/runs/{identity}"
    assert run["status"] == "complete"
    assert run["trigger_mode"] == "manual"
    assert run["quote"]["fields"]["day_change_percent"] == -3
    assert run["quote"]["fields"]["currency_source"] == "configuration"
    assert providers.count("/prices/snapshot") == 3
    assert providers.count("/search") == 3
    assert providers.count("/v1/systemone") == 1
    assert len(run["sources"]) == 1
    source = run["sources"][0]
    assert source["query_ids"] == ["Q1", "Q2", "Q3"]
    assert len(source["raw_refs"]) == 3
    assert source["url"] == "https://synthetic.invalid/story"
    assert source["date_check"]["timing"] == "after_quote"
    assert source["route"]["final"] == "company"
    assert run["briefing"]["sections"]["company"] == ["S1"]
    for query in run["queries"]:
        assert query["request"]["startPublishedDate"] == run["window_start"]
        assert query["request"]["endPublishedDate"] == run["cutoff"]
        assert query["request"]["contents"] == {"highlights": True}
    assert source["judgment"]["call"]["request"]["body"]["state"]["source"]["passages"] == source["passages"]

    before = len(providers.calls)
    duplicate = client.post("/api/runs", json={"quote_id": run["quote"]["id"]})
    assert duplicate.json()["id"] == identity
    assert len(providers.calls) == before
    review = client.post(base + "/sources/S1/review", json={"decision": "out", "note": "Synthetic exclusion"})
    assert review.status_code == 200
    reviewed = review.json()["sources"][0]
    assert reviewed["route"]["automated"] == "company"
    assert reviewed["route"]["final"] == "out"
    assert reviewed["judgment"] == source["judgment"]
    assert reviewed["review"]["reviewed_at"]
    assert len(providers.calls) == before
    assert client.post(base + "/sources/S1/review", json={"decision": "keep", "note": "Synthetic approval"}).status_code == 200

    expanded = client.post(base + "/sources/S1/expand")
    assert expanded.status_code == 200, expanded.text
    source = expanded.json()["sources"][0]
    assert source["original_highlights"] == run["sources"][0]["original_highlights"]
    assert source["expanded_passages"] == ["Synthetic expanded passage, preserved verbatim."]
    assert len(source["judgment_history"]) == 2
    assert providers.count("/contents") == 1
    assert providers.count("/v1/systemone") == 2
    assert client.post(base + "/sources/S1/expand").status_code == 409

    exported = client.get(base + "/export?format=json").json()
    markdown = client.get(base + "/export?format=md")
    assert markdown.status_code == 200
    for text in ("-3%", "after_quote", "On-demand research", "do not establish what caused it", "Synthetic expanded passage"):
        assert text in markdown.text
    before = len(providers.calls)
    assert client.get(base + "?replay=true").json()["readonly"] is True
    assert client.get("/api/runs").json()["runs"][0]["id"] == identity
    assert len(providers.calls) == before

    imported_settings = Settings(data_dir=tmp_path / "imported")
    imported = import_recording(copy.deepcopy(exported), Store(imported_settings.data_dir))
    for key in ("created_at", "cutoff", "window_start", "sources", "quote", "queries"):
        assert imported[key] == exported[key]
    with pytest.raises(ValueError, match="already exists"):
        import_recording(copy.deepcopy(exported), Store(imported_settings.data_dir))
    with TestClient(create_app(imported_settings, httpx.MockTransport(providers))) as replay:
        assert replay.get(base).json()["readonly"] is True
        assert replay.post(base + "/sources/S1/review", json={"decision": "out", "note": "Must fail"}).status_code == 409
        assert replay.post(base + "/sources/S1/expand").status_code == 409
        assert replay.get(base + "/export?format=json").status_code == 200
        assert replay.get(base + "/export?format=md").status_code == 200
    assert len(providers.calls) == before


@pytest.mark.parametrize("bad", [
    {"price": 0}, {"ticker": "WRONG"}, {"day_change_percent": "3"},
    {"time": None, "time_milliseconds": None}, {"time_milliseconds": 0},
])
def test_invalid_quote_blocks_only_its_research(client, providers, bad):
    providers.bad_quote = bad
    quotes = client.post("/api/check-prices").json()["quotes"]
    invalid = next(q for q in quotes if q["ticker"] == "NVDA")
    assert invalid["valid"] is False
    assert invalid["error"]
    assert all(q["valid"] for q in quotes if q["ticker"] != "NVDA")
    assert client.post("/api/runs", json={"quote_id": invalid["id"]}).status_code == 422
    assert client.post("/api/runs", json={"quote_id": "not-stored"}).status_code == 422
    assert providers.count("/search") == 0


@pytest.mark.parametrize("missing", ["fin_data", "exa_key", "jev_key"])
def test_missing_credentials_disable_only_required_actions(settings, providers, missing):
    setattr(settings, missing, "")
    with TestClient(create_app(settings, httpx.MockTransport(providers))) as client:
        config = client.get("/api/config").json()
        assert config["credentials"]["prices"] == (missing != "fin_data")
        assert config["credentials"]["research"] == (missing == "fin_data")
        response = client.post("/api/check-prices")
        if missing == "fin_data":
            assert response.status_code == 409
            assert not providers.calls
        else:
            quote = response.json()["quotes"][0]
            assert client.post("/api/runs", json={"quote_id": quote["id"]}).status_code == 409
            assert providers.count("/search") == 0


@pytest.mark.parametrize("failures,empty,status,judgments", [(1, False, "partial", 1), (3, False, "failed", 0), (0, True, "no_sources", 0)])
def test_search_outcomes(client, providers, failures, empty, status, judgments):
    providers.search_failures, providers.empty = failures, empty
    run = start_run(client)
    assert run["status"] == status
    assert providers.count("/search") == 3
    assert providers.count("/v1/systemone") == judgments
    assert sum(bool(q["call"]["error"]) for q in run["queries"]) == failures
    if failures:
        assert run["warnings"]
    if not judgments:
        assert run["sources"] == []
        assert run["briefing"]["sections"]["company"] == []


@pytest.mark.parametrize("failure", ["http", "url", "empty"])
def test_expansion_failure_retains_evidence_and_judgment(client, providers, failure):
    run = start_run(client)
    original = run["sources"][0]
    providers.expansion_failure = failure
    response = client.post(f"/api/runs/{run['id']}/sources/S1/expand")
    assert response.status_code == 200
    updated = response.json()
    source = updated["sources"][0]
    for key in ("passages", "original_highlights", "judgment", "judgment_history", "route"):
        assert source[key] == original[key]
    assert source["expansion"]["success"] is False
    assert updated["busy"] is False
    assert any("last usable evidence retained" in warning for warning in updated["warnings"])
    assert providers.count("/v1/systemone") == 1


def test_invalid_judgment_is_reviewable_not_included(client, providers):
    providers.invalid_judgment = True
    run = start_run(client)
    assert run["status"] == "partial"
    assert run["sources"][0]["judgment"]["valid"] is False
    assert run["sources"][0]["route"]["final"] == "review"
    assert run["briefing"]["unresolved"] == ["S1"]


@pytest.mark.parametrize("status,busy", [("queued", False), ("searching", False), ("judging", False), ("complete", True)])
def test_restart_interrupts_unfinished_work_without_calls(client, settings, providers, status, busy):
    run = start_run(client)
    run.update(status=status, busy=busy)
    client.app.state.store.save("runs", run)
    before = len(providers.calls)
    with TestClient(create_app(settings, httpx.MockTransport(providers))) as restarted:
        interrupted = restarted.get(f"/api/runs/{run['id']}").json()
        assert interrupted["status"] == "interrupted"
        assert interrupted["busy"] is False
        assert interrupted["sources"] == run["sources"]
        assert any("interrupted" in warning for warning in interrupted["warnings"])
    assert len(providers.calls) == before


def test_redaction_in_reviews_api_exports_and_saved_artifacts(client, settings):
    run = start_run(client)
    base = f"/api/runs/{run['id']}"
    note = "Synthetic secret-fin secret-exa secret-jev Bearer synthetic-other-token"
    response = client.post(base + "/sources/S1/review", json={"decision": "keep", "note": note})
    texts = [response.text, client.get(base).text, client.get("/api/config").text,
             client.get(base + "/export?format=json").text, client.get(base + "/export?format=md").text]
    texts.extend(path.read_text() for path in settings.data_dir.rglob("*.json"))
    for text in texts:
        for secret in settings.secrets + ["synthetic-other-token"]:
            assert secret not in text
    assert "[REDACTED]" in response.text


@pytest.fixture
def recording(client):
    run = start_run(client)
    return client.get(f"/api/runs/{run['id']}/export?format=json").json()


@pytest.mark.parametrize("case", [
    "schema", "naive_cutoff", "bad_created", "reversed_window", "quote_time", "source_time",
    "briefing_ref", "unresolved_ref", "query_ref", "raw_ref", "duplicate_source", "quote_fields",
])
def test_import_rejects_invalid_schema_timestamps_and_references(recording, tmp_path, case):
    payload = copy.deepcopy(recording)
    if case == "schema":
        payload["schema_version"] = 99
    elif case == "naive_cutoff":
        payload["cutoff"] = "2026-11-06T10:00:00"
    elif case == "bad_created":
        payload["created_at"] = "not-a-time"
    elif case == "reversed_window":
        payload["window_start"] = (datetime.fromisoformat(payload["cutoff"]) + timedelta(hours=1)).isoformat()
    elif case == "quote_time":
        payload["quote"]["retrieved_at"] = "not-a-time"
    elif case == "source_time":
        payload["sources"][0]["retrieved_at"] = "not-a-time"
    elif case == "briefing_ref":
        payload["briefing"]["sections"]["company"] = ["S999"]
    elif case == "unresolved_ref":
        payload["briefing"]["unresolved"] = ["S999"]
    elif case == "query_ref":
        payload["sources"][0]["query_ids"] = ["Q999"]
    elif case == "raw_ref":
        payload["sources"][0]["raw_refs"][0]["result_index"] = 999
    elif case == "duplicate_source":
        payload["sources"].append(copy.deepcopy(payload["sources"][0]))
    elif case == "quote_fields":
        payload["quote"]["fields"]["price"] = -100
    store = Store(tmp_path / "invalid-import")
    with pytest.raises((ValueError, KeyError, TypeError)):
        import_recording(payload, store)
    assert store.list("runs") == []
    assert store.list("quotes") == []
