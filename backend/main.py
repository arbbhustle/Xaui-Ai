"""Android-compatible read API. Starting the service never enables broker trading."""
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import json
import logging
import os
import sys

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from .domain import Policy, apply_veto, parse, stamp, freshness_errors
from .engine import Engine
from .feed import TwelveDataFeed
from .storage import Store
from .worker import Monitor


def database_path():
    path = Path(os.environ.get("DEMO_DB_PATH", "backend/data/demo.sqlite3"))
    if os.environ.get("RENDER"):
        # Fail rather than silently claiming persistence on Render's ephemeral disk.
        disk = Path(os.environ.get("DEMO_DISK_PATH", "/var/data"))
        if not path.is_absolute() or not disk.is_mount() or not path.resolve().is_relative_to(disk.resolve()):
            raise RuntimeError("Render requires DEMO_DB_PATH on a mounted persistent DEMO_DISK_PATH")
    return path


def create_app(db_path=None, policy=None, feed=None, start_worker=True,
               clock=lambda: datetime.now(timezone.utc), engine_factory=Engine):
    # Database and worker start during lifespan, not at module import.
    policy = policy or Policy(kill_switch=os.environ.get("DEMO_KILL_SWITCH", "false").lower() == "true")

    @asynccontextmanager
    async def lifespan(app):
        store = Store(db_path if db_path is not None else database_path())
        engine = engine_factory(store, policy)
        monitor = Monitor(engine, feed or TwelveDataFeed(os.environ.get("TWELVE_DATA_API_KEY", "")), clock)
        app.state.store, app.state.engine = store, engine
        logger = logging.getLogger("dardania")
        handler = None
        previous_level, previous_propagate = logger.level, logger.propagate
        if not logger.handlers:
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
        try:
            if start_worker:
                monitor.start()
            yield
        finally:
            if start_worker and monitor.thread:
                monitor.stop()
            if handler is not None:
                logger.removeHandler(handler)
                handler.close()
            logger.setLevel(previous_level)
            logger.propagate = previous_propagate

    strategy_version = engine_factory.strategy_version
    app = FastAPI(title=f"DardaniaXAUTRADE AI — {strategy_version} Demo",
                  version=strategy_version, lifespan=lifespan)

    def read_health(conn, now=None):
        now = now or clock()
        health = Store.get_state(conn, "health", {"status": "STARTING", "data_errors": ["WAITING_FOR_DATA"]})
        errors = list(health.get("data_errors", []))
        errors.extend(freshness_errors(health.get("source_candle_closes", {}), now, policy))
        at = health.get("observed_at")
        if not at or not 0 <= (now - parse(at)).total_seconds() <= policy.heartbeat_limit_seconds:
            errors.append("MONITOR_STALE")
        result = dict(health, status="STALE" if errors else health["status"],
                      data_errors=sorted(set(errors)), served_at=stamp(now))
        return app.state.engine.api_health(conn, result, now)

    @app.get("/")
    def root():
        return {"name": app.title, "version": strategy_version, "mode": "DEMO_ONLY", "signal_endpoint": "/signal"}

    @app.get("/health")
    def health():
        try:
            with app.state.store.connect() as conn:
                result = read_health(conn)
            return JSONResponse(result, status_code=200 if result["status"] == "OK" else 503)
        except Exception:
            return JSONResponse({"status": "ERROR", "data_errors": ["DATABASE_UNAVAILABLE"]}, status_code=503)

    @app.get("/live")
    def live():
        # Hosting liveness must not restart the process merely because markets close.
        return {"status": "alive", "mode": "DEMO_ONLY"}

    @app.get("/signal")
    def signal():
        now = clock()
        with app.state.store.connect() as conn:
            conn.execute("BEGIN")  # consistent read of decision, health and demo state
            row = conn.execute("SELECT payload FROM decisions ORDER BY candle_close DESC LIMIT 1").fetchone()
            result = json.loads(row[0]) if row else {
                "direction": "NO_TRADE", "confidence": 0, "buy_score": 0, "sell_score": 0,
                "entry": None, "sl": None, "tp1": None, "tp2": None,
                "action": "WAIT", "mode": "DATA_WAIT", "data_status": "MISSING",
                "timestamp_utc": "", "reasons": ["Waiting for first closed-candle decision"],
            }
            health = read_health(conn, now)
            codes = list(health.get("data_errors", [])) + list(health.get("risk_codes", []))
            decision_errors = freshness_errors(result.get("source_candle_closes", {}), now, policy)
            codes.extend(decision_errors)
            if result.get("expires_at") and now >= parse(result["expires_at"]):
                codes.append("SIGNAL_EXPIRED")
            if result.get("decision_id"):
                trade = conn.execute("SELECT status FROM trades WHERE decision_id=?", (result["decision_id"],)).fetchone()
                if trade and trade[0] in ("CLOSED", "CANCELLED"):
                    codes.append("TRADE_PLAN_FINISHED")
            result = apply_veto(result, codes)
            if health["status"] != "OK":
                result["data_status"] = health["status"]
            elif decision_errors:
                result["data_status"] = "STALE"
            result = app.state.engine.api_decision(conn, result, now)
            result["performance"] = Store.summary(conn)
            result["demo_trade"] = result["performance"]["open_trade"]
            result["health"] = health
            result["served_at"] = stamp(now)
            return result

    @app.get("/performance")
    def performance():
        with app.state.store.connect() as conn:
            conn.execute("BEGIN")
            return dict(Store.summary(conn), health=read_health(conn))

    @app.get("/trades")
    def trades(limit: int = Query(50, ge=1, le=500), before_row: int | None = None):
        with app.state.store.connect() as conn:
            conn.execute("BEGIN")
            rows = conn.execute("SELECT rowid,payload FROM trades WHERE rowid < ? ORDER BY rowid DESC LIMIT ?",
                                (before_row if before_row is not None else 9223372036854775807, limit)).fetchall()
            items = [dict(json.loads(r["payload"]), cursor=r["rowid"]) for r in rows]
            active = Store.active(conn)
            return {"open_trade": active if active and active["status"] == "OPEN" else None,
                    "closed_trades": [x for x in items if x["status"] == "CLOSED"],
                    "items": items, "next_cursor": rows[-1]["rowid"] if rows else None,
                    "health": read_health(conn)}

    @app.get("/history")
    def history(limit: int = Query(30, ge=1, le=200)):
        now = clock()
        with app.state.store.connect() as conn:
            items = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM decisions ORDER BY candle_close DESC LIMIT ?", (limit,))]
            for item in items:
                item["observed_data_status"] = item["data_status"]
                item["freshness_errors"] = freshness_errors(item.get("source_candle_closes", {}), now, policy)
                item["data_status"] = "STALE" if item["freshness_errors"] else "HISTORICAL"
                item["served_at"] = stamp(now)
            return items

    @app.get("/market-status")
    def market_status():
        with app.state.store.connect() as conn:
            return {"provider": "Twelve Data", "symbol": "XAU/USD", "interval": "5min",
                    "monitor_interval": "1min", "confirmation": "15min", "bias": ["1h", "4h"],
                    "health": read_health(conn)}

    @app.get("/strategy")
    def strategy():
        phase2 = strategy_version.startswith(("phase2-", "phase3a-"))
        return {"version": strategy_version, "mode": "DEMO_ONLY",
                "confidence_kind": "EMPIRICAL_DEMO_BETA_BIN_OR_UNCALIBRATED_WARMUP" if phase2 else "UNCALIBRATED_SCORE",
                "code_hash": app.state.engine.code_hash,
                "note": "Technical council plus source-stamped macro/news intelligence; unavailable providers block entries."
                        if strategy_version.startswith("phase3a-") else
                        "Technical council with historical demo calibration; 4H bias is price-derived, not macro news."
                        if phase2 else "Original 5m scores plus closed-candle confirmation and risk vetoes."}

    # No reset endpoint or public custom-candle mutation endpoint.
    return app


app = create_app()
