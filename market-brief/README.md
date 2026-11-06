# Market Brief

A local, inspectable draft of reporting around a stock's daily move. Financial Datasets supplies prices, Exa retrieves evidence, Jev classifies each source, and Python rules assemble the briefing. No writing model, automatic movement threshold, accounts, trading, notifications, or scheduled jobs.

## Setup

Python 3.11+ is required. Tested on macOS arm64 with Python 3.14.7. From `market-brief/`:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open <http://127.0.0.1:8000>. Run one process, without `--workers`; background execution is deliberately local. Restarting marks unfinished work interrupted and never retries providers automatically. The app opens without credentials.

## Run the recorded example

**Public example pending.** Local user-captured runs remain private in `data/`; they are not redistributed as examples. `examples/recorded-run/` intentionally does not contain a pretend `run.json`. Synthetic provider responses exist only in tests and are labeled as such.

After a genuine run is exported as JSON and its source/data redistribution permissions are checked, place that file at `examples/recorded-run/run.json` and run:

```sh
python -m app.import_run examples/recorded-run/run.json
```

The importer validates the recording and quote artifact, preserves captured timestamps, rejects conflicting IDs, and marks the import read-only. It does not call providers. If the sample is absent, it explains that capture is pending rather than inventing one. Start the app and choose the recording in Saved runs. Opening any saved run is a **Recorded run** view: inspection and exports work, review and expansion are disabled, and no provider requests occur. This view does not mutate an active original run. Imported runs are also immutable at the API boundary.

## Run with your own keys

The existing `../.env` is read in place and is never modified. A `market-brief/.env` can also be used; its values take precedence over the parent file. Environment variables take final precedence. Names are **case-sensitive**:

```dotenv
fin_data=
exa_key=
jev_key=
JEV_MODEL=jev-latest
```

Use the exact existing names, not alternate provider variable names. `JEV_MODEL` is optional and defaults to `jev-latest`. Credentials remain server-side. The config endpoint only exposes availability flags. Do not commit `.env`, the virtual environment, or private `data/`.

1. Click **Check prices** to refresh the three presets and any previously added tickers. Alternatively, enter a supported **US ticker** and click **Add ticker**: the server verifies its company facts and fetches only that stock's quote. This uses Financial Datasets API credits and does not start research. Unsupported, inactive, mismatched, or unavailable companies produce an error, not invented metadata. Company names and sector/industry come from the provider; USD is explicit configuration for the US snapshot endpoint, not global coverage.
2. Select a validated quote and click **Research company**. This explicitly authorizes three concurrent Exa searches and at most 12 Jev evaluations before retries. Repeated submission of the same quote and settings reuses its run.
3. Inspect evidence and its requests, answers, routes, timing and usage. Optionally use **Get more context** for at most two distinct sources, once each. Each expansion uses one Contents request and, only after successful retrieval, one Jev request before retries. Failed expansion attempts count against the cap and retain usable evidence.
4. Review with a required note using **Keep in draft** or **Keep out**. Review never calls a provider. Export Markdown for reading or JSON for standalone inspection/import.

The **Latest fetched prices** panel and **Quote used for this run** banner are separate. Refreshing a quote or selecting another ticker does not change an existing run's price, movement, or research window. Imported-only quotes are not presented as newly fetched prices. Excerpts show a 280-character preview; **Read full excerpt** reveals all original passages, and the inspector always shows the complete evidence.

**Reclassify saved evidence** is an explicit paid Jev-only action. It creates a separate run linked to the original, keeps the original quote/window/passages, and applies the current classification rubric. It makes no new Exa or price calls. Previous judgments remain inspectable; previous human reviews are retained as history but are not applied to the new classification. Repeated submission for the same parent/evidence/model/rubric reuses the derived run. Opening a recording alone still makes no provider calls.

No paid provider request is made by startup, polling, import, inspection, export, or tests. Only explicit live buttons invoke providers. Missing credentials disable only the corresponding live actions. Requests time out at 30 seconds and retry rate limits/temporary failures at most twice. Authentication, payment and malformed-request responses are not retried. Retries and reported costs may increase real usage; no access is purchased by this app.

## Evidence Workflow

`app/prices.py` validates ticker identity, positive finite price, signed daily change/percentage, and quote timestamps. The percentage is already a percentage: `3` displays as `3%`. When both timestamps exist they must agree to subsecond precision. Provider quote time and retrieval time stay separate. Prior calendar-day quotes are explicitly labeled **Prior-date quote** using UTC dates. USD is watchlist configuration, not a field invented in the provider response.

`build_queries` and `search_sources` in `app/research.py` freeze a 72-hour window at run creation, independently of quote time. Three searches cover direct company developments, connected customers/suppliers/competitors, and sector/policy/macro context. Each uses `type: auto`, four results and highlights. Duplicate URLs merge only after fragment/known tracking-parameter normalization; all original URLs, query IDs and raw references remain inspectable. Partial search failures stay visible.

