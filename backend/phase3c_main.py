"""Local Phase 3C companion. /signal remains the untouched Phase 3B champion."""
from datetime import datetime,timezone
from pathlib import Path
import os

from fastapi import HTTPException
from .main import create_app
from .feed import TwelveDataFeed
from .intelligence import IntelligencePolicy
from .intelligence_providers import IntelligenceFeed,IntelligenceMarketFeed
from .hidden_state import HiddenPolicy
from .slow_context import SlowPolicy,SlowFeed,Phase3BFeed
from .meta_council import MetaPolicy,DemoCosts
from .phase3c import Phase3CEngine


def create_phase3c_app(db_path=None,providers=None,slow_providers=None,market_feed=None,
                      intelligence_policy=IntelligencePolicy(),hidden_policy=HiddenPolicy(),slow_policy=SlowPolicy(),
                      meta_policy=MetaPolicy(),costs=DemoCosts(),clock=lambda:datetime.now(timezone.utc),**kwargs):
    if os.environ.get("RENDER"):
        raise RuntimeError("Phase 3C is local-only; deployment is not enabled")
    path=Path(db_path or os.environ.get("PHASE3C_DEMO_DB_PATH","backend/data/phase3c.sqlite3"))
    for key,default in (("DEMO_DB_PATH","backend/data/demo.sqlite3"),("PHASE2_DEMO_DB_PATH","backend/data/phase2.sqlite3"),
                        ("PHASE3A_DEMO_DB_PATH","backend/data/phase3a.sqlite3"),("PHASE3B_DEMO_DB_PATH","backend/data/phase3b.sqlite3")):
        if path.resolve()==Path(os.environ.get(key,default)).resolve():
            raise ValueError("Phase 3C must use its own database")

    class Configured(Phase3CEngine):
        def __init__(self,store,policy):
            super().__init__(store,policy,intelligence_policy=intelligence_policy,hidden_policy=hidden_policy,
                             slow_policy=slow_policy,meta_policy=meta_policy,costs=costs)

    market=IntelligenceMarketFeed(market_feed or TwelveDataFeed(os.environ.get("TWELVE_DATA_API_KEY","")),IntelligenceFeed(providers,clock))
    app=create_app(db_path=path,feed=Phase3BFeed(market,SlowFeed(slow_providers,clock)),clock=clock,engine_factory=Configured,**kwargs)

    @app.get("/meta/decision")
    def decision():
        return app.state.engine.latest_meta(clock())

    @app.get("/meta/analytics")
    def analytics():
        return app.state.engine.meta_analytics(clock())

    @app.get("/meta/promotion")
    def promotion():
        return app.state.engine.meta_analytics(clock())["promotion"]

    @app.get("/meta/replay/{decision_id}")
    def replay(decision_id:str):
        try:
            return app.state.engine.replay_meta(decision_id)
        except KeyError:
            raise HTTPException(status_code=404,detail="Decision not found")

    return app


app=create_phase3c_app()
