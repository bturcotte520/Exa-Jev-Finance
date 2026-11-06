"""Financial Datasets snapshots, with validation separate from retrieval."""

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .config import WATCHLIST
from .models import TickerRequest
from .provider import call_api, finite_number, parse_time, utc_now


def validate_company(response, ticker):
    if TickerRequest(ticker=ticker).ticker != ticker:
        raise ValueError("Company ticker must be normalized")
    facts = response.get("company_facts") if isinstance(response, dict) else None
    if not isinstance(facts, dict) or facts.get("ticker") != ticker:
        raise ValueError("Company facts ticker does not match requested ticker")
    name = facts.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Company facts did not report a company name")
    if facts.get("is_active") is False:
        raise ValueError("Company is inactive")
    labels = {key: value.strip() if isinstance(value := facts.get(key), str) and value.strip()
              else "Not reported" for key in ("sector", "industry", "exchange")}
    context = ", ".join(labels[key] for key in ("sector", "industry") if labels[key] != "Not reported")
    return dict(ticker=ticker, company=name.strip(), **labels,
                context=context or "Company announcements, earnings, guidance, and filings",
                currency="USD")  # This application uses the US snapshot endpoint, not global currency inference.


def company_from_call(call, ticker):
    if not isinstance(call, dict) or call.get("error"):
        raise ValueError("Company facts request failed or is missing")
    request = call.get("request", {})
    if (not isinstance(request, dict) or request.get("method") != "GET" or
            request.get("url") != "https://api.financialdatasets.ai/company/facts" or
            request.get("params") != {"ticker": ticker}):
        raise ValueError("Company facts request identity does not match ticker")
    return validate_company(call.get("response"), ticker)


def validate_snapshot(response, ticker, retrieved_at, company=None):
    snapshot = response.get("snapshot") if isinstance(response, dict) else None
    if not isinstance(snapshot, dict):
        raise ValueError("Missing snapshot")
    if snapshot.get("ticker") != ticker:
        raise ValueError("Snapshot ticker does not match requested ticker")
    company = company or next((item for item in WATCHLIST if item["ticker"] == ticker), None)
    if company is None or company.get("ticker") != ticker or company.get("currency") != "USD":
        raise ValueError("Ticker is not configured")
    for name in ("price", "day_change", "day_change_percent"):
        if not finite_number(snapshot.get(name)):
            raise ValueError(f"Invalid {name}: expected finite number")
    if snapshot["price"] <= 0:
        raise ValueError("Price must be positive")
    try:
        stamp = snapshot.get("time")
        # The provider explicitly documents the human-readable timestamp as UTC.
        if isinstance(stamp, str):
            if len(stamp) <= 10:
                raise ValueError("Quote timestamp must include a time")
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            quote_time = parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
        elif stamp is not None:
            raise ValueError("Invalid quote timestamp")
        else:
            quote_time = None
        milliseconds = snapshot.get("time_milliseconds")
        if milliseconds is not None:
            if not finite_number(milliseconds):
                raise ValueError("Invalid millisecond timestamp")
            epoch_time = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
            if quote_time and abs((quote_time - epoch_time).total_seconds()) >= 1:
                raise ValueError("Provider timestamps disagree")
            quote_time = epoch_time
        if quote_time is None:
            raise ValueError("Missing quote timestamp")
        if quote_time > parse_time(retrieved_at) + timedelta(minutes=5):
            raise ValueError("Quote timestamp is in the future")
    except (TypeError, OverflowError, OSError) as exc:
        raise ValueError("Invalid quote timestamp") from exc
    return {**{name: snapshot[name] for name in ("price", "ticker", "day_change", "day_change_percent")},
            "quote_time": quote_time.isoformat(), "prior_date": quote_time.date() < parse_time(retrieved_at).date(),
            "currency": company["currency"], "currency_source": "configuration", "provider": "Financial Datasets"}


async def check_prices(settings, client, companies=None):
    async def fetch(company):
        ticker = company["ticker"]
        call = await call_api(client, "GET", "https://api.financialdatasets.ai/prices/snapshot",
                              settings.fin_data, "Financial Datasets", params={"ticker": ticker})
        retrieved_at = utc_now()
        fields = {"price": None, "ticker": ticker, "day_change": None,
                  "day_change_percent": None, "quote_time": None, "prior_date": False,
                  "currency": company["currency"], "currency_source": "configuration",
                  "provider": "Financial Datasets"}
        error = call["error"]
        if not error:
            try:
                fields = validate_snapshot(call["response"], ticker, retrieved_at, company)
            except (ValueError, TypeError, OverflowError, OSError) as exc:
                error = str(exc)
        return {"id": str(uuid4()), "ticker": ticker, "company": dict(company),
                "retrieved_at": retrieved_at, "valid": error is None, "error": error,
                "fields": fields, "call": call}

    companies = WATCHLIST if companies is None else companies
    unique = {company["ticker"]: company for company in companies}
    return list(await asyncio.gather(*(fetch(company) for company in unique.values())))
