from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from backend.domain import (INTERVALS, Policy, canonical, closed_frame, evaluate,
                            parse, stamp, market_closed)
from backend.engine import Engine
from backend.main import create_app, database_path
from backend.storage import Store
from backend.worker import Monitor, WorkerLock, seconds_to_next_tick

NOW = parse("2026-09-15T14:00:10+00:00")


def frame(interval, now=NOW, down=False):
    seconds = INTERVALS[interval]
    boundary = now.replace(second=0, microsecond=0)
    boundary -= timedelta(seconds=int(boundary.timestamp()) % seconds)
    result = []
    for i in range(120):
        price = 4371.6 - i * .3 if down else 4300 + i * .3
        result.append(dict(t=stamp(boundary - timedelta(seconds=seconds * (120-i))),
                           o=price, h=price+.5, l=price-.4, c=price+(.2 if not down else -.2)))
    return result


def frames(now=NOW):
    return {k: frame(k, now) for k in INTERVALS}


def snapshot(data, now=NOW):
    return {"observed_at": stamp(now), "frames": data}


def append_minute(data, opened, price=4335.9, high=None, low=None):
    data["1min"].append(dict(t=stamp(opened), o=price, h=high if high is not None else price+.1,
                               l=low if low is not None else price-.1, c=price))


@pytest.fixture
def engine(tmp_path):
    return Engine(Store(tmp_path / "demo.sqlite3"))


def active(engine):
    with engine.store.connect() as conn:
        return Store.active(conn)


def test_original_scores_and_deterministic_decision():
    data = frames()
    first = evaluate(snapshot(data), Policy())
    assert first == evaluate(snapshot(data), Policy())
    assert (first["direction"], first["buy_score"], first["sell_score"]) == ("BUY", 78, 5)
    assert first["confidence_kind"] == "UNCALIBRATED_SCORE"
    assert first["timeframes"]["4h"]["candle_close"] == "2026-09-15T12:00:00+00:00"


@pytest.mark.parametrize("interval,code", [("15min", "CONFIRMATION_15M"),
                                          ("1h", "BIAS_CONFLICT:1h"), ("4h", "BIAS_CONFLICT:4h")])
def test_confirmation_and_bias_veto(interval, code):
    data = frames()
    data[interval] = frame(interval, down=True)
    result = evaluate(snapshot(data), Policy())
    assert result["candidate_direction"] == "BUY"
    assert result["direction"] == "NO_TRADE"
    assert code in result["veto_codes"]
    assert result["sl"] is None


def test_forming_candle_cannot_change_scores():
    data = frames()
    before = evaluate(snapshot(data), Policy())
    data["5min"].append(dict(t="2026-09-15T14:00:00+00:00", o=4335, h=5000, l=4000, c=4999))
    assert evaluate(snapshot(data), Policy()) == before


@pytest.mark.parametrize("kind", ["stale", "missing", "nan", "duplicate", "ohlc", "future", "gap"])
def test_bad_data_blocks_entries(kind):
    data = frames()
    if kind == "stale":
        data["1min"] = frame("1min", NOW-timedelta(minutes=10))
    elif kind == "missing":
        data.pop("15min")
    elif kind == "nan":
        data["5min"][-1]["c"] = float("nan")
    elif kind == "duplicate":
        data["5min"].append(data["5min"][-1])
    elif kind == "ohlc":
        data["5min"][-1]["l"] = 9000
    elif kind == "future":
        data["1min"][-1]["t"] = stamp(NOW+timedelta(minutes=2))
    else:
        del data["1min"][-3]
    result = evaluate(snapshot(data), Policy())
    assert result["direction"] == "NO_TRADE"
    assert result["risk_veto"]


def test_kill_switch_and_market_closure():
    assert "KILL_SWITCH" in evaluate(snapshot(frames()), Policy(kill_switch=True))["veto_codes"]
    assert market_closed(parse("2026-09-19T15:00:00Z"))
    assert not market_closed(NOW)


def test_unique_decision_under_concurrent_ticks_and_restart(engine):
    data = frames()
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Engine(Store(engine.store.path)).tick(data, NOW), range(4)))
    restarted = Engine(Store(engine.store.path))
    restarted.tick(data, NOW+timedelta(seconds=10))
    with restarted.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert active(restarted)["status"] == "PENDING"


