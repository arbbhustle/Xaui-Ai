"""Forensic regressions for failure isolation, state history and point-in-time data."""
from copy import deepcopy
from datetime import timedelta
import json
import threading

import pytest
from backend.domain import Policy, parse, stamp
from backend.hidden_state import HiddenPolicy, event_absorption, liquidity, macro_pressure
from backend.slow_context import SLOW_KEY, SlowPolicy, normalize_slow, assess_slow, SlowFeed
from backend import gold_shadows
from backend.storage import Store
from test_phase3b import make_engine, candles, replace, intelligence, slow_fixture
from test_phase1 import NOW, append_minute, active
from intelligence_fixtures import enriched, event


def test_prior_sweep_invalidated_by_subsequent_breakout():
    bars = candles()
    bars[-2] = replace(bars[-2],h=102,c=100)
    bars[-1] = replace(bars[-1],o=100,c=103,h=103.5,l=99.5)
    result = liquidity(bars)
    assert not result["upper_sweep"] and result["reclaim_direction"] == 0


def test_shadow_failure_rolls_back_only_shadow_work(tmp_path,monkeypatch,caplog):
    engine = make_engine(tmp_path/"failure.sqlite3")
    original = gold_shadows.create
    def fail(*args):
        original(*args)
        raise RuntimeError("NON_SECRET_SENTINEL_MUST_NOT_BE_LOGGED")
    monkeypatch.setattr(gold_shadows,"create",fail)
    result = engine.tick(enriched(),NOW)
    assert active(engine) is not None
    with engine.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM decisions").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM phase3b_shadows").fetchone()[0] == 0
        assert Store.get_state(conn,"phase3b_shadow_health")["status"] == "ERROR"
    assert "NON_SECRET_SENTINEL" not in caplog.text
    assert engine.replay(result["decision_id"])["matches"]


def test_all_three_shadow_evaluations_are_persisted_even_when_no_setup(tmp_path):
    engine = make_engine(tmp_path/"variants.sqlite3")
    engine.tick(enriched(),NOW)
    with engine.store.connect() as conn:
        rows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM phase3b_shadow_decisions")]
    assert {r["variant"] for r in rows} == set(gold_shadows.VARIANTS)
    assert any(r["direction"] == "NO_TRADE" for r in rows)


def test_stale_slow_context_does_not_freeze_known_exit(tmp_path):
    engine = make_engine(tmp_path/"exits.sqlite3",slow_policy=SlowPolicy(True))
    data = enriched()
    data[SLOW_KEY] = slow_fixture()
    engine.tick(data,NOW)
    for minute in (0,1):
        append_minute(data,NOW.replace(second=0)+timedelta(minutes=minute))
    engine.tick(data,NOW+timedelta(minutes=2))
    assert active(engine)["status"] == "OPEN"
    data[SLOW_KEY]["options"]["records"][0]["observed_at"] = stamp(NOW-timedelta(days=2))
    append_minute(data,NOW.replace(second=0)+timedelta(minutes=2),low=4320)
    result = engine.tick(data,NOW+timedelta(minutes=3))
    assert "INVALID_SLOW_FRESHNESS:options" in result["veto_codes"]
    assert active(engine) is None
    with engine.store.connect() as conn:
        assert Store.summary(conn)["losses"] == 1


def test_unstable_state_history_veto_and_future_history_exclusion(tmp_path):
    engine = make_engine(tmp_path/"unstable.sqlite3")
    decision = engine.tick(enriched(),NOW)
    with engine.store.connect() as conn:
        snapshot=json.loads(conn.execute("SELECT payload FROM snapshots").fetchone()[0])
    states = ["SHOCK","COMPRESSION","EXHAUSTION"]
    snapshot["hidden_history"] = [{"at":stamp(NOW-timedelta(minutes=15-i*5)),
        "candle_close":stamp(NOW.replace(second=0)-timedelta(minutes=15-i*5)),"state":state,"tension":30}
        for i,state in enumerate(states)]
    assert "UNSTABLE_HIDDEN_STATE" in engine.evaluate_snapshot(snapshot,Policy())["veto_codes"]
    for row in snapshot["hidden_history"]:
        row["at"] = stamp(NOW+timedelta(seconds=1))
    assert "UNSTABLE_HIDDEN_STATE" not in engine.evaluate_snapshot(snapshot,Policy())["veto_codes"]


