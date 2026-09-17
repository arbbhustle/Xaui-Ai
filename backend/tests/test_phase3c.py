"""Offline causal meta-council fixtures; no live or fabricated provider data."""
from copy import deepcopy
from dataclasses import asdict,replace
from datetime import timedelta
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.domain import canonical,digest,parse,stamp
from backend.intelligence import IntelligencePolicy
from backend.storage import Store
from backend.phase3b import Phase3BEngine
from backend.phase3c import Phase3CEngine,BASE_HASH,SOURCE_FILES,implementation_hash
from backend.phase3c_main import create_phase3c_app
from backend.meta_council import (BASELINE,MetaPolicy,DemoCosts,evaluate_meta,walk_forward,trust,independent,
                                 select_history,transition,calibration,bounded_weights,votes)
from backend.meta_evaluation import performance,promotion_report
from intelligence_fixtures import enriched
from test_phase1 import NOW,append_minute,active


@pytest.fixture
def champion(tmp_path):
    return Phase3BEngine(Store(tmp_path/"champion.sqlite3"),intelligence_policy=IntelligencePolicy(allow_fixture_data=True)).tick(enriched(),NOW)


def sample(champion,index=0,**changes):
    opened=NOW-timedelta(days=150-index)
    r={"id":f"outcome-{index}","namespace":"test","source":"CHAMPION","decision_id":f"past-{index}",
       "status":"CLOSED","opened_at":stamp(opened),"closed_at":stamp(opened+timedelta(hours=1)),
       "known_at":stamp(opened+timedelta(hours=1,minutes=1)),"gross_r":1.0,"net_r":.8,"raw_score":90,
       "regime":dict(champion["analytics_context"]),"votes":votes(champion),"prediction":.75,
       "prediction_at":stamp(opened-timedelta(minutes=1)),"prediction_target":"POSITIVE_NET_SIMULATED_R",
       "leakage_audit_passed":True}
    r.update(changes)
    return r


def inputs(champion,history=None,**changes):
    result={"at":stamp(NOW),"feature_start":stamp(NOW-timedelta(days=20)),"namespace":"test",
            "champion":champion,"history":history or [],"weights_previous":dict(BASELINE),
            "transition_memory":{},"policy":asdict(MetaPolicy()),"integrity_errors":[]}
    result.update(changes)
    return result


def engine(path,**kwargs):
    return Phase3CEngine(Store(path),intelligence_policy=IntelligencePolicy(allow_fixture_data=True),**kwargs)


def seed_history(e,champion,n=80):
    namespace=e.namespace(champion)
    with e.store.transaction() as conn:
        for i in range(n):
            row=sample(champion,i,namespace=namespace)
            conn.execute("INSERT INTO meta_outcomes VALUES (?,?,?,?,?)",(row["id"],namespace,row["known_at"],canonical(row),digest(row)))


def test_empty_history_falls_back_without_fake_probability(champion):
    result=evaluate_meta(inputs(champion))
    assert result["routing"]=={"active":"PHASE3B_CHAMPION","fallback":True,"automatic_promotion":False}
    assert result["challenger"]["direction"]=="NO_TRADE"
    assert result["calibration"]["probability"] is None
    assert result["uncertainty_level"]==100
    assert result["adaptive_influence"]==0
    assert result["champion"]==champion


def test_prior_closed_evidence_can_enable_only_separate_challenger(champion):
    result=evaluate_meta(inputs(champion,[sample(champion,i) for i in range(80)]))
    assert result["challenger"]["direction"]=="BUY"
    assert not result["routing"]["fallback"]
    assert result["routing"]["active"]=="PHASE3B_CHAMPION"
    assert result["calibration"]["source"]=="CHAMPION_PROXY_EXACT_REGIME"
    assert result["effective_sample_size"]==80
    assert set(result["weights_before_decision"])==set(BASELINE)


@pytest.mark.parametrize("defect",["future_known","future_close","same_decision","open","wrong_namespace","overlap_window","embargo"])
def test_ineligible_outcome_never_trains(champion,defect):
    row=sample(champion)
    cutoff=NOW-timedelta(days=20)
    if defect=="future_known": row["known_at"]=stamp(NOW+timedelta(seconds=1))
    if defect=="future_close": row.update(closed_at=stamp(NOW+timedelta(seconds=1)),known_at=stamp(NOW+timedelta(seconds=2)))
    if defect=="same_decision": row["decision_id"]=champion["decision_id"]
    if defect=="open": row["status"]="OPEN"
    if defect=="wrong_namespace": row["namespace"]="different"
    if defect=="overlap_window": row.update(opened_at=stamp(cutoff-timedelta(hours=2)),closed_at=stamp(cutoff+timedelta(minutes=1)),known_at=stamp(cutoff+timedelta(minutes=2)))
    if defect=="embargo": row.update(closed_at=stamp(cutoff-timedelta(minutes=30)),known_at=stamp(cutoff-timedelta(minutes=29)))
    result=evaluate_meta(inputs(champion,[row]))
    assert result["historical_sample_ids"]==[]
    assert result["effective_sample_size"]==0


