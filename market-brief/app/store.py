import json
import math
import re
import sqlite3
from pathlib import Path

from .models import now

ACTIVE = {"queued", "searching", "judging"}


def redact(value, secrets=()):
    if isinstance(value, dict):
        return {key: ("[REDACTED]" if re.sub(r"[-_]", "", key.lower()) in
                     {"authorization", "xapikey", "apikey", "findata", "exakey", "jevkey"}
                     else redact(item, secrets)) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "[REDACTED]")
        return re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    return value


class Store:
    def __init__(self, root: Path, secrets=()):
        self.root, self.secrets = Path(root), secrets
        self.root.mkdir(parents=True, exist_ok=True)
        for directory in ("quotes", "runs"):
            (self.root / directory).mkdir(exist_ok=True)
        self.db = self.root / "market-brief.sqlite3"
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS quotes (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, status TEXT NOT NULL,
                    fingerprint TEXT UNIQUE, payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS run_status ON runs(status);
            """)

    def connect(self):
        return sqlite3.connect(self.db)

    def save(self, kind, value, fingerprint=None):
        assert kind in {"quotes", "runs"}
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value["id"]):
            raise ValueError("Unsafe artifact ID")
        clean = redact(value, self.secrets)
        payload = json.dumps(clean, ensure_ascii=False, allow_nan=False, indent=2)
        with self.connect() as db:
            if kind == "quotes":
                db.execute("INSERT INTO quotes VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                           (clean["id"], clean["retrieved_at"], payload))
            else:
                db.execute("INSERT INTO runs VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload",
                           (clean["id"], clean["created_at"], clean["status"], fingerprint, payload))
        directory = self.root / kind / clean["id"]
        directory.mkdir(exist_ok=True)
        temporary = directory / "payload.tmp"
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(directory / ("quote.json" if kind == "quotes" else "run.json"))
        return clean

    def get(self, kind, identity):
        assert kind in {"quotes", "runs"}
        with self.connect() as db:
            row = db.execute(f"SELECT payload FROM {kind} WHERE id=?", (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def list(self, kind):
        assert kind in {"quotes", "runs"}
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute(f"SELECT payload FROM {kind} ORDER BY created_at DESC")]

    def find_run(self, fingerprint):
        with self.connect() as db:
            row = db.execute("SELECT payload FROM runs WHERE fingerprint=?", (fingerprint,)).fetchone()
        return json.loads(row[0]) if row else None

    def interrupt(self):
        for run in self.list("runs"):
            if run["status"] in ACTIVE or run.get("busy"):
                run.update(status="interrupted", stage="Interrupted by process restart", busy=False, updated_at=now())
                run["warnings"].append("Execution interrupted. Saved evidence is preserved; no automatic provider retry.")
                self.save("runs", run)
