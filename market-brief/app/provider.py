"""Small auditable HTTP boundary; credentials never enter call artifacts."""

import asyncio
import math
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def parse_time(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Missing or invalid timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


async def call_api(client, method, url, key, provider, body=None, params=None):
    call = {"request": {"method": method, "url": url, "body": body, "params": params},
            "response": None, "attempts": [], "timing_ms": 0, "error": None,
            "usage": None, "cost": {"label": "Not reported", "value": None}}
    if not key:
        call["error"] = f"Missing {provider} credential"
        return call
    headers = {"Authorization": f"Bearer {key}"} if provider == "Jev" else {"X-API-KEY": key}
    started = time.perf_counter()
    for attempt in range(3):
        tick = time.perf_counter()
        delay = 2 ** attempt
        retry = False
        status = None
        try:
            response = await client.request(method, url, headers=headers, json=body,
                                            params=params, timeout=30.0)
            status = response.status_code
            try:
                call["response"] = response.json()
                json_valid = True
            except ValueError:
                call["response"] = {"text": response.text}
                json_valid = False
            call["error"] = None if response.is_success else f"{provider} HTTP {status}"
            if response.is_success and not json_valid:
                call["error"] = f"{provider} returned invalid JSON"
            if response.is_success and not isinstance(call["response"], dict):
                call["error"] = f"{provider} returned a non-object response"
            if response.is_success and isinstance(call["response"], dict):
                call["usage"] = call["response"].get("usage")
                cost = call["response"].get("costDollars")
                if cost is not None:
                    call["cost"] = {"label": "Reported", "value": cost, "currency": "USD"}
            retry = status == 429 or status in {500, 502, 503, 504, 529}
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        delay = (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        delay = 2 ** attempt
                delay = min(30, max(0, delay)) if math.isfinite(delay) else 2 ** attempt
        except httpx.TransportError as exc:
            call["error"] = f"{provider} transport failure: {type(exc).__name__}"
            call["response"] = None
            retry = True
        call["attempts"].append({"number": attempt + 1, "status": status,
                                 "timing_ms": round((time.perf_counter() - tick) * 1000, 2),
                                 "error": call["error"]})
        if not retry or attempt == 2:
            break
        await asyncio.sleep(delay)
    call["timing_ms"] = round((time.perf_counter() - started) * 1000, 2)

    def sanitize(value):
        if isinstance(value, str):
            return value.replace(key, "[REDACTED]")
        if isinstance(value, dict):
            return {sanitize(k): sanitize(v) for k, v in value.items()}
        if isinstance(value, list):
            return [sanitize(v) for v in value]
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value

    return sanitize(call)
