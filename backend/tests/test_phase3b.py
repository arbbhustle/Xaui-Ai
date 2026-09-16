"""Deterministic TEST_DATA only: state, causality, isolation and persistence regressions."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from backend.domain import Policy, canonical, parse, stamp
from backend.scoring import Candle
from backend.hidden_state import (HiddenPolicy, STATES, entropy, liquidity, tension, resilience,
                                  event_absorption, classify_state)
from backend.intelligence import IntelligencePolicy, assess_intelligence
from backend.intelligence_providers import normalize_bundle
from backend.slow_context import SlowPolicy, SLOW_KEY, CHANNELS, MAX_AGE, normalize_slow, assess_slow, SlowFeed
from backend.phase3a import Phase3AEngine
from backend.phase3b import Phase3BEngine, SOURCE_FILES, implementation_hash
from backend.phase3b_main import create_phase3b_app
from backend.storage import Store
from backend.gold_analytics import summarize, DIMENSIONS
from backend import gold_shadows
from intelligence_fixtures import enriched, bundle, event
from test_phase1 import NOW, append_minute, active


def slow_fixture(now=NOW):
    return {k:{"status":"OK","provider":"test-"+k,"data_mode":"TEST_DATA","retrieved_at":stamp(now),
               "records":[{"source":"test-source","url":"https://fixtures.invalid/"+k,
                           "observed_at":stamp(now-timedelta(hours=12)),"published_at":stamp(now-timedelta(hours=1)),
                           "score":30,"method":"DETERMINISTIC_TEST_INDEX"}]} for k in CHANNELS}


def make_engine(path,**kwargs):
    return Phase3BEngine(Store(path),intelligence_policy=IntelligencePolicy(allow_fixture_data=True),**kwargs)


@pytest.fixture
def engine(tmp_path):
    return make_engine(tmp_path/"phase3b.sqlite3")


def candles(n=120,step=0,price=100,interval=1):
    return [Candle(t=stamp(NOW.replace(second=0)-timedelta(minutes=(n-i)*interval)),
                   o=price+i*step,h=price+i*step+.5,l=price+i*step-.5,c=price+i*step,v=None) for i in range(n)]


def replace(bar,**kwargs):
    return Candle(**dict(asdict(bar),**kwargs))


def intelligence(data=None):
    return assess_intelligence(normalize_bundle(data or bundle(),NOW),NOW,IntelligencePolicy(allow_fixture_data=True))


def test_phase3b_defaults_and_replay(engine):
    result = engine.tick(enriched(),NOW)
    assert result["strategy_version"] == "phase3b-1"
    assert result["hidden_state"]["state"] in STATES
    assert result["hidden_state"]["score_kind"] == "UNCALIBRATED_INDEX"
    assert result["slow_regime"]["data_mode"] == "UNAVAILABLE"
    assert all(v is None for v in result["slow_regime"]["scores"].values())
    assert engine.replay(result["decision_id"])["matches"]
    assert make_engine(engine.store.path).replay(result["decision_id"])["matches"]


@pytest.mark.parametrize("side",[1,-1])
def test_liquidity_sweeps_and_reclaim(side):
    bars = candles()
    bars[-1] = replace(bars[-1],h=102 if side < 0 else 100.5,l=98 if side > 0 else 99.5,c=100)
    result = liquidity(bars)
    assert result["lower_sweep"] if side > 0 else result["upper_sweep"]
    assert result["reclaim_direction"] == side
    assert result["liquidity_pressure"]*side > 0


@pytest.mark.parametrize("side",[1,-1])
def test_false_breakout_returns_inside_prior_range(side):
    bars = candles()
    bars[-2] = replace(bars[-2],o=100,c=100+side,h=102 if side>0 else 100.5,l=98 if side<0 else 99.5)
    result = liquidity(bars)
    assert result["false_breakout"]
    assert result["reclaim_direction"] == -side


def test_two_sided_sweep_does_not_invent_intrabar_order():
    bars = candles()
    bars[-1] = replace(bars[-1],h=103,l=97)
    assert liquidity(bars)["liquidity_pressure"] == 0


def test_displacement_follow_through():
    bars = candles()
    bars[-2] = replace(bars[-2],c=101,h=101.2)
    bars[-1] = replace(bars[-1],o=101,c=102,h=102.2,l=100.8)
    result = liquidity(bars)
    assert result["displacement"] >= 50
    assert result["follow_through"]


@pytest.mark.parametrize("state,values",[
    ("SHOCK",(True,0,0,0,False,0,0)),("EXHAUSTION",(False,80,0,0,False,0,0)),
    ("EXPANSION",(False,0,80,0,False,0,0)),("FRACTURE",(False,0,0,70,False,0,0)),
    ("LIQUIDITY_SWEEP",(False,0,0,0,True,0,0)),("COMPRESSION",(False,0,0,0,False,80,0)),
    ("ACCUMULATION",(False,0,0,0,False,0,80)),("RANGE",(False,0,0,0,False,0,0))])
def test_all_state_classification_paths(state,values):
    assert classify_state(*values) == state


def test_compression_to_expansion(engine):
    data = enriched()
    rows = data["5min"]
    for row in rows[-7:]:
        row.update(o=4334,c=4334,h=4334.04,l=4333.96)
    first = engine.tick(data,NOW)
    assert first["hidden_state"]["dgfe"]["compression_score"] > 80
    # Re-evaluate the same timestamp in a separate database to isolate feature response.
    rows[-1].update(o=4334,c=4335.7,h=4335.8,l=4333.95)
    with engine.store.connect() as conn:
        snapshot=json.loads(conn.execute("SELECT payload FROM snapshots").fetchone()[0])
    snapshot["frames"]["5min"] = rows
    result = engine.evaluate_snapshot(snapshot,Policy())["hidden_state"]
    assert result["dgfe"]["prior_compression_score"] > 80
    assert result["dgfe"]["expansion_candidate_score"] > first["hidden_state"]["dgfe"]["expansion_candidate_score"]
    assert result["state"] in ("EXPANSION","FRACTURE")


def test_trend_with_rejection_is_exhaustion(engine):
    data = enriched()
    last = data["5min"][-1]
    last.update(o=last["c"],h=last["c"]+1.4,l=last["c"]-.1)
    hidden = engine.tick(data,NOW)["hidden_state"]
    assert hidden["dgfe"]["exhaustion_score"] >= 70
    assert hidden["state"] == "EXHAUSTION"


def test_entropy_distinguishes_trend_from_chop():
    assert entropy(list(range(25)))["score"] == 0
    assert entropy([100+i%2 for i in range(25)])["score"] > 95
    assert entropy([100]*25)["score"] == 100


def test_high_entropy_veto(engine):
    data = enriched()
    for i,row in enumerate(data["5min"]):
        row.update(o=4335+i%2,c=4335+i%2,h=4335.4+i%2,l=4334.6+i%2)
    result = engine.tick(data,NOW)
    assert result["direction"] == "NO_TRADE"
    assert "HIGH_MARKET_ENTROPY" in result["veto_codes"]


def test_timeframe_convergence():
    mixed = {k:{"bias_score":v} for k,v in zip(("1min","5min","15min","1h","4h"),[1,1,-1,-1,0])}
    prior = tension(mixed)
    aligned = {k:{"bias_score":1} for k in mixed}
    result = tension(aligned,prior["score"])
    assert prior["score"] == 100
    assert result["score"] == 0 and result["convergence"] == 100


def test_extreme_timeframe_disagreement_is_vetoed(engine):
    from test_phase1 import frame
    data = enriched()
    for interval in ("15min","1h"):
        data[interval] = frame(interval,down=True)
    result = engine.tick(data,NOW)
    assert result["direction"] == "NO_TRADE"
    assert any("TIMEFRAME" in v for v in result["veto_codes"])


@pytest.mark.parametrize("pressure,gold_step,expected",[(-100,.1,1),(100,-.1,-1),(-100,0,1),(100,0,-1)])
def test_gold_resilience(pressure,gold_step,expected):
    audit = intelligence()
    for key in ("usd_score","yields_score","macro_score","news_sentiment_score"):
        audit["scores"][key] = pressure
    result = resilience(candles(step=gold_step),audit)
    assert result["gold_resilience_score"]*expected > 0
    assert -100 <= result["gold_resilience_score"] <= 100
    assert all(c["resisted_expected_move"] for c in result["components"].values())


def test_resilience_missing_data_is_not_neutral():
    audit = intelligence()
    audit["scores"]["usd_score"] = None
    assert resilience(candles(),audit)["gold_resilience_score"] is None


def test_event_absorption_and_failed_expected_reaction():
    now = NOW.replace(second=0)
    bars = candles()
    start = now-timedelta(minutes=40)
    for i in range(80,83):
        bars[i] = replace(bars[i],l=97,h=100,c=98)
    for i in range(83,120):
        bars[i] = replace(bars[i],o=100,c=101,h=101.2,l=99.9)
    e = event(now,"CPI",-40)
    prior = [{"at":stamp(start-timedelta(minutes=5)),"pressure":-80}]
    result = event_absorption(bars,[e],prior,now,HiddenPolicy())[0]
    assert result["initial_impulse_atr"] < 0
    assert result["recovery_seconds"] == 240
    assert result["failed_expected_reaction"] is True
    assert result["rejection"] == 100
    assert result["unresolved"] is False


def test_event_does_not_borrow_later_expected_pressure():
    now = NOW.replace(second=0)
    e = event(now,"NFP",-40)
    result = event_absorption(candles(),[e],[{"at":stamp(now),"pressure":90}],now,HiddenPolicy())[0]
    assert result["failed_expected_reaction"] is None


def test_event_insufficient_post_history_keeps_shock_unresolved():
    now = NOW.replace(second=0)
    result = event_absorption(candles(),[event(now,"FOMC",-1)],[],now,HiddenPolicy())[0]
    assert result["status"] == "INSUFFICIENT_HISTORY" and result["unresolved"]


@pytest.mark.parametrize("channel",CHANNELS)
def test_stale_slow_observation_veto_even_if_published_today(channel):
    raw = slow_fixture()
    raw[channel]["records"][0]["observed_at"] = stamp(NOW-timedelta(seconds=MAX_AGE[channel]))
    result = assess_slow(normalize_slow(raw,NOW),NOW,SlowPolicy(True))
    assert f"INVALID_SLOW_FRESHNESS:{channel}" in result["vetoes"]
    assert result["scores"][channel] is None


@pytest.mark.parametrize("channel",CHANNELS)
def test_delayed_publication_must_be_known_before_decision(channel):
    raw = slow_fixture()
    raw[channel]["records"][0]["published_at"] = stamp(NOW+timedelta(seconds=1))
    result = assess_slow(normalize_slow(raw,NOW),NOW,SlowPolicy(True))
    assert f"FUTURE_SLOW_DATA:{channel}" in result["vetoes"]
    assert result["scores"][channel] is None


def test_slow_test_data_requires_permission_and_cannot_mix_with_real_gold(engine,tmp_path):
    data = enriched()
    data[SLOW_KEY] = slow_fixture()
    assert "SLOW_TEST_DATA_DISABLED" in engine.tick(data,NOW)["veto_codes"]
    data["__market_provenance__"]["data_mode"] = "LIVE"
    result = make_engine(tmp_path/"other.sqlite3",slow_policy=SlowPolicy(True)).tick(data,NOW)
    assert "SLOW_TEST_DATA_IN_REAL_MARKET" in result["veto_codes"]


def test_missing_slow_is_optional_but_failed_provider_is_not():
    result = assess_slow(normalize_slow({},NOW),NOW,SlowPolicy())
    assert not result["vetoes"]
    raw = {"cot":{"status":"UNAVAILABLE","provider":"configured-provider"}}
    assert "INVALID_SLOW_FRESHNESS:cot" in assess_slow(normalize_slow(raw,NOW),NOW,SlowPolicy())["vetoes"]


def test_slow_provider_acquisition_provenance_and_failure_redaction():
    class TestProvider:
        name = "test-provider"
        data_mode = "TEST_DATA"
        def fetch(self,now):
            return dict(slow_fixture()["cot"],data_mode="LIVE")
    result = SlowFeed({"cot":TestProvider()},clock=lambda:NOW).fetch(NOW)
    assert result["cot"]["data_mode"] == "TEST_DATA"
    assert result["etf"]["status"] == "UNAVAILABLE"


def test_shadow_strategies_do_not_change_production_or_calibration(tmp_path):
    enabled = make_engine(tmp_path/"enabled.sqlite3")
    disabled = make_engine(tmp_path/"disabled.sqlite3",shadow_enabled=False)
    data = enriched()
    one,two = enabled.tick(data,NOW),disabled.tick(data,NOW)
    assert one == two
    assert len(gold_shadows.VARIANTS) == 3
    for minute in (0,1):
        append_minute(data,NOW.replace(second=0)+timedelta(minutes=minute))
    assert enabled.tick(data,NOW+timedelta(minutes=2)) == disabled.tick(data,NOW+timedelta(minutes=2))
    assert active(enabled) == active(disabled)
    with enabled.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM phase3b_shadows").fetchone()[0] > 0
    with disabled.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM phase3b_shadows").fetchone()[0] == 0


def test_shadow_outcome_is_chronological_persistent_and_not_a_main_trade(engine):
    data = enriched()
    decision = engine.tick(data,NOW)
    for minute in (0,1):
        append_minute(data,NOW.replace(second=0)+timedelta(minutes=minute))
    engine.tick(data,NOW+timedelta(minutes=2))
    append_minute(data,NOW.replace(second=0)+timedelta(minutes=2),low=4320)
    engine.tick(data,NOW+timedelta(minutes=3))
    restarted = make_engine(engine.store.path)
    stats = restarted.analytics()
    assert stats["shadow_demo"]["TREND_CONTINUATION"]["overall"]["sample_count"] == 1
    assert stats["production_demo"]["overall"]["sample_count"] == 1
    assert restarted.replay(decision["decision_id"])["matches"]


def test_shadow_missing_minute_does_not_invent_fill(engine):
    data = enriched()
    engine.tick(data,NOW)
    append_minute(data,NOW.replace(second=0)+timedelta(minutes=2),low=4320)
    engine.tick(data,NOW+timedelta(minutes=3))
    with engine.store.connect() as conn:
        trade = json.loads(conn.execute("SELECT payload FROM phase3b_shadows").fetchone()[0])
    assert trade["status"] == "PENDING" and trade["checkpoint"] is None


def test_analytics_metrics_and_all_strata():
    rows = [{"id":str(i),"status":"CLOSED","closed_at":str(i),"r_multiple":r,
             "analytics_context":{k:"sample" for k in DIMENSIONS}} for i,r in enumerate([1,-2,3,-1])]
    result = summarize(rows)
    metrics = result["overall"]
    assert metrics["sample_count"] == 4 and metrics["win_rate"] == .5
    assert metrics["expectancy"] == metrics["average_r"] == .25
    assert metrics["profit_factor"] == pytest.approx(4/3)
    assert metrics["max_drawdown_r"] == 2
    assert set(result["by"]) == set(DIMENSIONS)
    assert summarize([])["overall"]["win_rate"] is None


@pytest.mark.parametrize("interval",["1min","5min","15min","1h","4h"])
def test_future_gold_data_cannot_enter_features(engine,interval):
    data = enriched()
    row = dict(data[interval][-1],t=stamp(NOW.replace(second=0)+timedelta(days=1)))
    data[interval].append(row)
    result = engine.tick(data,NOW)
    assert result["direction"] == "NO_TRADE"
    assert result["hidden_state"]["state"] is None


def test_forming_candle_does_not_leak_into_features(engine):
    data = enriched()
    first = engine.tick(data,NOW)
    with engine.store.connect() as conn:
        snap = json.loads(conn.execute("SELECT payload FROM snapshots").fetchone()[0])
    snap["frames"]["1min"].append(dict(t=stamp(NOW.replace(second=0)),o=4000,h=5000,l=3000,c=5000))
    second = engine.evaluate_snapshot(snap,Policy())
    assert first["hidden_state"] == second["hidden_state"]


def test_source_hash_portability_and_full_replay_integrity(engine,tmp_path):
    result = engine.tick(enriched(),NOW)
    root = Path(__file__).parents[1]
    for name in SOURCE_FILES:
        (tmp_path/name).write_bytes((root/name).read_text(encoding="utf-8").replace("\n","\r\n").encode())
    assert implementation_hash(tmp_path) == engine.code_hash
    with engine.store.transaction() as conn:
        conn.execute("UPDATE decisions SET payload=json_set(payload,'$.hidden_state.state','SHOCK')")
    with pytest.raises(ValueError,match="checksum"):
        engine.replay(result["decision_id"])


def test_phase3a_database_is_preserved(tmp_path):
    store = Store(tmp_path/"old.sqlite3")
    Phase3AEngine(store,intelligence_policy=IntelligencePolicy(allow_fixture_data=True)).tick(enriched(),NOW)
    with store.connect() as conn:
        before = list(conn.iterdump())
    with pytest.raises(RuntimeError,match="separate demo database"):
        make_engine(store.path)
    with store.connect() as conn:
        assert list(conn.iterdump()) == before


def test_phase3b_refuses_render_and_protected_paths(monkeypatch):
    with pytest.raises(ValueError,match="own demo database"):
        create_phase3b_app(db_path="backend/data/phase3a.sqlite3")
    monkeypatch.setenv("RENDER","true")
    with pytest.raises(RuntimeError,match="local-only"):
        create_phase3b_app()


def test_api_exposes_separate_analytics_and_checks_slow_freshness(tmp_path):
    clock = [NOW]
    app = create_phase3b_app(db_path=tmp_path/"api.sqlite3",start_worker=False,clock=lambda:clock[0],
                           intelligence_policy=IntelligencePolicy(allow_fixture_data=True),slow_policy=SlowPolicy(True))
    with TestClient(app) as client:
        data = enriched()
        data[SLOW_KEY] = slow_fixture()
        app.state.engine.tick(data,NOW)
        assert client.get("/signal").json()["slow_regime"]["data_mode"] == "TEST_DATA"
        assert set(client.get("/gold/analytics").json()["shadow_demo"]) == set(gold_shadows.VARIANTS)
        assert client.get("/gold/shadows").json()["mode"] == "SHADOW_DEMO_ONLY"
        clock[0] += timedelta(days=2)
        result = client.get("/signal").json()
        assert result["direction"] == "NO_TRADE"
        assert "INVALID_SLOW_FRESHNESS:options" in result["veto_codes"]