Publication checks are independent of the search filters. Missing dates, ambiguous timestamps and normalized midnight dates remain uncertain. Sources show **before_quote**, **after_quote**, **at_quote**, or **uncertain**. A date-only source may have known ordering while its exact time remains uncertain. Known outside-window publications are excluded. Pages fetched now are not historical snapshots.

`QUESTIONS`, `make_state`, and `judge_source` in `app/judge.py` pass original passages unchanged to two Choice questions in one request: **scope** (company / business_connection / sector_macro / unrelated / unclear) and **substance** (concrete_development / price_recap_only / commentary / insufficient). Concurrency is capped at three. Missing passages skip Jev. Whole passages omitted to respect the conservative context budget are recorded. Requests, answers, actual model and usage remain inspectable. Invalid choices/probabilities remain unclassified. Confidence is neither factual truth nor the probability an event caused a price move.

Rubric v2 supplies the research window's start as well as its cutoff and explicitly separates publication time from event time. New concrete developments require evidence of a new event, announcement, or disclosure in that window. An outlook recycling old guidance is commentary, not a new development; unclear event timing is insufficient. A newly announced plan for a future launch can still qualify because the announcement is new. The rubric does not force varied classifications. Older recordings retain their original answers and show an older-rubric notice. Mocked regressions verify the request contract and routing, not live-model accuracy; the stricter rubric still requires evaluation on real evidence through an explicit run.

`route_source` in `app/briefing.py` applies date exclusion, review requirements, relevance/substance exclusions, commentary review, then company/context routing. Human review is stored separately from automated routes and original judgments, including review history and timestamps. Approval cannot erase uncertain dates or commentary labels. Outside-window sources remain excluded. The briefing includes every qualifying source, grouped into company developments, context, and approved commentary. Original search order breaks publication-date ties. Saved runs and exports rebuild the uncapped briefing from their existing evidence without provider calls or changes to stored snapshots. A single **Exa Sources & Jev Classifications** disclosure lists source titles/links alongside relevance and content labels; per-source context, inspection, and review controls expand on demand. The briefing sits underneath. Search still returns up to 12 sources; there is no separate five-source briefing limit.

> These are reported developments around the observed move. They do not establish what caused it.

## Persistence And Inspection

SQLite stores quote/run JSON, IDs, timestamps and indexed run status. Sanitized artifacts are written to `data/quotes/<id>/quote.json` and `data/runs/<id>/run.json`. The run JSON embeds exact sanitized provider requests/responses, quote artifacts, source provenance, evidence, date checks, classification histories, routes, reviews, warnings, call attempts and usage/timing. Headers are not recorded. Known secret values and credential-named fields are redacted. JSON is standalone; no private artifact path is required for replay.

The drawer includes Source, Exa request (select any originating query), Jev request, Jev response, Routing and Usage/timing tabs. Quote inspection has Quote request and Quote source tabs. Source passages display Markdown through markdown-it-py 4.2.0 with raw HTML disabled, remote images suppressed, and links restricted to HTTP/HTTPS. This display is derived on read; stored passages, JSON exports, and Jev inputs retain the original text. Local same-origin mutation checks and a restrictive content-security policy are enabled. This is a local single-user app, not a public multi-user server; bind to loopback.

## Tests And Versions

```sh
python -m pytest -q
python -m pip check
```

Tests use mocked HTTP transports and temporary stores, not genuine news or paid integrations. `requirements.in` records direct dependencies; `requirements.txt` locks all tested runtime/test versions. Key versions: FastAPI 0.162.0, httpx 0.28.1, Pydantic 2.14.3, uvicorn 0.54.0, pytest 9.2.0. The requested default model is `jev-latest`; current docs checked November 6, 2026 identify `jev-1.14.0`, but the actual returned model is recorded per call rather than assumed. No live integration/model output has been verified by the build.

## References

- [Financial Datasets snapshot schema](https://docs.financialdatasets.ai/api/prices/snapshot)
- [Exa Search](https://exa.ai/docs/search/quickstart.md) and [Contents](https://exa.ai/docs/contents/quickstart.md)
- [Jev System One](https://docs.typesafe.ai/api/system-one) and [Choice](https://docs.typesafe.ai/primitives/choice)
- [Original Exa evidence-to-Jev demo](https://demos.exa.ai/jev-web-context), credited for the evidence-to-classification pattern, not reproduced as an empty-evidence comparison.
- [Revolut customer case](https://exa.ai/customers/revolut), inspiration for financial web research. This app does not claim that Revolut uses Jev or reproduce Revolut's internal implementation.