def test_future_values_and_future_regime_labels_do_not_change_past(champion):
    row=sample(champion,known_at=stamp(NOW+timedelta(days=1)))
    old=evaluate_meta(inputs(champion,[row]))
    row.update(net_r=float("nan"),regime={"corrupt_future":"ignored"})
    assert evaluate_meta(inputs(champion,[row]))==old


def test_overlapping_samples_do_not_inflate_effective_evidence(champion):
    rows=[sample(champion,0,id=str(i)) for i in range(80)]
    result=evaluate_meta(inputs(champion,rows))
    assert result["evidence_sample_count"]==80
    assert result["effective_sample_size"]==1
    assert result["trust"]["technical"]["sample_count"]==1
    assert result["routing"]["fallback"]


@pytest.mark.parametrize("axis",["hidden_state","session","volatility_regime","macro_regime","timeframe_alignment","direction"])
def test_trust_does_not_transfer_across_regimes(champion,axis):
    rows=[sample(champion,i) for i in range(80)]
    for r in rows:
        r["regime"][axis]="SELL" if axis=="direction" else "DIFFERENT"
    result=evaluate_meta(inputs(champion,rows))
    assert result["trust"]["technical"]["strength"]==0
    assert result["weights_before_decision"]==BASELINE
    assert result["routing"]["fallback"]


@pytest.mark.parametrize("n",[1,5,29])
def test_small_samples_shrink_and_cannot_move_weights(champion,n):
    result=evaluate_meta(inputs(champion,[sample(champion,i) for i in range(n)]))
    assert result["trust"]["technical"]["trust_index"]<100
    assert result["trust"]["technical"]["strength"]==0
    assert result["weights_before_decision"]==BASELINE


def test_shadow_trust_requires_own_closed_minimum(champion):
    rows=[sample(champion,i,source="SWEEP_RECLAIM") for i in range(29)]
    report,_=trust(rows,champion["analytics_context"],MetaPolicy())
    assert not report["SWEEP_RECLAIM"]["eligible"]
    rows.append(sample(champion,29,source="SWEEP_RECLAIM"))
    report,_=trust(rows,champion["analytics_context"],MetaPolicy())
    assert report["SWEEP_RECLAIM"]["eligible"]
    assert report["technical"]["sample_count"]==0


def test_gradual_bounded_weights_and_weak_evidence_reversion(champion):
    previous=dict(BASELINE)
    for _ in range(20):
        target={k:v*(1.8 if k=="technical" else .8) for k,v in BASELINE.items()}
        current=bounded_weights(target,previous,MetaPolicy())
        assert sum(current.values())==pytest.approx(1)
        assert all(0<=current[k]<=.35 and abs(current[k]-previous[k])<=.005000001 for k in BASELINE)
        previous=current
    output=evaluate_meta(inputs(champion,weights_previous=previous))
    assert abs(output["weights_before_decision"]["technical"]-BASELINE["technical"])<abs(previous["technical"]-BASELINE["technical"])


@pytest.mark.parametrize("source",["CHAMPION","ADAPTIVE_CHALLENGER","TREND_CONTINUATION","SWEEP_RECLAIM","RESILIENCE_DIVERGENCE"])
def test_drift_is_detected_for_every_cohort_and_falls_back(champion,source):
    rows=[sample(champion,i,source=source,net_r=1 if i<60 else -1) for i in range(80)]
    result=evaluate_meta(inputs(champion,rows))
    assert result["drift"][source]["status"]=="DRIFT"
    assert "DRIFT_FALLBACK" in result["challenger"]["vetoes"]
    assert result["adaptive_influence"]==0


def test_transition_uses_only_prior_counts():
    first,memory=transition({},"COMPRESSION")
    second,memory=transition(memory,"EXPANSION")
    assert second["transition_count"]==0
    assert memory["counts"]["COMPRESSION>EXPANSION"]==1
    _,memory=transition(memory,"COMPRESSION")
    third,_=transition(memory,"EXPANSION")
    assert third["transition_count"]==1
    assert third["previous_state"]=="COMPRESSION" and third["current_state"]=="EXPANSION"