def test_macro_context_at_blackout_is_usable_but_never_from_stale_source():
    audit = intelligence()
    audit["vetoes"] = ["MAJOR_EVENT_IMMINENT"]
    assert macro_pressure(audit) is not None
    audit["vetoes"].append("STALE_INTELLIGENCE:usd")
    assert macro_pressure(audit) is None


def test_event_gap_before_release_does_not_invent_initial_price():
    now = NOW.replace(second=0)
    bars = candles()
    del bars[79]
    result = event_absorption(bars,[event(now,"CPI",-40)],[],now,HiddenPolicy())[0]
    assert result["initial_impulse_atr"] is None and result["unresolved"]


def test_event_features_ignore_future_candles():
    now = NOW.replace(second=0)
    bars = candles()
    e = event(now,"PCE",-40)
    before = event_absorption(bars,[e],[],now,HiddenPolicy())
    bars.append(replace(bars[-1],t=stamp(now),h=10000,c=9999))
    assert event_absorption(bars,[e],[],now,HiddenPolicy()) == before


def test_shadow_same_bar_stop_target_uses_stop_first(tmp_path):
    engine = make_engine(tmp_path/"ambiguous.sqlite3")
    data = enriched()
    engine.tick(data,NOW)
    for minute in (0,1):
        append_minute(data,NOW.replace(second=0)+timedelta(minutes=minute),low=4310,high=4360)
    engine.tick(data,NOW+timedelta(minutes=2))
    with engine.store.connect() as conn:
        row=json.loads(conn.execute("SELECT payload FROM phase3b_shadows WHERE variant='TREND_CONTINUATION'").fetchone()[0])
    assert row["status"] == "CLOSED" and row["result"] == "SL"
    assert row["ambiguity"] and row["r_multiple"] == pytest.approx(-1)


def test_shadow_monitor_rejects_changed_market_provenance(tmp_path):
    engine = make_engine(tmp_path/"mode.sqlite3")
    data = enriched()
    engine.tick(data,NOW)
    data["__market_provenance__"]["data_mode"] = "LIVE"
    for minute in (0,1):
        append_minute(data,NOW.replace(second=0)+timedelta(minutes=minute))
    engine.tick(data,NOW+timedelta(minutes=2))
    with engine.store.connect() as conn:
        row=json.loads(conn.execute("SELECT payload FROM phase3b_shadows").fetchone()[0])
    assert row["status"] == "PENDING" and row["checkpoint"] is None


def test_later_macro_revisions_do_not_change_old_decision_replay(tmp_path):
    engine = make_engine(tmp_path/"revisions.sqlite3")
    old = engine.tick(enriched(),NOW)
    later = NOW+timedelta(minutes=5)
    data = enriched(later)
    for row in data["__intelligence__"]["usd"]["records"]:
        if row["id"].endswith("latest"):
            row["value"] = 101
    engine.tick(data,later)
    assert engine.replay(old["decision_id"])["matches"]


def test_slow_conflicting_sources_and_nan_are_fail_closed():
    data = slow_fixture()
    first = data["cot"]["records"][0]
    first["score"] = 90
    data["cot"]["records"].append(dict(first,source="other",score=-90))
    assert "CONFLICTING_SLOW_DATA:cot" in assess_slow(normalize_slow(data,NOW),NOW,SlowPolicy(True))["vetoes"]
    first["score"] = float("nan")
    assert "INVALID_SLOW_FRESHNESS:cot" in assess_slow(normalize_slow(data,NOW),NOW,SlowPolicy(True))["vetoes"]


def test_slow_same_time_revisions_have_no_arbitrary_winner():
    data = slow_fixture()
    first = data["cot"]["records"][0]
    data["cot"]["records"].append(dict(first,score=-first["score"]))
    assert "CONFLICTING_SLOW_DATA:cot" in assess_slow(normalize_slow(data,NOW),NOW,SlowPolicy(True))["vetoes"]


