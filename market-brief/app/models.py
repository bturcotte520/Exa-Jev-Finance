from datetime import datetime, timezone
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def now():
    return datetime.now(timezone.utc).isoformat()


class TickerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(strict=True, min_length=1, max_length=12)

    @field_validator("ticker", mode="before")
    @classmethod
    def normalize_ticker(cls, value):
        if not isinstance(value, str) or not value.isascii():
            raise ValueError("Ticker must be a string")
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z][A-Z0-9]*(?:[.-][A-Z0-9]+)*", value, flags=re.ASCII):
            raise ValueError("Use a ticker with letters, digits, and internal dots or hyphens")
        return value


class RunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    quote_id: str = Field(min_length=1, max_length=100)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["keep", "out"]
    note: str = Field(min_length=1, max_length=2000)

    @field_validator("note")
    @classmethod
    def meaningful_note(cls, value):
        if not value.strip():
            raise ValueError("A review note is required")
        return value.strip()


class RecordedRun(BaseModel):
    model_config = ConfigDict(extra="allow")
    schema_version: Literal[1]
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    created_at: datetime
    cutoff: datetime
    window_start: datetime
    trigger_mode: Literal["manual"]
    status: Literal["complete", "partial", "failed", "no_sources", "interrupted"]
    company: dict
    quote: dict
    queries: list[dict]
    sources: list[dict]
    briefing: dict
    warnings: list[str]

    @field_validator("created_at", "cutoff", "window_start")
    @classmethod
    def timezone_required(cls, value):
        if value.tzinfo is None:
            raise ValueError("Recorded timestamps must include timezone")
        return value
