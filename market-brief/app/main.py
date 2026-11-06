import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from .config import ROOT, Settings, WATCHLIST
from .models import ReviewRequest, RunRequest, TickerRequest, now
from .pipeline import Pipeline
from .prices import check_prices, company_from_call
from .provider import call_api, parse_time
from .rendering import display_run
from .briefing import build_briefing
from .store import ACTIVE, Store, redact


def markdown(run):
    def text(value):
        return str(value).replace("<", "&lt;").replace(">", "&gt;").replace("[", "\\[").replace("]", "\\]")

    quote = run["quote"]["fields"]
    lines = [f"# {text(run['company']['company'])} ({run['company']['ticker']})", "",
             "On-demand research | Draft of reported developments", "",
             f"Observed daily change: {quote['day_change_percent']:+g}%",
             f"Quote as of: {quote['quote_time']} | {quote['provider']}",
             f"Retrieved at: {run['quote']['retrieved_at']}",
             f"Research cutoff: {run['cutoff']}",
             f"Research window: {run['window_start']} to {run['cutoff']}",
             f"Run status: {run['status']}", "",
             run["briefing"].get("disclaimer", "These are reported developments around the observed move. They do not establish what caused it."), ""]
    if quote.get("prior_date") or parse_time(quote["quote_time"]).date() < parse_time(run["created_at"]).date():
        lines += ["Prior-date quote", ""]
    sources = {s["id"]: s for s in run["sources"]}
    for key, title in (("company", "Company developments"), ("context", "Broader context"), ("commentary", "Commentary")):
        ids = run["briefing"].get("sections", {}).get(key, [])
        if key == "commentary" and not ids:
            continue
        lines += [f"## {title}", ""]
        if not ids:
            lines += ["No qualifying source found.", ""]
        for identity in ids:
            source = sources[identity]
            lines += [f"### {identity}: {text(source['title'])}",
                      f"Publication: {text(source.get('published_date') or 'Unknown')} | {text(source['date_check'].get('timing', 'Uncertain'))}",
                      f"Date precision: {text(source.get('publication_precision', 'unknown'))}; date uncertainty: {source['date_check'].get('uncertain', True)}", ""]
            for passage in source["passages"]:
                lines += ["> " + text(passage).replace("\n", "\n> "), ""]
            lines += [f"Source: {text(source['domain'])} | {text(source['url'])}", ""]
            if source.get("review"):
                lines += [f"Human review: {text(source['review']['note'])}", ""]
    lines += ["## Still unresolved", ""]
    unresolved = run["briefing"].get("unresolved", [])
    lines += [f"- {identity}: {text(sources[identity]['title'])} | {text(sources[identity]['route']['reason'])} | {text(sources[identity]['date_check'].get('timing', 'Uncertain'))}" for identity in unresolved]
    if not unresolved:
        lines.append("No unresolved sources.")
    lines += ["", "## Warnings", ""] + [text(w) for w in run["warnings"]]
    lines += ["", "Pages fetched at retrieval time are not historical snapshots. Classification confidence is not factual truth or probability of market causation."]
    return "\n\n".join(lines)


