"""Separate local Phase 2 service; Phase 1 and Render configuration are unchanged."""
import os
from pathlib import Path

from .main import create_app
from .phase2 import Phase2Engine


def create_phase2_app(**kwargs):
    path = kwargs.pop("db_path", None)
    if path is None:
        path = os.environ.get("PHASE2_DEMO_DB_PATH", "backend/data/phase2.sqlite3")
    phase1 = Path(os.environ.get("DEMO_DB_PATH", "backend/data/demo.sqlite3"))
    if Path(path).resolve() == phase1.resolve():
        raise ValueError("Phase 2 must use its own demo database")
    # No deployment target is enabled by this local entry point.
    if os.environ.get("RENDER"):
        raise RuntimeError("Phase 2 is local-only pending a separate deployment review")
    return create_app(db_path=path, engine_factory=Phase2Engine, **kwargs)


app = create_phase2_app()
