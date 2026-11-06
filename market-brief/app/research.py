"""Three focused searches, original passages, and conservative date checks."""

import asyncio
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from .provider import call_api, parse_time, utc_now


def build_queries(company, window_start, cutoff):
    subject = f"{company['company']} ({company['ticker']})"
    window = f"between {window_start} and {cutoff}"
    texts = [
        f"Recent {subject} announcements, earnings, guidance, filings, or operational developments {window}.",
        f"Customer, supplier, or competitor developments {window} with a concrete connection to {subject}'s business: {company['context']}.",
        f"Sector, policy, supply, demand, or macroeconomic developments {window} relevant to {subject}, {company['sector']}: {company['context']}.",
    ]
    return [{"id": f"Q{i}", "request": {"query": text, "type": "auto", "numResults": 4,
             "startPublishedDate": window_start, "endPublishedDate": cutoff,
             "contents": {"highlights": True}}} for i, text in enumerate(texts, 1)]


def normalize_url(url):
    if not isinstance(url, str):
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
            return None
        tracking = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"}
        query = "&".join(part for part in parts.query.split("&")
                         if not (unquote_plus(part.split("=", 1)[0]).lower().startswith("utm_")
                                 or unquote_plus(part.split("=", 1)[0]).lower() in tracking))
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, query, ""))
    except ValueError:
        return None


def check_date(published, run):
    result = {"outside_window": False, "uncertain": True, "timing": "uncertain",
              "precision": "unknown", "note": "Publication date missing or ambiguous; fetched pages are not historical snapshots."}
    if not isinstance(published, str) or not published:
        return result
    try:
        stamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
        date_only = len(published) == 10 or stamp.time().replace(tzinfo=None) == datetime.min.time()
        if stamp.tzinfo is None and not date_only:
            return result
        stamp = stamp.replace(tzinfo=timezone.utc) if stamp.tzinfo is None else stamp.astimezone(timezone.utc)
        low = stamp
        high = stamp + timedelta(days=1) if date_only else stamp
        start, end = parse_time(run["window_start"]), parse_time(run["cutoff"])
        result["outside_window"] = (high <= start if date_only else high < start) or low > end
        result["precision"] = "day" if date_only else "timestamp"
        result["uncertain"] = date_only
        result["note"] = ("Date-only or normalized midnight; exact publication time is uncertain. "
                          if date_only else "Reported publication timestamp. ") + "Fetched pages are not historical snapshots."
        quote = parse_time(run["quote"]["fields"]["quote_time"])
        if (high <= quote if date_only else high < quote):
            result["timing"] = "before_quote"
        elif low > quote:
            result["timing"] = "after_quote"
        elif not date_only:
            result["timing"] = "at_quote"
        return result
    except (ValueError, TypeError, OverflowError):
        return result


def usable_passages(value):
    return [text for text in value if isinstance(text, str) and text.strip()] if isinstance(value, list) else []


async def search_sources(settings, client, run):
    queries = build_queries(run["company"], run["window_start"], run["cutoff"])

    async def search(query):
        query["call"] = await call_api(client, "POST", "https://api.exa.ai/search", settings.exa_key,
                                       "Exa", body=query["request"])
        query["retrieved_at"] = utc_now()
        response = query["call"]["response"]
        if not query["call"]["error"] and not isinstance(response.get("results"), list):
            query["call"]["error"] = "Exa returned no results array"

    await asyncio.gather(*(search(query) for query in queries))
    sources, by_url, warnings = [], {}, []
    failures = 0
    for query in queries:
        if query["call"]["error"]:
            failures += 1
            warnings.append(f"{query['id']}: {query['call']['error']}")
            continue
        for index, item in enumerate(query["call"]["response"]["results"]):
            if not isinstance(item, dict) or not (url := normalize_url(item.get("url"))):
                warnings.append(f"{query['id']} result {index}: invalid source URL")
                continue
            passages = usable_passages(item.get("highlights"))
            ref = {"query_id": query["id"], "result_index": index}
            if url in by_url:
                source = by_url[url]
                if item["url"] not in source["original_urls"]:
                    source["original_urls"].append(item["url"])
                if query["id"] not in source["query_ids"]:
                    source["query_ids"].append(query["id"])
                source["raw_refs"].append(ref)
                for passage in passages:
                    if passage not in source["original_highlights"]:
                        source["original_highlights"].append(passage)
                        source["passages"].append(passage)
                if item.get("publishedDate") != source["published_date"]:
                    source["date_check"]["uncertain"] = True
                    source["date_check"]["note"] += " Duplicate results disagree on publication date."
                continue
            if len(sources) >= 12:
                continue
            date_check = check_date(item.get("publishedDate"), run)
            source = {"id": f"S{len(sources) + 1}", "title": item.get("title") or url,
                      "url": url, "domain": urlsplit(url).hostname, "original_urls": [item["url"]],
                      "query_ids": [query["id"]], "raw_refs": [ref], "published_date": item.get("publishedDate"),
                      "publication_precision": date_check["precision"], "retrieved_at": query["retrieved_at"],
                      "original_highlights": list(passages), "passages": list(passages), "date_check": date_check}
            sources.append(source)
            by_url[url] = source
    status = "failed" if failures == 3 else "partial" if failures else "complete" if sources else "no_sources"
    return {"queries": queries, "sources": sources, "warnings": warnings, "status": status}


async def expand_source(settings, client, run, source):
    body = {"ids": [source["url"]], "highlights": {"query":
            f"What concrete development does this page report, when, and what is its connection to {run['company']['company']} ({run['company']['ticker']}) and {run['company']['context']}?"}}
    call = await call_api(client, "POST", "https://api.exa.ai/contents", settings.exa_key, "Exa", body=body)
    error, passages = call["error"], []
    if not error:
        response = call["response"]
        statuses = response.get("statuses", [])
        matching = [entry for entry in statuses if isinstance(entry, dict) and normalize_url(entry.get("id")) == source["url"]] if isinstance(statuses, list) else []
        if not matching or any(entry.get("status") != "success" for entry in matching):
            error = "Exa Contents did not report success for this URL"
        else:
            results = response.get("results", [])
            for item in results if isinstance(results, list) else []:
                if isinstance(item, dict) and normalize_url(item.get("url") or item.get("id")) == source["url"]:
                    passages.extend(usable_passages(item.get("highlights")))
            if not passages:
                error = "Exa Contents returned no usable passages"
    return {"success": error is None, "call": call, "passages": passages if not error else [],
            "error": error, "retrieved_at": utc_now()}
