import json
from pathlib import Path

from app.prices import validate_snapshot


def test_labeled_synthetic_snapshot():
    fixture = json.loads((Path(__file__).parent / "fixtures/synthetic_snapshot.json").read_text())
    assert "SYNTHETIC" in fixture["fixture_notice"]
    fields = validate_snapshot(fixture, "NVDA", "2026-11-06T10:00:00Z")
    assert fields["day_change_percent"] == 3
    assert fields["prior_date"]