def test_slow_timeout_is_bounded_and_single_inflight():
    released = threading.Event()
    calls=[]
    class Slow:
        name="test-slow"
        data_mode="TEST_DATA"
        def fetch(self,now):
            calls.append(1)
            released.wait(2)
            return slow_fixture()["cot"]
    feed = SlowFeed({"cot":Slow()},clock=lambda:NOW,timeout_seconds=.01)
    try:
        for _ in range(2):
            assert feed.fetch(NOW)["cot"]["status"] == "UNAVAILABLE"
        assert len(calls) == 1
    finally:
        released.set()


def test_slow_scores_are_background_only(tmp_path):
    results=[]
    for index,score in enumerate((-100,100)):
        data = enriched()
        data[SLOW_KEY]=slow_fixture()
        for channel in data[SLOW_KEY].values():
            channel["records"][0]["score"]=score
        engine=make_engine(tmp_path/f"background-{index}.sqlite3",slow_policy=SlowPolicy(True))
        results.append(engine.tick(data,NOW))
    for field in ("direction","buy_score","sell_score","confidence","hidden_state"):
        assert results[0][field] == results[1][field]


def test_shadows_never_feed_production_calibration(tmp_path):
    engine=make_engine(tmp_path/"calibration.sqlite3")
    first=engine.tick(enriched(),NOW)
    with engine.store.transaction() as conn:
        rows=conn.execute("SELECT payload FROM phase3b_shadows").fetchall()
        for row in rows:
            trade=json.loads(row[0]);trade.update(status="CLOSED",r_multiple=100,closed_at=stamp(NOW))
            conn.execute("UPDATE phase3b_shadows SET status='CLOSED',payload=? WHERE id=?",(json.dumps(trade),trade["id"]))
    next_at=NOW+timedelta(minutes=5)
    result=engine.tick(enriched(next_at),next_at)
    assert result["calibration"]["sample_count"] == 0


@pytest.mark.parametrize("bad",[{"entropy_veto":float("nan")},{"shock_atr":0},{"unstable_transitions":1},{"unstable_transitions":4},{"event_resolution_minutes":1}])
def test_invalid_hidden_settings_rejected(bad):
    with pytest.raises(ValueError):
        HiddenPolicy(**bad)


def test_one_minute_shock_is_visible_before_next_five_minute_close(tmp_path):
    engine=make_engine(tmp_path/"minute-shock.sqlite3")
    data=enriched()
    data["1min"][-1]["h"] += 10
    result=engine.tick(data,NOW)
    assert result["hidden_state"]["state"] == "SHOCK"
    assert "1min" in result["hidden_state"]["shock_timeframes"]
    assert "MARKET_SHOCK" in result["veto_codes"]


def test_recent_event_keeps_unresolved_shock_veto(tmp_path):
    engine=make_engine(tmp_path/"event-shock.sqlite3")
    data=enriched()
    data["__intelligence__"]["calendar"]["records"]=[event(NOW.replace(second=0),"CPI",-10)]
    result=engine.tick(data,NOW)
    assert result["hidden_state"]["state"] == "SHOCK"
    assert "UNRESOLVED_EVENT_SHOCK" in result["veto_codes"]
    assert result["direction"] == "NO_TRADE"


def test_conflicting_macro_technical_does_not_invent_reversal(tmp_path):
    engine=make_engine(tmp_path/"opposing.sqlite3")
    data=enriched()
    for channel in ("usd","yields"):
        for row in data["__intelligence__"][channel]["records"]:
            if row["id"].endswith("latest"):
                row["value"] += 1
    data["__intelligence__"]["macro"]["records"][0]["expected_rate"] = 5
    data["__intelligence__"]["news"]["records"][0]["title"] = "Gold falls as dollar rises"
    result=engine.tick(data,NOW)
    assert result["technical_scores"]["buy_score"] > result["technical_scores"]["sell_score"]
    assert result["hidden_state"]["resilience"]["gold_resilience_score"] > 0
    assert result["direction"] in ("BUY","NO_TRADE")


