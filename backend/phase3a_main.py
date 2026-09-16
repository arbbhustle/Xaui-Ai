"""Separate local Phase 3A entry point. No provider credentials are bundled."""
from datetime import datetime, timezone
from pathlib import Path
import os

from .feed import TwelveDataFeed
from .intelligence import IntelligencePolicy
from .intelligence_providers import IntelligenceFeed, IntelligenceMarketFeed
from .main import create_app
from .phase3a import Phase3AEngine


def create_phase3a_app(db_path=None, providers=None, market_feed=None,
                      intelligence_policy=IntelligencePolicy(), clock=lambda: datetime.now(timezone.utc), **kwargs):
    path = Path(db_path or os.environ.get("PHASE3A_DEMO_DB_PATH", "backend/data/phase3a.sqlite3"))
    protected = [Path(os.environ.get(key, default)).resolve() for key, default in
                 (("DEMO_DB_PATH", "backend/data/demo.sqlite3"), ("PHASE2_DEMO_DB_PATH", "backend/data/phase2.sqlite3"))]
    if path.resolve() in protected:
        raise ValueError("Phase 3A must use its own demo database")
    if os.environ.get("RENDER"):
        raise RuntimeError("Phase 3A is local-only pending separate deployment review")

    class ConfiguredEngine(Phase3AEngine):
        def __init__(self, store, policy):
            super().__init__(store, policy, intelligence_policy=intelligence_policy)

    feed = IntelligenceMarketFeed(market_feed or TwelveDataFeed(os.environ.get("TWELVE_DATA_API_KEY", "")),
                                  IntelligenceFeed(providers, clock))
    return create_app(db_path=path, feed=feed, clock=clock, engine_factory=ConfiguredEngine, **kwargs)


app = create_phase3a_app()