def test_no_pre_entry_candles_then_fill_and_target_survive_restart(engine):
    data = frames()
    decision = engine.tick(data, NOW)
    assert active(engine)["opened_at"] is None
    # This candle starts before the pending order's first eligible full minute.
    append_minute(data, NOW.replace(second=0), high=4350, low=4200)
    engine.tick(data, NOW+timedelta(minutes=1))
    assert active(engine)["status"] == "PENDING"
    append_minute(data, NOW.replace(second=0)+timedelta(minutes=1))
    engine.tick(data, NOW+timedelta(minutes=2))
    assert active(engine)["status"] == "OPEN"
    assert active(engine)["opened_at"] == "2026-09-15T14:01:00+00:00"
    restarted = Engine(Store(engine.store.path))
    append_minute(data, NOW.replace(second=0)+timedelta(minutes=2), high=4344)
    restarted.tick(data, NOW+timedelta(minutes=3))
    assert active(restarted) is None
    with restarted.store.connect() as conn:
        summary = Store.summary(conn)
        assert summary["closed_trades"] == 1 and summary["wins"] == 1
        assert summary["total_r"] > 2
        before = conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0]
    restarted.tick(data, NOW+timedelta(minutes=3, seconds=10))
    with restarted.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM trade_events").fetchone()[0] == before
    assert restarted.replay(decision["decision_id"])["matches"]


def test_stop_first_ambiguity_and_adverse_gap():
    from backend.scoring import Candle
    trade = dict(direction="BUY", entry=100, sl=95, tp1=105, tp2=110,
                 status="OPEN", ambiguity=False, tp1_hit=False, tp2_hit=False)
    Engine._apply_bar(trade, Candle(stamp(NOW.replace(second=0)), 100, 112, 94, 100))
    assert trade["result"] == "SL" and trade["r_multiple"] == -1 and trade["ambiguity"]
    trade.update(status="OPEN")
    Engine._apply_bar(trade, Candle(stamp(NOW.replace(second=0)), 90, 94, 88, 92))
    assert trade["exit_price"] == 90 and trade["r_multiple"] == -2


def test_pending_gap_cancelled(engine):
    data = frames()
    engine.tick(data, NOW)
    append_minute(data, NOW.replace(second=0))
    append_minute(data, NOW.replace(second=0)+timedelta(minutes=1), price=4340)
    engine.tick(data, NOW+timedelta(minutes=2))
    assert active(engine) is None
    with engine.store.connect() as conn:
        trade = json.loads(conn.execute("SELECT payload FROM trades").fetchone()[0])
        assert trade["status"] == "CANCELLED"


def test_signal_episode_not_reopened_each_five_minutes(engine):
    data = frames()
    engine.tick(data, NOW)
    with engine.store.transaction() as conn:
        trade = Store.active(conn)
        trade.update(status="CANCELLED", result="EXPIRED")
        Store.save_trade(conn, trade)
    next_now = NOW+timedelta(minutes=5)
    result = Engine(Store(engine.store.path)).tick(frames(next_now), next_now)
    assert "DUPLICATE_SIGNAL_EPISODE" in result["veto_codes"]
    with engine.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
    assert engine.replay(result["decision_id"])["matches"]


def test_daily_risk_veto(engine):
    engine.tick(frames(), NOW)
    with engine.store.transaction() as conn:
        trade = Store.active(conn)
        trade.update(status="CLOSED", result="SL", closed_at=stamp(NOW), r_multiple=-3.0)
        Store.save_trade(conn, trade)
        Store.set_state(conn, "episode", {"direction": "NO_TRADE", "used": False})
    next_now = NOW+timedelta(minutes=5)
    result = engine.tick(frames(next_now), next_now)
    assert "DAILY_DEMO_LOSS_LIMIT" in result["veto_codes"]
    assert active(engine) is None
    assert engine.replay(result["decision_id"])["matches"]


def test_api_gets_are_read_only_and_fail_closed_when_worker_stale(engine):
    engine.tick(frames(), NOW)
    now = [NOW]
    app = create_app(engine.store.path, start_worker=False, clock=lambda: now[0])
    with engine.store.connect() as conn:
        before = list(conn.iterdump())
    with TestClient(app) as client:
        for _ in range(3):
            for endpoint in ("/signal", "/performance", "/trades", "/history", "/health"):
                assert client.get(endpoint).status_code == 200
        assert client.post("/reset-demo").status_code == 404
        assert client.post("/analyze", json={}).status_code == 404
        now[0] += timedelta(minutes=3)
        result = client.get("/signal").json()
        assert result["direction"] == "NO_TRADE" and "MONITOR_STALE" in result["veto_codes"]
        assert client.get("/health").status_code == 503
    with engine.store.connect() as conn:
        assert before == list(conn.iterdump())