def test_shadow_scheduled_weekend_gap_cancels_expired_order(tmp_path):
    engine=make_engine(tmp_path/"weekend.sqlite3")
    data=enriched()
    engine.tick(data,NOW)
    friday=parse("2026-09-18T21:00:00+00:00")
    sunday=parse("2026-09-20T22:00:00+00:00")
    with engine.store.transaction() as conn:
        row=conn.execute("SELECT payload FROM phase3b_shadows").fetchone()
        trade=json.loads(row[0])
        trade.update(eligible_from=stamp(friday),expires_at=stamp(friday+timedelta(minutes=5)))
        conn.execute("UPDATE phase3b_shadows SET payload=? WHERE id=?",(json.dumps(trade),trade["id"]))
    append_minute(data,sunday)
    engine.tick(data,sunday+timedelta(minutes=1))
    with engine.store.connect() as conn:
        updated=json.loads(conn.execute("SELECT payload FROM phase3b_shadows WHERE id=?",(trade["id"],)).fetchone()[0])
    assert updated["status"] == "CANCELLED" and updated["result"] == "EXPIRED"


def test_sustained_directional_advance_is_not_labelled_range(tmp_path):
    engine=make_engine(tmp_path/"continuation.sqlite3")
    hidden=engine.tick(enriched(),NOW)["hidden_state"]
    assert hidden["state"] == "EXPANSION"
    assert hidden["dgfe"]["expansion_candidate_score"] >= 65


def test_synthetic_slow_envelope_marker_cannot_be_promoted_live():
    data=slow_fixture()
    data["cot"].update(data_mode="LIVE",is_synthetic=True)
    assert normalize_slow(data,NOW)["cot"]["data_mode"] == "TEST_DATA"


def test_test_data_macro_record_cannot_be_promoted_live():
    from backend.intelligence_providers import normalize_bundle
    from intelligence_fixtures import bundle
    data=bundle()
    data["news"]["data_mode"]="LIVE"
    data["news"]["records"][0]["data_mode"]="TEST_DATA"
    assert normalize_bundle(data,NOW)["news"]["data_mode"] == "FIXTURE"


@pytest.mark.parametrize("mode",["FIXTURE","TEST_DATA"])
def test_marked_market_payload_cannot_be_promoted_by_adapter(mode):
    from backend.intelligence_providers import IntelligenceFeed,IntelligenceMarketFeed,MARKET_KEY
    class Market:
        name="contract-test-market"
        data_mode="LIVE"
        def fetch(self,now):
            data=enriched()
            data[MARKET_KEY]["data_mode"]=mode
            return data,{}
    frames,_=IntelligenceMarketFeed(Market(),IntelligenceFeed(clock=lambda:NOW)).fetch(NOW)
    assert frames[MARKET_KEY]["data_mode"] == "FIXTURE"


@pytest.mark.parametrize("marker",["true",1,None])
def test_invalid_synthetic_marker_fails_closed(marker):
    raw=slow_fixture()
    raw["cot"].update(data_mode="LIVE",is_synthetic=marker)
    result=normalize_slow(raw,NOW)
    assert result["cot"]["status"] == "UNAVAILABLE"


def test_registered_test_adapter_cannot_promote_live_news():
    from backend.intelligence_providers import IntelligenceFeed
    from intelligence_fixtures import bundle
    class Provider:
        name="contract-test-news"
        data_mode="TEST_DATA"
        def fetch(self,now):
            return dict(bundle()["news"],data_mode="LIVE")
    assert IntelligenceFeed({"news":Provider()},clock=lambda:NOW).fetch(NOW)["news"]["data_mode"] == "FIXTURE"


def test_resilience_does_not_compare_weekend_move_to_one_hour_pressure():
    from backend.hidden_state import resilience
    bars=candles()
    bars[-1]=replace(bars[-1],t=stamp(parse(bars[-1].t)+timedelta(days=2)))
    assert resilience(bars,intelligence())["gold_resilience_score"] is None


def test_resilience_uses_configured_macro_momentum_horizon():
    from backend.hidden_state import resilience
    assert resilience(candles(),intelligence(),7200)["gold_resilience_score"] is None
    result=resilience(candles(n=121),intelligence(),7200)
    assert (parse(result["window_end"])-parse(result["window_start"])).total_seconds() == 7200
