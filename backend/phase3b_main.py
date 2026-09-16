"""Separate local Phase 3B DEMO entry point. No deployment or broker adapter."""
from datetime import datetime, timezone
from pathlib import Path
import json
import os

from fastapi import Query
from .feed import TwelveDataFeed
from .intelligence import IntelligencePolicy
from .intelligence_providers import IntelligenceFeed, IntelligenceMarketFeed
from .hidden_state import HiddenPolicy
from .slow_context import SlowPolicy, SlowFeed, Phase3BFeed
from .main import create_app
from .phase3b import Phase3BEngine
from .storage import Store


def create_phase3b_app(db_path=None,providers=None,slow_providers=None,market_feed=None,
                      intelligence_policy=IntelligencePolicy(),hidden_policy=HiddenPolicy(),slow_policy=SlowPolicy(),
                      clock=lambda:datetime.now(timezone.utc),**kwargs):
    path = Path(db_path or os.environ.get("PHASE3B_DEMO_DB_PATH","backend/data/phase3b.sqlite3"))
    protected = [Path(os.environ.get(key,default)).resolve() for key,default in
                 (("DEMO_DB_PATH","backend/data/demo.sqlite3"),("PHASE2_DEMO_DB_PATH","backend/data/phase2.sqlite3"),
                  ("PHASE3A_DEMO_DB_PATH","backend/data/phase3a.sqlite3"))]
    if path.resolve() in protected:
        raise ValueError("Phase 3B must use its own demo database")
    if os.environ.get("RENDER"):
        raise RuntimeError("Phase 3B is local-only pending separate deployment review")

    class ConfiguredEngine(Phase3BEngine):
        def __init__(self,store,policy):
            super().__init__(store,policy,intelligence_policy=intelligence_policy,
                             hidden_policy=hidden_policy,slow_policy=slow_policy)

    market = IntelligenceMarketFeed(market_feed or TwelveDataFeed(os.environ.get("TWELVE_DATA_API_KEY","")),
                                    IntelligenceFeed(providers,clock))
    app = create_app(db_path=path,feed=Phase3BFeed(market,SlowFeed(slow_providers,clock)),clock=clock,
                     engine_factory=ConfiguredEngine,**kwargs)

    @app.get("/gold/analytics")
    def analytics():
        return app.state.engine.analytics()

    @app.get("/gold/shadows")
    def shadows(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
        with app.state.store.connect() as conn:
            rows = conn.execute("SELECT payload FROM phase3b_shadows ORDER BY rowid DESC LIMIT ? OFFSET ?",(limit,offset)).fetchall()
            health = Store.get_state(conn,"phase3b_shadow_health",{"status":"STARTING"})
        return {"mode":"SHADOW_DEMO_ONLY","health":health,"trades":[json.loads(r[0]) for r in rows]}

    @app.get("/gold/shadow-decisions")
    def shadow_decisions(limit:int=Query(100,ge=1,le=500),offset:int=Query(0,ge=0)):
        with app.state.store.connect() as conn:
            rows = conn.execute("SELECT payload FROM phase3b_shadow_decisions ORDER BY rowid DESC LIMIT ? OFFSET ?",(limit,offset)).fetchall()
        return {"mode":"SHADOW_DEMO_ONLY","decisions":[json.loads(r[0]) for r in rows]}

    return app


app = create_phase3b_app()
