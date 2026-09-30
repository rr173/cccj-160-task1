"""Service facade wiring store, provider and domain services together."""
from __future__ import annotations

from pathlib import Path

from .evaluation import EvaluationService
from .governance import Governance
from .models import ModelProvider
from .replay import ReplayService
from .serving import Serving
from .store import Database


class Service:
    def __init__(self, db_path: str = ":memory:"):
        self.db = Database(db_path)
        self.provider = ModelProvider()
        self.gov = Governance(self.db, self.provider)
        self.evals = EvaluationService(self.db, self.provider, self.gov)
        self.serving = Serving(self.db, self.provider, self.gov)
        self.replay = ReplayService(self.db, self.provider, self.gov)

    def close(self) -> None:
        self.db.close()


def default_db_path() -> str:
    return str(Path(__file__).resolve().parent.parent / "data" / "governance.db")