def test_backup_restore_retains_decisions_and_history(engine, tmp_path):
    result = engine.tick(frames(), NOW)
    path = tmp_path / "restored.sqlite3"
    engine.store.backup(path)
    restored = Engine(Store(path))
    assert active(restored)["id"] == active(engine)["id"]
    assert restored.replay(result["decision_id"])["matches"]


def test_transaction_rolls_back_on_failure(engine):
    with pytest.raises(RuntimeError), engine.store.transaction() as conn:
        Store.set_state(conn, "should_not_exist", True)
        raise RuntimeError("crash")
    with engine.store.connect() as conn:
        assert Store.get_state(conn, "should_not_exist") is None


def test_worker_logs_do_not_leak_provider_secrets(engine, caplog, monkeypatch):
    import logging
    monkeypatch.setattr(logging.getLogger("dardania"), "propagate", True)
    class BrokenFeed:
        def fetch(self, now):
            raise RuntimeError("apikey=SECRET_VALUE")
    monitor = Monitor(engine, BrokenFeed(), clock=lambda: NOW)
    monitor.run_once()
    assert "SECRET_VALUE" not in caplog.text
    assert "monitor_failed" in caplog.text
    with engine.store.connect() as conn:
        assert Store.get_state(conn, "health")["status"] == "ERROR"


def test_worker_lock_and_minute_alignment(tmp_path):
    path = tmp_path / "worker.lock"
    with WorkerLock(path):
        with pytest.raises(RuntimeError):
            with WorkerLock(path):
                pass
    with WorkerLock(path):
        pass
    assert seconds_to_next_tick(NOW) == 60
    assert seconds_to_next_tick(NOW+timedelta(seconds=20)) == 40


