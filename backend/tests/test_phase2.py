"""Offline Phase 2 behavior, calibration leakage, persistence and replay tests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
import json

import pytest
from fastapi.testclient import TestClient

from backend.calibration import calibrate
from backend.council import CouncilPolicy, MODEL_VERSION, WEIGHTS, evaluate_council
from backend.domain import INTERVALS, Policy, canonical, digest, parse, stamp
from backend.engine import Engine
from backend.phase2 import Phase2Engine, SOURCE_FILES, implementation_hash
from backend.phase2_main import create_phase2_app
from backend.storage import Store
from test_phase1 import NOW, active, append_minute, frame, frames


@pytest.fixture
def engine(tmp_path):
    return Phase2Engine(Store(tmp_path / "phase2.sqlite3"))


def evaluate(data=None, now=NOW, settings=CouncilPolicy(), samples=None, policy=Policy()):
    return evaluate_council({"observed_at": stamp(now), "frames": frames(now) if data is None else data,
                             "council_policy": asdict(settings), "calibration_samples": samples or [],
                             "model_identity": "test-model"}, policy)


def outcomes(count=30, wins=24, direction="BUY", score=94):
    return [{"trade_id": f"history-{i}", "direction": direction, "raw_score": score,
             "win": i < wins, "closed_at": stamp(NOW - timedelta(minutes=20, seconds=i)),
             "known_at": stamp(NOW - timedelta(minutes=19, seconds=i))} for i in range(count)]


def seed_closed_history(engine, samples, model_identity=None, record_events=True):
    """Explicit synthetic journal fixtures; never invoked by production code."""
    with engine.store.transaction() as conn:
        for i, sample in enumerate(samples):
            identifier = sample["trade_id"]
            snap = {"observed_at": sample["known_at"], "fixture": identifier}
            snap_id = digest(snap)
            decision = {"strategy_version": engine.strategy_version,
                        "model_version": MODEL_VERSION, "model_identity": model_identity or engine.model_identity,
                        "candidate_direction": sample["direction"], "raw_score": sample["raw_score"]}
            trade = {"id": identifier, "decision_id": identifier, "status": "CLOSED",
                     "direction": sample["direction"], "closed_at": sample["closed_at"],
                     "r_multiple": 2.1 if sample["win"] else -1, "result": "TP2" if sample["win"] else "SL"}
            conn.execute("INSERT INTO snapshots VALUES (?,?,?)", (snap_id, sample["known_at"], canonical(snap)))
            conn.execute("INSERT INTO decisions VALUES (?,?,?,?)", (identifier,
                         stamp(NOW - timedelta(days=1, minutes=i)), snap_id, canonical(decision)))
            conn.execute("INSERT INTO trades VALUES (?,?,?,?)", (identifier, identifier, "CLOSED", canonical(trade)))
            if record_events:
                Store.event(conn, trade, "CLOSED", trade["closed_at"], snap_id)


@pytest.mark.parametrize("down,direction", [(False, "BUY"), (True, "SELL")])
def test_components_and_all_timeframe_roles(down, direction):
    data = {k: frame(k, down=down) for k in INTERVALS}
    result = evaluate(data)
    assert result["direction"] == direction
    assert set(result["components"]) == set(WEIGHTS)
    assert set(result["timeframes"]) == set(INTERVALS)
    assert result["timeframes"]["4h"]["role"] == "MACRO_TREND_BIAS"
    for interval, item in result["timeframes"].items():
        assert item["bias"] == direction
        assert parse(item["candle_close"]) <= (NOW if interval == "1min" else parse(result["signal_candle_close"]))
    for side in ("buy", "sell"):
        assert result[f"{side}_score"] == round(sum(c[side] * c["weight"] for c in result["components"].values()), 4)
    assert all(0 <= c[side] <= 100 for c in result["components"].values() for side in ("buy", "sell"))
    assert result["warmup_trade"] and result["calibrated_confidence"] is None
    assert result["confidence_kind"] == "UNCALIBRATED_WARMUP"
    assert len(result["reasons"]) >= 6


@pytest.mark.parametrize("interval", ["15min", "1h", "4h"])
@pytest.mark.parametrize("neutral", [False, True])
def test_timeframe_disagreement_is_a_hard_veto(interval, neutral):
    data = frames()
    data[interval] = frame(interval, down=True)
    if neutral:
        for row in data[interval]:
            row.update(o=4335.9, h=4336.2, l=4335.6, c=4335.9)
    result = evaluate(data)
    assert result["candidate_direction"] == "BUY"
    assert result["direction"] == "NO_TRADE"
    assert f"TIMEFRAME_DISAGREEMENT:{interval}" in result["veto_codes"]
    assert result["sl"] is result["tp1"] is result["tp2"] is None


def test_one_minute_opposition_blocks_timing():
    data = frames()
    data["1min"] = frame("1min", down=True)
    result = evaluate(data)
    assert "TIMING_DISAGREEMENT:1min" in result["veto_codes"]
    assert result["direction"] == "NO_TRADE"


def test_trend_reversal_changes_components_and_meta_direction():
    data = frames()
    assert evaluate(data)["direction"] == "BUY"
    for rows in data.values():
        for i, row in enumerate(rows[80:]):
            price = 4324 - i * .6
            row.update(o=price, h=price+.4, l=price-.5, c=price-.2)
    result = evaluate(data)
    assert result["direction"] == "SELL"
    assert result["components"]["technical"]["sell"] > result["components"]["technical"]["buy"]
    assert result["components"]["momentum"]["sell"] > result["components"]["momentum"]["buy"]


@pytest.mark.parametrize("interval", list(INTERVALS))
def test_every_stale_feed_vetoes(interval):
    data = frames()
    data[interval] = frame(interval, NOW - timedelta(seconds=INTERVALS[interval] + 180))
    result = evaluate(data)
    assert result["direction"] == "NO_TRADE"
    assert f"STALE_DATA:{interval}" in result["veto_codes"]
    assert result["data_status"] != "LIVE_DATA"


@pytest.mark.parametrize("interval", ["15min", "1h", "4h"])
@pytest.mark.parametrize("kind", ["missing", "nan", "short"])
def test_missing_or_invalid_higher_timeframes_fail_closed(interval, kind):
    data = frames()
    if kind == "missing":
        del data[interval]
    elif kind == "nan":
        data[interval][-1]["c"] = float("nan")
    else:
        data[interval] = data[interval][-59:]
    result = evaluate(data)
    assert result["direction"] == "NO_TRADE" and result["risk_veto"]


@pytest.mark.parametrize("interval", list(INTERVALS))
def test_extreme_volatility_is_separate_from_direction(interval):
    data = frames()
    data[interval][-1].update(h=4370, l=4300)
    result = evaluate(data)
    assert result["direction"] == "NO_TRADE"
    assert f"EXTREME_VOLATILITY:{interval}" in result["veto_codes"]
    assert result["mode"] == "HIGH_VOLATILITY"


def test_future_forming_higher_candle_does_not_enter_council():
    data = frames()
    initial = evaluate(data)
    data["4h"].append(dict(t="2026-09-15T12:00:00+00:00", o=4335, h=9000, l=1000, c=8000))
    after = evaluate(data)
    assert after == initial


def test_flat_market_is_no_trade():
    data = frames()
    for rows in data.values():
        for row in rows:
            row.update(o=4335.9, h=4336.1, l=4335.7, c=4335.9)
    result = evaluate(data)
    assert result["direction"] == "NO_TRADE" and "INSUFFICIENT_SCORE" in result["veto_codes"]


def test_kill_switch_veto_remains():
    result = evaluate(policy=Policy(kill_switch=True))
    assert result["direction"] == "NO_TRADE" and "KILL_SWITCH" in result["veto_codes"]


def test_warmup_is_explicit_and_stricter_than_normal_score_gate():
    result = evaluate(settings=replace(CouncilPolicy(), warmup_min_score=99))
    assert result["candidate_direction"] == "BUY" and result["direction"] == "NO_TRADE"
    assert "INSUFFICIENT_CALIBRATION" in result["veto_codes"]
    assert result["calibrated_confidence"] is None and not result["warmup_trade"]
    blocked = evaluate(settings=replace(CouncilPolicy(), allow_demo_warmup=False))
    assert blocked["direction"] == "NO_TRADE"


def test_calibration_probability_and_confidence_veto():
    good = evaluate(samples=outcomes())
    assert good["direction"] == "BUY"
    assert good["confidence"] == pytest.approx(26 / 34, abs=1e-6)
    assert good["confidence"] == good["calibrated_confidence"]
    assert good["confidence_kind"] == "EMPIRICAL_DEMO_BETA_BIN"
    bad = evaluate(samples=outcomes(wins=2))
    assert bad["direction"] == "NO_TRADE"
    assert "INSUFFICIENT_CONFIDENCE" in bad["veto_codes"]
    assert not bad["warmup_trade"]


def test_timeframe_veto_does_not_claim_a_warmup_trade():
    data = frames()
    data["4h"] = frame("4h", down=True)
    result = evaluate(data)
    assert result["calibration"]["status"] == "WARMUP"
    assert result["direction"] == "NO_TRADE" and not result["warmup_trade"]


def test_calibration_excludes_future_outcomes_and_late_knowledge():
    samples = outcomes()
    for sample in samples[:10]:
        sample["closed_at"] = stamp(NOW + timedelta(minutes=1))
    for sample in samples[10:20]:
        sample["known_at"] = stamp(NOW + timedelta(minutes=1))
    result = evaluate(samples=samples)
    assert result["calibration"]["sample_count"] == 10
    assert result["calibrated_confidence"] is None


def test_calibration_buckets_are_direction_specific_and_duplicates_rejected():
    samples = outcomes()
    assert calibrate("SELL", 94, samples, NOW)["sample_count"] == 0
    assert calibrate("BUY", 70, samples, NOW)["sample_count"] == 0
    with pytest.raises(ValueError, match="Duplicate"):
        calibrate("BUY", 94, samples + samples[:1], NOW)


def test_calibration_snapshot_and_replay_ignore_later_database_changes(engine):
    seed_closed_history(engine, outcomes(wins=30))
    result = engine.tick(frames(), NOW)
    assert result["calibration"]["sample_count"] == 30
    assert result["direction"] == "BUY"
    with engine.store.transaction() as conn:
        conn.execute("UPDATE trade_events SET payload=json_set(payload, '$.r_multiple', -1) WHERE kind='CLOSED'")
    restarted = Phase2Engine(Store(engine.store.path))
    assert restarted.replay(result["decision_id"])["matches"]
    assert active(restarted)["calibrated_confidence"] == result["confidence"]


@pytest.mark.parametrize("invalid_history", ["wrong_model", "late_knowledge", "no_event"])
def test_persistent_calibration_filters_invalid_history(engine, invalid_history):
    samples = outcomes()
    if invalid_history == "late_knowledge":
        for sample in samples:
            sample["known_at"] = stamp(NOW + timedelta(minutes=1))
    seed_closed_history(engine, samples, model_identity="other-model" if invalid_history == "wrong_model" else None,
                        record_events=invalid_history != "no_event")
    result = engine.tick(frames(), NOW)
    assert result["calibration"]["sample_count"] == 0


def test_low_empirical_confidence_cannot_fall_back_to_warmup(tmp_path):
    engine = Phase2Engine(Store(tmp_path / "low-confidence.sqlite3"), Policy(daily_loss_limit_r=100))
    seed_closed_history(engine, outcomes(wins=2))
    result = engine.tick(frames(), NOW)
    assert "INSUFFICIENT_CONFIDENCE" in result["veto_codes"]
    assert "INSUFFICIENT_CALIBRATION" not in result["veto_codes"]
    assert not result["warmup_trade"] and active(engine) is None
    assert engine.replay(result["decision_id"])["matches"]


def test_other_direction_cannot_evict_calibration_and_restart_warmup(engine):
    samples = outcomes(wins=30)
    opposite = outcomes(count=500, wins=500, direction="SELL")
    for i, sample in enumerate(opposite):
        sample.update(trade_id=f"opposite-{i}",
                      closed_at=stamp(NOW - timedelta(minutes=2)),
                      known_at=stamp(NOW - timedelta(minutes=1)))
    seed_closed_history(engine, samples + opposite)
    result = engine.tick(frames(), NOW)
    assert result["calibration"]["sample_count"] == 30
    assert result["confidence_kind"] == "EMPIRICAL_DEMO_BETA_BIN"
    assert not result["warmup_trade"]


def test_episode_dedup_survives_restart_and_actual_target_close(engine):
    data = frames()
    first = engine.tick(data, NOW)
    append_minute(data, NOW.replace(second=0))
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=1), high=4344)
    engine.tick(data, NOW + timedelta(minutes=2))
    assert active(engine) is None
    later = NOW + timedelta(minutes=5)
    restarted = Phase2Engine(Store(engine.store.path))
    result = restarted.tick(frames(later), later)
    assert result["direction"] == "NO_TRADE" and "DUPLICATE_SIGNAL_EPISODE" in result["veto_codes"]
    assert not result["warmup_trade"]
    assert result["calibration"]["sample_count"] == 1
    assert restarted.replay(first["decision_id"])["matches"]
    assert restarted.replay(result["decision_id"])["matches"]


def test_concurrent_ticks_cannot_duplicate_trade_or_decision(engine):
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: Phase2Engine(Store(engine.store.path)).tick(frames(), NOW), range(4)))
    with engine.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1


def test_five_minute_opposite_episode_can_follow_closed_trade(engine):
    data = frames()
    engine.tick(data, NOW)
    append_minute(data, NOW.replace(second=0))
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=1), high=4344)
    engine.tick(data, NOW + timedelta(minutes=2))
    later = NOW + timedelta(minutes=5)
    result = engine.tick({k: frame(k, later, down=True) for k in INTERVALS}, later)
    assert result["direction"] == "SELL" and active(engine)["direction"] == "SELL"


def test_phase1_protections_still_monitor_phase2_trade_with_bad_higher_feed(engine):
    data = frames()
    engine.tick(data, NOW)
    for minute in (0, 1):
        append_minute(data, NOW.replace(second=0) + timedelta(minutes=minute))
    engine.tick(data, NOW + timedelta(minutes=2))
    assert active(engine)["status"] == "OPEN"
    del data["1min"][10]
    data["4h"][-1]["c"] = float("nan")
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=2), low=4330)
    engine.tick(data, NOW + timedelta(minutes=3))
    with engine.store.connect() as conn:
        assert Store.summary(conn)["losses"] == 1
    assert active(engine) is None


def test_phase2_later_risk_veto_cannot_erase_past_fill(engine):
    data = frames()
    engine.tick(data, NOW)
    append_minute(data, NOW.replace(second=0))
    append_minute(data, NOW.replace(second=0) + timedelta(minutes=1), low=4330)
    data["1min"][-1]["c"] = 4330
    result = engine.tick(data, NOW + timedelta(minutes=2))
    assert "ENTRY_DRIFT" in result["veto_codes"]
    with engine.store.connect() as conn:
        assert Store.summary(conn)["losses"] == 1


def test_phase2_api_explains_scores_and_rechecks_freshness_without_writes(engine):
    data = frames()
    data["1min"].pop()
    decision = engine.tick(data, NOW)
    now = [NOW]
    with engine.store.connect() as conn:
        before = list(conn.iterdump())
    with TestClient(create_phase2_app(db_path=engine.store.path, start_worker=False, clock=lambda: now[0])) as client:
        result = client.get("/signal").json()
        assert result["components"] == decision["components"]
        assert result["calibrated_confidence"] is None and result["warmup_trade"]
        assert client.get("/strategy").json()["version"] == "phase2-1"
        now[0] += timedelta(seconds=140)
        stale = client.get("/signal").json()
        assert stale["data_status"] == "STALE" and stale["direction"] == "NO_TRADE"
        assert client.get("/health").status_code == 503
        assert client.post("/reset-demo").status_code == 404
    with engine.store.connect() as conn:
        assert before == list(conn.iterdump())


def test_phase1_database_is_refused_unchanged(tmp_path):
    store = Store(tmp_path / "phase1.sqlite3")
    Engine(store).tick(frames(), NOW)
    with store.connect() as conn:
        before = list(conn.iterdump())
    with pytest.raises(RuntimeError, match="separate demo database"):
        Phase2Engine(store)
    with store.connect() as conn:
        assert before == list(conn.iterdump())


def test_phase2_local_app_refuses_render_and_phase1_path(monkeypatch):
    monkeypatch.setenv("DEMO_DB_PATH", "backend/data/shared.sqlite3")
    with pytest.raises(ValueError, match="own demo database"):
        create_phase2_app(db_path="backend/data/shared.sqlite3")
    monkeypatch.setenv("RENDER", "true")
    with pytest.raises(RuntimeError, match="local-only"):
        create_phase2_app()


def test_phase2_replay_is_portable_and_checks_all_components(engine, tmp_path):
    decision = engine.tick(frames(), NOW)
    source = Path(__file__).parents[1]
    for name in SOURCE_FILES:
        (tmp_path / name).write_bytes((source / name).read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    assert implementation_hash(tmp_path) == engine.code_hash
    assert engine.replay(decision["decision_id"])["matches"]
    with engine.store.transaction() as conn:
        conn.execute("UPDATE decisions SET payload=json_set(payload, '$.components.technical.buy', 0)")
    assert not engine.replay(decision["decision_id"])["matches"]


def test_phase2_snapshot_tamper_is_detected(engine):
    decision = engine.tick(frames(), NOW)
    with engine.store.transaction() as conn:
        conn.execute("UPDATE snapshots SET payload=json_set(payload, '$.council_policy.min_score', 99)")
    with pytest.raises(ValueError, match="checksum"):
        engine.replay(decision["decision_id"])


def test_phase2_backup_restore_and_replay(engine, tmp_path):
    decision = engine.tick(frames(), NOW)
    backup = tmp_path / "restored.sqlite3"
    engine.store.backup(backup)
    restored = Phase2Engine(Store(backup))
    assert restored.replay(decision["decision_id"])["matches"]
    assert active(restored) == active(engine)


def test_phase2_cli_dispatches_replay(engine, monkeypatch, capsys):
    from backend.manage import main
    decision = engine.tick(frames(), NOW)
    monkeypatch.setattr("sys.argv", ["manage", "--database", engine.store.path, "replay", decision["decision_id"]])
    main()
    assert json.loads(capsys.readouterr().out)["matches"]


def test_calibration_capture_rolls_back_with_failed_decision(engine, monkeypatch):
    def fail(*args):
        raise RuntimeError("simulated commit failure")
    monkeypatch.setattr(engine, "_decision", fail)
    with pytest.raises(RuntimeError, match="simulated"):
        engine.tick(frames(), NOW)
    with engine.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM snapshots").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM trades").fetchone()[0] == 0


def test_replay_uses_captured_policy_not_current_thresholds(engine):
    decision = engine.tick(frames(), NOW)
    strict = Phase2Engine(engine.store, council_policy=replace(CouncilPolicy(), warmup_min_score=99))
    assert strict.replay(decision["decision_id"])["matches"]


@pytest.mark.parametrize("settings", [dict(min_confidence=float("nan")), dict(min_confidence=1.1),
                                      dict(calibration_min_samples=1), dict(calibration_window=29),
                                      dict(allow_demo_warmup=1), dict(warmup_min_score=10)])
def test_invalid_policy_is_rejected(settings):
    with pytest.raises(ValueError):
        CouncilPolicy(**settings)
