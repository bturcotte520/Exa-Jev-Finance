import asyncio
import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from .briefing import build_briefing, route_source
from .judge import JUDGE_VERSION, judge_source
from .models import now
from .research import expand_source, search_sources
from .store import ACTIVE


class Pipeline:
    def __init__(self, settings, store, client):
        self.settings, self.store, self.client = settings, store, client
        self.tasks = set()
        self.judges = asyncio.Semaphore(3)
        self.locks = {}

    def lock(self, identity):
        return self.locks.setdefault(identity, asyncio.Lock())

    def create(self, quote):
        settings = {"jev_model": self.settings.jev_model, "window_hours": 72, "rules_version": 1, "judge_version": JUDGE_VERSION}
        fingerprint = hashlib.sha256(json.dumps([quote["id"], settings], sort_keys=True).encode()).hexdigest()
        existing = self.store.find_run(fingerprint)
        if existing:
            return existing
        cutoff = datetime.now(timezone.utc)
        run = dict(schema_version=1, id=str(uuid4()), created_at=cutoff.isoformat(), updated_at=cutoff.isoformat(),
                   cutoff=cutoff.isoformat(), window_start=(cutoff - timedelta(hours=72)).isoformat(),
                   company=quote["company"], quote=quote, settings=settings, trigger_mode="manual",
                   status="queued", stage="Waiting to search", readonly=False, busy=False,
                   queries=[], sources=[], warnings=[], expansions=[], briefing={})
        self.store.save("runs", run, fingerprint)
        task = asyncio.create_task(self.execute(run))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return run

    def reclassify(self, parent):
        settings = {**parent.get("settings", {}), "jev_model": self.settings.jev_model, "judge_version": JUDGE_VERSION}
        fingerprint = hashlib.sha256(json.dumps(["reclassify", parent["id"], settings,
            [(s["id"], s.get("passages", []), s.get("date_check", {})) for s in parent["sources"]]], sort_keys=True).encode()).hexdigest()
        existing = self.store.find_run(fingerprint)
        if existing:
            return existing
        run = deepcopy(parent)
        run.update(id=str(uuid4()), created_at=now(), updated_at=now(), settings=settings,
                   parent_run_id=parent["id"], kind="reclassification", readonly=False, imported=False,
                   status="queued", stage="Waiting to reclassify saved evidence", busy=False,
                   briefing={}, warnings=["Uses the original quote, research window, and evidence. No new prices or searches. Prior reviews are not applied to this new classification.", *parent.get("warnings", [])])
        run.pop("replay", None)
        for source in run["sources"]:
            source["previous_judgment"] = deepcopy(source.get("judgment"))
            source["previous_review"] = deepcopy(source.get("review"))
            for key in ("judgment", "route", "review", "review_history", "passages_html"):
                source.pop(key, None)
        self.store.save("runs", run, fingerprint)
        task = asyncio.create_task(self.classify_saved(run))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return run

    async def classify_saved(self, run):
        try:
            run.update(status="judging", stage="Jev is reclassifying saved evidence")
            self.save(run)

            async def classify(source):
                await self.judge(run, source)
                self.rebuild(run)

            await asyncio.gather(*(classify(source) for source in run["sources"]))
            invalid = sum(not source["judgment"]["valid"] for source in run["sources"])
            if invalid:
                run["warnings"].append(f"{invalid} source(s) could not be classified; review required.")
            incomplete_search = any(query.get("call", {}).get("error") for query in run.get("queries", []))
            run.update(status="partial" if invalid or incomplete_search else "complete", stage="Reclassification ready")
            self.rebuild(run)
        except asyncio.CancelledError:
            run.update(status="interrupted", stage="Reclassification interrupted")
            self.save(run)
            raise
        except Exception:
            run.update(status="failed", stage="Reclassification failed")
            run["warnings"].append("Reclassification failed; original run and evidence are unchanged.")
            self.rebuild(run)

    def save(self, run):
        run["updated_at"] = now()
        return self.store.save("runs", run)

    def rebuild(self, run):
        for source in run["sources"]:
            source["route"] = route_source(source)
        run["briefing"] = build_briefing(run)
        return self.save(run)

    async def judge(self, run, source):
        async with self.judges:
            source["judgment"] = await judge_source(self.settings, self.client, run, source)
            source.setdefault("judgment_history", []).append(source["judgment"])

    async def execute(self, run):
        try:
            run.update(status="searching", stage="Searching three Exa queries")
            self.save(run)
            result = await search_sources(self.settings, self.client, run)
            run.update(queries=result["queries"], sources=result["sources"], warnings=result["warnings"])
            if result["status"] in {"failed", "no_sources"}:
                run.update(status=result["status"], stage="Search failed" if result["status"] == "failed" else "No sources found")
                self.rebuild(run)
                return
            run.update(status="judging", stage=f"Classifying {len(run['sources'])} sources with Jev")
            self.save(run)

            async def classify(source):
                await self.judge(run, source)
                self.rebuild(run)

            await asyncio.gather(*(classify(source) for source in run["sources"]))
            invalid = sum(not source["judgment"]["valid"] for source in run["sources"])
            if invalid:
                run["warnings"].append(f"{invalid} source(s) require review because classification was unavailable or invalid.")
            run.update(status="partial" if result["status"] == "partial" or invalid else "complete", stage="Draft ready")
            self.rebuild(run)
        except asyncio.CancelledError:
            run.update(status="interrupted", stage="Execution interrupted")
            self.save(run)
            raise
        except Exception:
            # Provider responses are retained by adapters; avoid leaking credentials from exception strings.
            run.update(status="failed", stage="Pipeline failed")
            run["warnings"].append("Unexpected pipeline failure; saved artifacts are available for inspection.")
            self.rebuild(run)

    async def expand(self, run, source):
        if len(run.get("expansions", [])) >= 2 or source.get("expansion"):
            raise ValueError("At most two distinct sources may be expanded, once each, per run")
        run["busy"] = True
        run.setdefault("expansions", []).append(source["id"])
        self.save(run)
        try:
            expansion = await expand_source(self.settings, self.client, run, source)
            source["expansion"] = expansion
            if expansion["success"]:
                source["expanded_passages"] = expansion["passages"]
                source["passages"] = list(dict.fromkeys(source["original_highlights"] + expansion["passages"]))
                await self.judge(run, source)
            else:
                run["warnings"].append(f"{source['id']}: expansion failed; last usable evidence retained.")
        finally:
            run["busy"] = False
            self.rebuild(run)
        return run

    async def shutdown(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