def create_app(settings=None, transport=None):
    settings = settings or Settings.load()

    @asynccontextmanager
    async def lifespan(app):
        store = Store(settings.data_dir, settings.secrets)
        store.interrupt()
        async with httpx.AsyncClient(timeout=30, transport=transport) as client:
            app.state.store = store
            app.state.pipeline = Pipeline(settings, store, client)
            app.state.client = client
            app.state.price_lock = asyncio.Lock()
            yield
            await app.state.pipeline.shutdown()

    app = FastAPI(title="Market Brief", lifespan=lifespan)

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urlsplit(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "Cross-origin mutations are disabled"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'"
        return response

    def stored_run(identity):
        run = app.state.store.get("runs", identity)
        if not run:
            raise HTTPException(404, "Run not found")
        return run

    def editable(run):
        if run.get("readonly"):
            raise HTTPException(409, "Recorded run is read-only")
        if run["status"] in ACTIVE or run.get("busy"):
            raise HTTPException(409, "Wait for execution to finish")

    def stored_source(run, identity):
        source = next((s for s in run["sources"] if s["id"] == identity), None)
        if source is None:
            raise HTTPException(404, "Source not found in this run")
        return source

    def current_prices():
        companies = {company["ticker"]: company for company in WATCHLIST}
        latest = {}
        company_calls = {}
        for quote in app.state.store.list("quotes"):
            if quote.get("origin") == "imported":
                continue
            latest.setdefault(quote["ticker"], quote)
            if quote.get("valid") and quote["ticker"] not in companies:
                try:
                    company = company_from_call(quote.get("company_call"), quote["ticker"])
                except (ValueError, TypeError):
                    continue
                if company == quote.get("company"):
                    companies[quote["ticker"]] = company
                    company_calls[quote["ticker"]] = quote["company_call"]
        return list(companies.values()), [q for t, q in latest.items() if t in companies], company_calls

    @app.get("/api/config")
    async def config():
        companies, quotes, _ = current_prices()
        return dict(watchlist=companies, credentials=dict(prices=bool(settings.fin_data), research=bool(settings.exa_key and settings.jev_key)),
                    model=settings.jev_model, quotes=quotes, judge_available=bool(settings.jev_key), sample="Pending genuine capture")

    @app.post("/api/tickers")
    async def add_ticker(body: TickerRequest):
        if not settings.fin_data:
            raise HTTPException(409, "Missing fin_data credential")
        if app.state.price_lock.locked():
            raise HTTPException(409, "Price refresh already running")
        async with app.state.price_lock:
            call = await call_api(app.state.client, "GET", "https://api.financialdatasets.ai/company/facts",
                                  settings.fin_data, "Financial Datasets", params={"ticker": body.ticker})
            try:
                company = company_from_call(call, body.ticker)
            except ValueError as exc:
                raise HTTPException(422, call.get("error") or str(exc)) from exc
            company = next((c for c in WATCHLIST if c["ticker"] == body.ticker), company)
            quote = (await check_prices(settings, app.state.client, [company]))[0]
            quote["company_call"] = call
            quote = app.state.store.save("quotes", quote)
            return {"quote": quote, "company": quote["company"]}

    @app.post("/api/check-prices")
    async def prices():
        if not settings.fin_data:
            raise HTTPException(409, "Missing fin_data credential")
        if app.state.price_lock.locked():
            raise HTTPException(409, "Price refresh already running")
        async with app.state.price_lock:
            companies, _, company_calls = current_prices()
            quotes = await check_prices(settings, app.state.client, companies)
            for quote in quotes:
                if quote["ticker"] in company_calls:
                    quote["company_call"] = company_calls[quote["ticker"]]
            return {"quotes": [app.state.store.save("quotes", q) for q in quotes]}

    @app.post("/api/runs")
    async def create(body: RunRequest):
        quote = app.state.store.get("quotes", body.quote_id)
        if not quote or not quote.get("valid") or quote.get("origin") == "imported":
            raise HTTPException(422, "Research requires a validated stored quote")
        if not settings.exa_key or not settings.jev_key:
            raise HTTPException(409, "Research requires exa_key and jev_key")
        return display_run(redact(app.state.pipeline.create(quote), settings.secrets))

    @app.get("/api/runs")
    async def runs():
        return {"runs": [{key: run.get(key) for key in ("id", "company", "created_at", "status", "readonly")} for run in app.state.store.list("runs")]}

    @app.get("/api/runs/{identity}")
    async def get_run(identity: str, replay: bool = False):
        run = stored_run(identity)
        if replay:
            # Replay is a view, not a mutation of a potentially active original run.
            run["readonly"] = True
            run["replay"] = True
        return display_run(run)

    @app.post("/api/runs/{identity}/sources/{source_id}/review")
    async def review(identity: str, source_id: str, body: ReviewRequest):
        async with app.state.pipeline.lock(identity):
            run = stored_run(identity)
            editable(run)
            source = stored_source(run, source_id)
            source["review"] = dict(decision=body.decision, note=body.note, reviewed_at=now())
            source.setdefault("review_history", []).append(source["review"])
            return display_run(app.state.pipeline.rebuild(run))

    @app.post("/api/runs/{identity}/reclassify")
    async def reclassify(identity: str):
        async with app.state.pipeline.lock(identity):
            run = stored_run(identity)
            if run["status"] in ACTIVE or run.get("busy"):
                raise HTTPException(409, "Wait for execution to finish")
            if not settings.jev_key:
                raise HTTPException(409, "Reclassification requires jev_key")
            if not run.get("sources"):
                raise HTTPException(409, "This run has no saved sources to classify")
            return display_run(redact(app.state.pipeline.reclassify(run), settings.secrets))

    @app.post("/api/runs/{identity}/sources/{source_id}/expand")
    async def expand(identity: str, source_id: str):
        async with app.state.pipeline.lock(identity):
            run = stored_run(identity)
            editable(run)
            if not settings.exa_key or not settings.jev_key:
                raise HTTPException(409, "Expansion requires exa_key and jev_key")
            source = stored_source(run, source_id)
            try:
                return display_run(redact(await app.state.pipeline.expand(run, source), settings.secrets))
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs/{identity}/export")
    async def export(identity: str, format: str = "json"):
        run = stored_run(identity)
        if run["status"] in ACTIVE or run.get("busy"):
            raise HTTPException(409, "Wait for execution before exporting")
        run["briefing"] = build_briefing(run)
        if format == "json":
            return JSONResponse(redact(run, settings.secrets), headers={"Content-Disposition": f'attachment; filename="{identity}.json"'})
        if format == "md":
            return PlainTextResponse(markdown(run), media_type="text/markdown", headers={"Content-Disposition": f'attachment; filename="{identity}.md"'})
        raise HTTPException(422, "Use format=md or format=json")

    @app.get("/")
    async def index():
        return FileResponse(ROOT / "app/static/index.html")

    app.mount("/static", StaticFiles(directory=ROOT / "app/static", check_dir=False), name="static")
    return app


app = create_app()
