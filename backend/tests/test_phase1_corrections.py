"""Regression coverage for the five Phase 1 review findings. No network calls."""
from datetime import timedelta
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.domain import Policy, freshness_errors, stamp
from backend.engine import Engine, implementation_hash
from backend.main import create_app
from backend.storage import Store
from test_phase1 import NOW, frames, frame, append_minute, active, engine


def saved_trade(engine):
    with engine.store.connect() as conn:
        return json.loads(conn.execute("SELECT payload FROM trades").fetchone()[0])


def open_position(engine):
    data = frames()
    engine.tick(data, NOW)
    for minute in (0, 1):
        append_minute(data, NOW.replace(second=0) + timedelta(minutes=minute))
    engine.tick(data, NOW + timedelta(minutes=2))
    assert active(engine)["status"] == "OPEN"
    return data


@pytest.mark.parametrize("direction", ["BUY", "SELL"])
@pytest.mark.parametrize("outcome", ["SL", "TP2"])
def test_later_drift_veto_cannot_erase_historical_fill(engine, direction, outcome):
    data = frames()
    if direction == "SELL":
        data = {k: frame(k, down=True) for k in data}
    decision = engine.tick(data, NOW)
    trade = active(engine)
    assert trade["direction"] == direction
    exit_price = trade["sl"] if outcome == "SL" else trade["tp2"]
    append_minute(data, NOW.replace(second=0), price=trade["entry"])
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=1),
                  price=trade["entry"], high=max(trade["entry"], exit_price) + .1,
                  low=min(trade["entry"], exit_price) - .1)
    data["1min"][-1]["c"] = exit_price
    restarted = Engine(Store(engine.store.path))
    result = restarted.tick(data, NOW + timedelta(minutes=2))
    assert "ENTRY_DRIFT" in result["veto_codes"]
    closed = saved_trade(engine)
    assert closed["status"] == "CLOSED" and closed["result"] == outcome
    assert closed["opened_at"] == "2026-09-15T14:01:00+00:00"
    assert closed["r_multiple"] == (-1 if outcome == "SL" else pytest.approx(2.1, abs=.01))
    with engine.store.connect() as conn:
        kinds = [r[0] for r in conn.execute("SELECT kind FROM trade_events ORDER BY id")]
    assert kinds == ["CREATED", "FILLED", "BAR", "CLOSED"]
    assert restarted.replay(decision["decision_id"])["matches"]


def test_veto_before_eligible_open_cancels(engine):
    data = frames()
    engine.tick(data, NOW)
    veto_engine = Engine(engine.store, Policy(kill_switch=True))
    veto_engine.tick(data, NOW + timedelta(seconds=20))
    assert saved_trade(engine)["result"] == "RISK_VETO"


def test_invalid_minute_feed_can_veto_before_fill_is_eligible(engine):
    data = frames()
    engine.tick(data, NOW)
    data["1min"] = None
    engine.tick(data, NOW + timedelta(seconds=20), {"1min": "ReadTimeout"})
    assert saved_trade(engine)["result"] == "RISK_VETO"


def test_veto_during_forming_fill_bar_does_not_cancel(engine):
    data = frames()
    engine.tick(data, NOW)
    append_minute(data, NOW.replace(second=0))
    veto_engine = Engine(engine.store, Policy(kill_switch=True))
    veto_engine.tick(data, NOW + timedelta(minutes=1))
    assert active(engine)["status"] == "PENDING"
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=1), low=4330)
    veto_engine.tick(data, NOW + timedelta(minutes=2))
    assert saved_trade(engine)["result"] == "SL"


def test_missing_fill_history_cannot_be_cancelled_by_expiry_or_veto(engine):
    data = frames()
    engine.tick(data, NOW)
    result = Engine(engine.store, Policy(kill_switch=True)).tick(data, NOW + timedelta(minutes=6))
    assert active(engine)["status"] == "PENDING"
    assert "MONITOR_CATCHUP_GAP" in result["veto_codes"]
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=1), low=4330)
    Engine(engine.store).tick(data, NOW + timedelta(minutes=7))
    assert saved_trade(engine)["result"] == "SL"


@pytest.mark.parametrize("interval", ["5min", "15min", "1h", "4h"])
@pytest.mark.parametrize("bad", ["nan", "inf", "timestamp", "row", "frame"])
def test_invalid_independent_feed_does_not_block_exit_or_hash(engine, interval, bad):
    data = open_position(engine)
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=2), low=4330)
    if bad in ("nan", "inf"):
        data[interval][-1]["c"] = float(bad)
    elif bad == "timestamp":
        data[interval][-1]["t"] = None
    elif bad == "row":
        data[interval][-1] = None
    else:
        data[interval] = None
    result = engine.tick(data, NOW + timedelta(minutes=3))
    assert saved_trade(engine)["result"] == "SL"
    assert f"INVALID_DATA:{interval}" in result["veto_codes"]
    with engine.store.connect() as conn:
        payload = conn.execute("SELECT payload FROM snapshots ORDER BY observed_at DESC LIMIT 1").fetchone()[0]
        json.loads(payload, parse_constant=lambda x: pytest.fail(f"Non-JSON value: {x}"))


