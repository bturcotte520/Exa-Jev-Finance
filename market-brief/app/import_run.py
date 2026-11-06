import argparse
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .config import Settings, WATCHLIST
from .models import RecordedRun
from .provider import parse_time
from .store import Store, redact


def validate_recording(payload):
    recording = RecordedRun.model_validate(payload)
    if recording.window_start > recording.cutoff:
        raise ValueError("Invalid research window")
    quote = payload["quote"]
    if not quote.get("valid") or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", quote.get("id", "")):
        raise ValueError("A validated quote with a safe ID is required")
    from .prices import company_from_call, validate_snapshot
    # Revalidate the provider artifact, rather than trusting an imported valid flag.
    company = next((c for c in WATCHLIST if c["ticker"] == quote.get("ticker")), None)
    if company is None:
        company = company_from_call(quote.get("company_call"), quote.get("ticker"))
    elif "company_call" in quote:
        company_from_call(quote["company_call"], quote.get("ticker"))
    if payload["company"] != company or quote.get("company") != company:
        raise ValueError("Unknown or inconsistent company identity")
    if quote["call"].get("error"):
        raise ValueError("Quote request failed")
    fields = validate_snapshot(quote["call"]["response"], quote["ticker"], quote["retrieved_at"], company)
    if fields != quote.get("fields"):
        raise ValueError("Validated quote fields disagree with original response")
    query_ids = {query.get("id") for query in payload["queries"]}
    if len(query_ids) != len(payload["queries"]) or not query_ids <= {"Q1", "Q2", "Q3"}:
        raise ValueError("Invalid query IDs")
    seen = set()
    for source in payload["sources"]:
        identity = source.get("id", "")
        if not re.fullmatch(r"S[1-9][0-9]*", identity) or identity in seen:
            raise ValueError("Invalid or duplicate source ID")
        seen.add(identity)
        if urlsplit(source.get("url", "")).scheme not in {"http", "https"}:
            raise ValueError("Source URLs must use HTTP or HTTPS")
        if not isinstance(source.get("passages"), list) or not all(isinstance(p, str) for p in source["passages"]):
            raise ValueError("Source passages must be strings")
        for key in ("title", "date_check", "route", "query_ids", "original_highlights"):
            if key not in source:
                raise ValueError(f"Missing source field: {key}")
        if not isinstance(source["date_check"], dict) or not isinstance(source["route"], dict):
            raise ValueError("Invalid source routing schema")
        if not isinstance(source["query_ids"], list) or not set(source["query_ids"]) <= query_ids:
            raise ValueError("Source references unknown query")
        if not isinstance(source["title"], str) or not isinstance(source["original_highlights"], list):
            raise ValueError("Invalid source text schema")
        parse_time(source["retrieved_at"])
        for ref in source.get("raw_refs", []):
            query = next((q for q in payload["queries"] if q["id"] == ref.get("query_id")), None)
            index = ref.get("result_index")
            if not query or type(index) is not int or not 0 <= index < len(query["call"]["response"].get("results", [])):
                raise ValueError("Invalid raw response reference")
    sections = payload["briefing"].get("sections")
    if not isinstance(sections, dict) or set(sections) != {"company", "context", "commentary"}:
        raise ValueError("Invalid briefing sections")
    for ids in payload["briefing"].get("sections", {}).values():
        if not isinstance(ids, list) or any(identity not in seen for identity in ids):
            raise ValueError("Briefing references an unknown source")
    unresolved = payload["briefing"].get("unresolved")
    if not isinstance(unresolved, list) or any(identity not in seen for identity in unresolved):
        raise ValueError("Invalid unresolved source references")
    return payload


def import_recording(payload, store):
    validate_recording(payload)
    payload = redact(payload, store.secrets)
    existing = store.get("runs", payload["id"])
    if existing:
        raise ValueError("Run ID already exists; existing data was not changed")
    quote = payload["quote"]
    existing_quote = store.get("quotes", quote["id"])
    if existing_quote and {k: v for k, v in existing_quote.items() if k != "origin"} != {k: v for k, v in quote.items() if k != "origin"}:
        raise ValueError("Quote ID conflicts with saved data")
    payload.update(readonly=True, imported=True, busy=False)
    if not existing_quote:
        store.save("quotes", {**quote, "origin": "imported"})
    return store.save("runs", payload)


def main():
    parser = argparse.ArgumentParser(description="Import a genuine recorded run without calling providers")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    if not args.path.exists():
        parser.exit(1, "Recorded example pending capture. Supply a genuine exported run.json; synthetic news is not provided.\n")
    settings = Settings.load()
    try:
        result = import_recording(json.loads(args.path.read_text()), Store(settings.data_dir, settings.secrets))
    except (ValueError, KeyError, TypeError) as exc:
        parser.exit(1, f"Import rejected: {type(exc).__name__}. Check the exported schema and IDs.\n")
    print(f"Imported read-only run {result['id']}")


if __name__ == "__main__":
    main()