def test_calibration_beta_shrinkage_and_pooled_fallback(champion):
    rows=[sample(champion,i,net_r=1 if i<48 else -1) for i in range(60)]
    result=calibration(rows,champion["analytics_context"],90,MetaPolicy())
    assert result["probability"]==pytest.approx(50/64)
    assert result["sample_count"]==60
    for r in rows[:40]:r["regime"]["hidden_state"]="RANGE"
    pooled=calibration(rows,champion["analytics_context"],90,MetaPolicy())
    assert "POOLED" in pooled["source"] and pooled["sample_count"]==60
    assert pooled["probability"]==result["probability"]


def test_challenger_own_calibration_preferred_when_supported(champion):
    rows=[sample(champion,i,source="ADAPTIVE_CHALLENGER") for i in range(60)]
    result=calibration(rows,champion["analytics_context"],90,MetaPolicy())
    assert result["source"]=="CHALLENGER_EXACT_REGIME"


def test_costs_are_explicit_and_do_not_rewrite_gross():
    costs=DemoCosts(.3,.1,.2)
    assert costs.net(1,2)==pytest.approx(.65)
    assert costs.net(-1,2)==pytest.approx(-1.35)
    assert DemoCosts(0,0,0).net(1,2)==1


def test_brier_reliability_and_net_expectancy(champion):
    rows=[sample(champion,i,net_r=1 if i%4!=3 else -1,prediction=.75) for i in range(100)]
    result=performance(rows)
    assert result["brier_score"]==pytest.approx(.1875)
    assert result["calibration_error"]==0
    assert result["hit_rate"]==.75 and result["net_expectancy_r"]==.5
    assert result["gross_expectancy_r"]==1
    rows[0]["prediction_at"]=rows[0]["closed_at"]
    assert performance(rows)["calibration_sample_count"]==99


def test_promotion_is_manual_and_cannot_pass_with_narrow_or_corrupt_history(champion):
    rows=[]
    for i in range(120):
        r=sample(champion,i,source="ADAPTIVE_CHALLENGER",net_r=1 if i%4!=3 else -.2)
        r["regime"].update(hidden_state=("RANGE","EXPANSION","COMPRESSION")[i%3],session=("LONDON","NEW_YORK")[i%2])
        rows.append(r)
    report=promotion_report(rows)
    assert report["status"]=="PROMOTION_ELIGIBLE_FOR_HUMAN_REVIEW"
    assert report["active_strategy"]=="PHASE3B_CHAMPION" and not report["automatic_promotion"]
    assert promotion_report(rows,False)["status"]=="PROMOTION_INELIGIBLE"
    assert promotion_report(rows[:10])["status"]=="PROMOTION_INELIGIBLE"
    for r in rows:r["regime"]["hidden_state"]="RANGE"
    assert promotion_report(rows)["status"]=="PROMOTION_INELIGIBLE"


def test_walk_forward_is_strict_and_future_outcomes_cannot_change_earlier_fold(champion):
    first={"at":stamp(NOW),"feature_start":stamp(NOW-timedelta(days=20)),"champion":champion}
    second=deepcopy(first);second["at"]=stamp(NOW+timedelta(days=1));second["champion"]["decision_id"]="next"
    history=[sample(champion,i) for i in range(80)]
    result=walk_forward([first,second],history,namespace="test")
    history.append(sample(champion,81,known_at=stamp(NOW+timedelta(days=2)),net_r=1000))
    assert [r["result"] for r in walk_forward([first,second],history,namespace="test")]==[r["result"] for r in result]
    with pytest.raises(ValueError,match="chronological"):
        walk_forward([second,first],history)


@pytest.mark.parametrize("corrupt",["duplicate","nan","timestamp"])
def test_corrupted_history_fails_closed(champion,corrupt):
    rows=[sample(champion)]
    if corrupt=="duplicate":rows.append(deepcopy(rows[0]))
    if corrupt=="nan":rows[0]["net_r"]=float("nan")
    if corrupt=="timestamp":rows[0]["closed_at"]=rows[0]["opened_at"]
    result=evaluate_meta(inputs(champion,rows))
    assert "CORRUPT_HISTORY" in result["challenger"]["vetoes"]
    assert result["adaptive_influence"]==0 and result["challenger"]["direction"]=="NO_TRADE"


def test_champion_identical_with_companion_enabled_disabled_and_plain_base(tmp_path):
    enabled=engine(tmp_path/"enabled.sqlite3")
    disabled=engine(tmp_path/"disabled.sqlite3",meta_enabled=False)
    base=Phase3BEngine(Store(tmp_path/"plain.sqlite3"),intelligence_policy=IntelligencePolicy(allow_fixture_data=True))
    data=enriched()
    assert enabled.tick(data,NOW)==disabled.tick(data,NOW)==base.tick(data,NOW)
    for minute in (0,1):append_minute(data,NOW.replace(second=0)+timedelta(minutes=minute))
    assert enabled.tick(data,NOW+timedelta(minutes=2))==disabled.tick(data,NOW+timedelta(minutes=2))==base.tick(data,NOW+timedelta(minutes=2))
    assert active(enabled)==active(disabled)==active(base)
    assert enabled.code_hash==BASE_HASH