@pytest.mark.parametrize("old_defect", ["gap", "nan", "short_history"])
def test_only_checkpoint_onward_is_needed_for_open_exit(engine, old_defect):
    data = open_position(engine)
    if old_defect == "gap":
        del data["1min"][10]
    elif old_defect == "nan":
        data["1min"][10]["c"] = float("nan")
    else:
        data["1min"] = []
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=2), low=4330)
    result = Engine(engine.store).tick(data, NOW + timedelta(minutes=3))
    assert saved_trade(engine)["result"] == "SL"
    assert result["monitor_veto_codes"] == []


def test_gap_after_checkpoint_still_blocks_exit(engine):
    data = open_position(engine)
    original = active(engine)
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=3), low=4330)
    result = engine.tick(data, NOW + timedelta(minutes=4))
    assert active(engine) == original
    assert "MONITOR_CATCHUP_GAP" in result["monitor_veto_codes"]


def test_responses_recompute_source_freshness_before_heartbeat_expires(engine):
    data = frames()
    data["1min"].pop()  # latest close 13:59, initially fresh
    engine.tick(data, NOW)
    with engine.store.connect() as conn:
        before = list(conn.iterdump())
    now = [NOW]
    with TestClient(create_app(engine.store.path, start_worker=False, clock=lambda: now[0])) as client:
        assert client.get("/signal").json()["data_status"] == "LIVE_DATA"
        now[0] += timedelta(seconds=140)  # heartbeat fresh, source stale
        result = client.get("/signal").json()
        assert result["direction"] == "NO_TRADE" and result["data_status"] == "STALE"
        assert "STALE_DATA:1min" in result["veto_codes"]
        assert "MONITOR_STALE" not in result["veto_codes"]
        assert client.get("/health").status_code == 503
        for endpoint in ("/market-status", "/performance", "/trades"):
            assert "STALE_DATA:1min" in client.get(endpoint).json()["health"]["data_errors"]
        history = client.get("/history").json()[0]
        assert history["data_status"] == "STALE"
        assert history["observed_data_status"] == "LIVE_DATA"
    with engine.store.connect() as conn:
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("interval", ["1min", "5min", "15min", "1h", "4h"])
def test_freshness_boundary_for_every_source(interval):
    from backend.domain import INTERVALS
    closes = {k: stamp(NOW - timedelta(seconds=1)) for k in INTERVALS}
    closes[interval] = stamp(NOW - timedelta(seconds=INTERVALS[interval] + 90))
    assert freshness_errors(closes, NOW, Policy()) == [f"STALE_DATA:{interval}"]
    assert freshness_errors(closes, NOW - timedelta(microseconds=1), Policy()) == []


def test_future_source_close_fails_freshness():
    from backend.domain import INTERVALS
    closes = {k: stamp(NOW) for k in INTERVALS}
    closes["4h"] = stamp(NOW + timedelta(seconds=1))
    assert freshness_errors(closes, NOW, Policy()) == ["STALE_DATA:4h"]


def test_rejected_nan_snapshot_replays_without_rewriting_history(engine):
    data = frames()
    data["4h"][-1]["c"] = float("nan")
    result = engine.tick(data, NOW)
    assert result["direction"] == "NO_TRADE"
    assert engine.replay(result["decision_id"])["matches"]


def test_missing_source_metadata_fails_closed(engine):
    engine.tick(frames(), NOW)
    with engine.store.transaction() as conn:
        health = Store.get_state(conn, "health")
        health.pop("source_candle_closes")
        Store.set_state(conn, "health", health)
    with TestClient(create_app(engine.store.path, start_worker=False, clock=lambda: NOW)) as client:
        assert client.get("/signal").json()["data_status"] != "LIVE_DATA"


def test_portable_hash_and_replay_across_line_endings(engine, tmp_path):
    decision = engine.tick(frames(), NOW)
    root = Path(__file__).parents[1]
    names = ("scoring.py", "domain.py", "engine.py", "storage.py")
    for ending in ("\n", "\r\n", "\r"):
        for name in names:
            (tmp_path / name).write_bytes((root / name).read_text(encoding="utf-8").replace("\n", ending).encode("utf-8"))
        assert implementation_hash(tmp_path) == engine.code_hash
        restarted = Engine(engine.store)
        restarted.code_hash = implementation_hash(tmp_path)
        assert restarted.replay(decision["decision_id"])["matches"]
    with (tmp_path / "engine.py").open("a", encoding="utf-8") as output:
        output.write("\n# different implementation\n")
    assert implementation_hash(tmp_path) != engine.code_hash
