"""Synthetic contract/routing regressions, not measurements of live Jev accuracy."""

import asyncio
import copy
import json

import httpx
import pytest

from app.briefing import route_source
from app.config import Settings, WATCHLIST
from app.judge import JUDGE_VERSION, QUESTIONS, judge_source


@pytest.mark.parametrize("passage,expected,destination", [
    ("Today, November 6, Aster announced a signed supply agreement. Deliveries begin next year.", "concrete_development", "company"),
    ("What happens next? Shares fell yesterday. August guidance predicted lower margins. Results are expected November 17; these figures could lift shares over the next six months.", "commentary", "review"),
    ("Shares fell 4% today on volume of 12 million, then recovered 1%.", "price_recap_only", "out"),
    ("Aster reported results. The excerpt does not say when the results were released.", "insufficient", "review"),
])
def test_temporal_rubric_is_sent_and_mocked_labels_drive_routes(passage, expected, destination):
    run = {"company": WATCHLIST[0], "window_start": "2026-11-03T18:00:00Z", "cutoff": "2026-11-06T18:00:00Z",
           "quote": {"fields": {"quote_time": "2026-11-06T17:00:00Z"}}}
    source = {"id": "S1", "title": "Synthetic fixture", "url": "https://example.test/story",
              "published_date": "2026-11-06T16:00:00Z", "passages": [passage],
              "date_check": {"outside_window": False, "uncertain": False, "timing": "before_quote"}}
    original = copy.deepcopy(source)

    def handler(request):
        body = json.loads(request.content)
        assert body["state"]["research_window_start"] == run["window_start"]
        assert body["state"]["research_cutoff"] == run["cutoff"]
        assert body["state"]["source"]["passages"] == [passage]
        instructions = body["questions"]["substance"]["instructions"]
        assert "Publication time is not event time" in instructions
        assert "August guidance" in instructions and "commentary" in instructions
        assert "new announcement is the event" in instructions
        assert "Do not force varied labels" in instructions
        answers = {name: {"type": "choice", "choice": choice, "confidence": 1.0,
                          "probabilities": {key: float(key == choice) for key in QUESTIONS[name]["criteria"]}}
                   for name, choice in (("scope", "company"), ("substance", expected))}
        return httpx.Response(200, json={"model": "synthetic-model", "answers": answers})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await judge_source(Settings(jev_key="synthetic"), client, run, source)

    result = asyncio.run(execute())
    assert result["valid"] and result["judge_version"] == JUDGE_VERSION == 2
    assert source == original
    source["judgment"] = result
    assert route_source(source)["final"] == destination
