"""Deterministic routing and a sourced draft. No synthesis model."""

from datetime import datetime, timezone

from .research import usable_passages


DISCLAIMER = "These are reported developments around the observed move. They do not establish what caused it."


def route_source(source):
    dates = source.get("date_check", {})
    judgment = source.get("judgment") or {}
    answers = judgment.get("answers") or {}
    scope_answer = answers.get("scope") if isinstance(answers, dict) else None
    substance_answer = answers.get("substance") if isinstance(answers, dict) else None
    scope = scope_answer.get("choice") if isinstance(scope_answer, dict) else None
    substance = substance_answer.get("choice") if isinstance(substance_answer, dict) else None
    if dates.get("outside_window"):
        automated, reason = "out", "Publication is outside the research window"
    elif not usable_passages(source.get("passages")):
        automated, reason = "review", "No usable passages"
    elif dates.get("uncertain", True):
        automated, reason = "review", "Publication date or timing is uncertain"
    elif not judgment.get("valid"):
        automated, reason = "review", "Judgment failed or is invalid"
    elif scope == "unclear" or substance == "insufficient":
        automated, reason = "review", "Evidence does not establish relevance or substance"
    elif scope == "unrelated" or substance == "price_recap_only":
        automated, reason = "out", "Unrelated evidence or price recap only"
    elif substance == "commentary":
        automated, reason = "review", "Commentary requires explicit review"
    elif scope == "company" and substance == "concrete_development":
        automated, reason = "company", "Direct company development"
    elif scope in {"business_connection", "sector_macro"} and substance == "concrete_development":
        automated, reason = "context", "Concrete broader business context"
    else:
        automated, reason = "review", "Unclassified evidence"
    final = automated
    review = source.get("review")
    if review and not dates.get("outside_window"):
        if review.get("decision") == "out":
            final = "out"
        elif review.get("decision") == "keep":
            # Unknown or failed classifications cannot silently become factual developments.
            final = "commentary" if substance != "concrete_development" or not judgment.get("valid") else "company" if scope == "company" else "context"
    return {"automated": automated, "final": final, "reason": reason, "review": review,
            "scope": scope, "substance": substance, "date_check": dates,
            "timing": dates.get("timing", "uncertain"),
            "uncertain": dates.get("uncertain", True),
            "commentary": substance == "commentary" or final == "commentary"}


def build_briefing(run):
    grouped = {"company": [], "context": [], "commentary": []}
    unresolved = []
    for source in run.get("sources", []):
        destination = route_source(source)["final"]
        if destination in grouped:
            grouped[destination].append(source)
        elif destination == "review":
            unresolved.append(source["id"])

    def newest(source):
        try:
            stamp = datetime.fromisoformat(source["published_date"].replace("Z", "+00:00"))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            return -stamp.timestamp()
        except (KeyError, AttributeError, TypeError, ValueError, OSError):
            return float("inf")

    for sources in grouped.values():
        sources.sort(key=newest)
    return {"sections": {section: [source["id"] for source in sources] for section, sources in grouped.items()},
            "unresolved": unresolved, "disclaimer": DISCLAIMER}
