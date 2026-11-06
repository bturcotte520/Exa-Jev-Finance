import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
WATCHLIST = [
    dict(ticker="NVDA", company="NVIDIA", sector="Semiconductors and AI computing", context="AI infrastructure spending, chip supply, export policy", currency="USD"),
    dict(ticker="AMD", company="Advanced Micro Devices", sector="Semiconductors", context="Data-center demand, PC demand, chip supply", currency="USD"),
    dict(ticker="MSFT", company="Microsoft", sector="Cloud and enterprise software", context="Cloud demand, software spending, AI infrastructure", currency="USD"),
]


@dataclass
class Settings:
    fin_data: str = ""
    exa_key: str = ""
    jev_key: str = ""
    jev_model: str = "jev-latest"
    data_dir: Path = field(default_factory=lambda: ROOT / "data")

    @classmethod
    def load(cls):
        # The original parent .env remains in place; a project-local file may override it.
        values = {**dotenv_values(ROOT.parent / ".env"), **dotenv_values(ROOT / ".env"), **os.environ}
        return cls(**{key: values.get(key) or "" for key in ("fin_data", "exa_key", "jev_key")},
                   jev_model=values.get("JEV_MODEL") or "jev-latest")

    @property
    def secrets(self):
        return [value for value in (self.fin_data, self.exa_key, self.jev_key) if value]