def test_meta_restart_replay_and_checksum_fallback(tmp_path):
    e=engine(tmp_path/"replay.sqlite3")
    result=e.tick(enriched(),NOW)
    assert e.replay_meta(result["decision_id"])["matches"]
    assert engine(e.store.path).replay_meta(result["decision_id"])["matches"]
    with e.store.transaction() as conn:
        conn.execute("UPDATE meta_decisions SET result=json_set(result,'$.adaptive_influence',1)")
    assert not e.replay_meta(result["decision_id"])["matches"]
    later=NOW+timedelta(minutes=5)
    e.tick(enriched(later),later)
    assert "REPLAY_MISMATCH" in e.latest_meta()["challenger"]["vetoes"]


def test_hypothetical_challenger_uses_separate_position_and_net_outcome(tmp_path,champion):
    e=engine(tmp_path/"positions.sqlite3")
    seed_history(e,champion)
    result=e.tick(enriched(),NOW)
    meta=e.latest_meta()
    assert meta["challenger"]["direction"]=="BUY"
    with e.store.connect() as conn:
        assert conn.execute("SELECT status FROM meta_positions").fetchone()[0]=="PENDING"
    data=enriched()
    for minute in (0,1):append_minute(data,NOW.replace(second=0)+timedelta(minutes=minute),low=4310)
    e.tick(data,NOW+timedelta(minutes=2))
    with e.store.connect() as conn:
        row=json.loads(conn.execute("SELECT payload FROM meta_outcomes WHERE json_extract(payload,'$.source')='ADAPTIVE_CHALLENGER'").fetchone()[0])
    assert row["gross_r"]==pytest.approx(-1)
    assert row["net_r"]<row["gross_r"]
    assert e.replay_meta(result["decision_id"])["matches"]


def test_sidecar_fault_does_not_roll_back_champion(tmp_path,monkeypatch,caplog):
    from backend import phase3c
    e=engine(tmp_path/"fault.sqlite3")
    def fail(*args,**kwargs):raise RuntimeError("NON_SECRET_SENTINEL_DO_NOT_LOG")
    monkeypatch.setattr(phase3c,"evaluate_meta",fail)
    result=e.tick(enriched(),NOW)
    assert result["direction"]=="BUY" and active(e)
    assert e.latest_meta()["routing"]["fallback"]
    assert "NON_SECRET_SENTINEL" not in caplog.text
    assert e.replay(result["decision_id"])["matches"]


def test_base_database_is_rejected_without_changes(tmp_path):
    store=Store(tmp_path/"base.sqlite3")
    Phase3BEngine(store,intelligence_policy=IntelligencePolicy(allow_fixture_data=True)).tick(enriched(),NOW)
    with store.connect() as conn:before=list(conn.iterdump())
    with pytest.raises(RuntimeError,match="separate database"):engine(store.path)
    with store.connect() as conn:assert list(conn.iterdump())==before


def test_local_factory_protection_and_read_api(tmp_path,monkeypatch):
    with pytest.raises(ValueError,match="own database"):create_phase3c_app(db_path="backend/data/phase3b.sqlite3")
    app=create_phase3c_app(db_path=tmp_path/"api.sqlite3",start_worker=False,clock=lambda:NOW,
                          intelligence_policy=IntelligencePolicy(allow_fixture_data=True))
    with TestClient(app) as client:
        champion=app.state.engine.tick(enriched(),NOW)
        assert client.get("/signal").json()["strategy_version"]=="phase3b-1"
        assert client.get("/meta/decision").json()["routing"]["active"]=="PHASE3B_CHAMPION"
        assert client.get("/meta/promotion").json()["status"]=="PROMOTION_INELIGIBLE"
        assert client.get("/meta/replay/"+champion["decision_id"]).json()["matches"]
    monkeypatch.setenv("RENDER","true")
    with pytest.raises(RuntimeError,match="local-only"):create_phase3c_app()


def test_meta_source_hash_is_portable(tmp_path):
    root=Path(__file__).parents[1]
    for name in SOURCE_FILES:(tmp_path/name).write_bytes((root/name).read_text(encoding="utf-8").replace("\n","\r\n").encode())
    assert implementation_hash(tmp_path)==implementation_hash()


@pytest.mark.parametrize("bad",[{"min_samples":1},{"max_influence":1},{"max_weight_step":.5},{"embargo_seconds":-1},{"shrinkage":0}])
def test_unsafe_learning_settings_rejected(bad):
    with pytest.raises(ValueError):MetaPolicy(**bad)