def test_render_refuses_ephemeral_database(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("DEMO_DB_PATH", "relative/demo.sqlite3")
    with pytest.raises(RuntimeError, match="persistent"):
        database_path()


def test_catchup_processes_missed_minutes_in_order(engine):
    data = frames()
    engine.tick(data, NOW)
    for minute in range(4):
        append_minute(data, NOW.replace(second=0)+timedelta(minutes=minute),
                      high=4344 if minute == 3 else None)
    # No intermediate worker runs: recover all bars from the persisted checkpoint.
    result = Engine(Store(engine.store.path)).tick(data, NOW+timedelta(minutes=4))
    with engine.store.connect() as conn:
        trade = json.loads(conn.execute("SELECT payload FROM trades").fetchone()[0])
        assert trade["result"] == "TP2"
        assert trade["closed_at"] == "2026-09-15T14:04:00+00:00"
        bars = conn.execute("SELECT occurred_at FROM trade_events WHERE kind='BAR' ORDER BY id").fetchall()
        assert [r[0] for r in bars] == ["2026-09-15T14:01:00+00:00", "2026-09-15T14:02:00+00:00", "2026-09-15T14:03:00+00:00"]


def test_missing_catchup_data_does_not_fabricate_trade_result(engine):
    data = frames()
    engine.tick(data, NOW)
    for minute in (0, 1):
        append_minute(data, NOW.replace(second=0)+timedelta(minutes=minute))
    engine.tick(data, NOW+timedelta(minutes=2))
    original = active(engine)
    later = NOW+timedelta(hours=3)
    result = engine.tick(frames(later), later)
    assert "MONITOR_CATCHUP_GAP" in result["veto_codes"]
    assert active(engine) == original
    assert engine.replay(result["decision_id"])["matches"]


def test_open_trade_protection_continues_when_higher_timeframe_feed_fails(engine):
    data = frames()
    engine.tick(data, NOW)
    for minute in (0, 1):
        append_minute(data, NOW.replace(second=0)+timedelta(minutes=minute))
    engine.tick(data, NOW+timedelta(minutes=2))
    append_minute(data, NOW.replace(second=0)+timedelta(minutes=2), low=4330)
    data["4h"] = []
    engine.tick(data, NOW+timedelta(minutes=3), {"4h": "ReadTimeout"})
    with engine.store.connect() as conn:
        assert Store.summary(conn)["losses"] == 1
        assert Store.active(conn) is None


def test_unknown_schema_is_rejected_without_modifying_it(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE schema_version(version INTEGER PRIMARY KEY)")
        conn.execute("INSERT INTO schema_version VALUES (999)")
    with pytest.raises(RuntimeError, match="schema"):
        Store(path)
    with sqlite3.connect(path) as conn:
        assert conn.execute("SELECT version FROM schema_version").fetchall() == [(999,)]


def test_replay_detects_modified_snapshot(engine):
    result = engine.tick(frames(), NOW)
    with engine.store.transaction() as conn:
        conn.execute("UPDATE snapshots SET payload=replace(payload,'4335.9','4335.8')")
    with pytest.raises(ValueError, match="checksum"):
        engine.replay(result["decision_id"])


def test_feed_forces_utc_and_does_not_serve_failed_cache(monkeypatch):
    import httpx
    from backend.feed import TwelveDataFeed
    calls = []
    original_client = httpx.Client
    def responder(request):
        calls.append(request)
        assert request.url.params["timezone"] == "UTC"
        assert request.url.params["symbol"] == "XAU/USD"
        if len(calls) > 1:
            return httpx.Response(429, json={"message": "secret api key text"})
        return httpx.Response(200, json={"values": [
            {"datetime": "2026-09-15 14:00:00", "open": "4335", "high": "4336", "low": "4334", "close": "4335"}
        ]})
    monkeypatch.setattr(httpx, "Client", lambda **kw: original_client(transport=httpx.MockTransport(responder), **kw))
    feed = TwelveDataFeed("test-secret")
    rows = feed._fetch("5min", NOW)
    assert rows[0]["t"] == "2026-09-15T14:00:00+00:00"
    assert feed._fetch("5min", NOW+timedelta(minutes=1)) == rows
    assert len(calls) == 1
    with pytest.raises(httpx.HTTPStatusError):
        feed._fetch("5min", NOW+timedelta(minutes=6))


def test_no_api_key_is_a_safe_provider_error():
    from backend.feed import TwelveDataFeed
    values, errors = TwelveDataFeed("").fetch(NOW)
    assert set(errors) == set(INTERVALS)
    assert all(v == [] for v in values.values())


def test_running_worker_operates_without_http_requests(engine):
    import threading
    class Feed:
        def fetch(self, now):
            return frames(), {}
    complete = threading.Event()
    original_tick = engine.tick
    def tick(*args):
        result = original_tick(*args)
        complete.set()
        return result
    engine.tick = tick
    monitor = Monitor(engine, Feed(), clock=lambda: NOW)
    monitor.start()
    try:
        assert complete.wait(5)
    finally:
        monitor.stop()
    assert active(engine)["status"] == "PENDING"


def test_new_opposite_episode_can_open_after_previous_trade_finishes(engine):
    engine.tick(frames(), NOW)
    with engine.store.transaction() as conn:
        trade = Store.active(conn)
        trade.update(status="CANCELLED", result="EXPIRED")
        Store.save_trade(conn, trade)
    later = NOW+timedelta(minutes=5)
    data = {k: frame(k, later, down=True) for k in INTERVALS}
    result = engine.tick(data, later)
    assert result["direction"] == "SELL"
    assert active(engine)["direction"] == "SELL"


def test_high_volatility_blocks_new_entry():
    data = frames()
    data["5min"][-1].update(h=4370, l=4300)
    result = evaluate(snapshot(data), Policy())
    assert result["direction"] == "NO_TRADE"
    assert "HIGH_VOLATILITY" in result["veto_codes"]


def test_liveness_stays_up_when_data_is_missing(engine):
    with TestClient(create_app(engine.store.path, start_worker=False, clock=lambda: NOW)) as client:
        assert client.get("/live").status_code == 200
        assert client.get("/health").status_code == 503
        result = client.get("/signal").json()
        assert result["direction"] == "NO_TRADE"
        assert result["timestamp_utc"] == ""


def test_expected_daily_session_gap_is_allowed():
    from backend.domain import allowed_session_gap
    # September: New York 17:00–18:00 is 21:00–22:00 UTC.
    assert allowed_session_gap(parse("2026-09-15T21:00:00Z"), parse("2026-09-15T22:00:00Z"), 60)
    assert not allowed_session_gap(parse("2026-09-15T20:59:00Z"), parse("2026-09-15T22:00:00Z"), 60)
