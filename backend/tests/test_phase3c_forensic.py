"""Adversarial causal-history, accounting and failure-isolation regressions."""
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
import json

import pytest

from backend.domain import canonical,digest,stamp
from backend.meta_council import MetaPolicy,DemoCosts,evaluate_meta,calibration
from backend.meta_evaluation import promotion_report
from test_phase3c import champion,engine,sample,inputs,seed_history
from intelligence_fixtures import enriched
from test_phase1 import NOW


def test_recent_purged_rows_cannot_evict_training_history(tmp_path,champion):
    e=engine(tmp_path/'bounded.sqlite3',meta_policy=MetaPolicy(history_limit=200))
    seed_history(e,champion)
    namespace=e.namespace(champion)
    with e.store.transaction() as conn:
        for i in range(250):
            opened=NOW-timedelta(days=2)+timedelta(minutes=i*2)
            row=sample(champion,i,id=f'recent-{i}',namespace=namespace,
                       opened_at=stamp(opened),closed_at=stamp(opened+timedelta(minutes=1)),
                       known_at=stamp(opened+timedelta(minutes=1)))
            conn.execute('INSERT INTO meta_outcomes VALUES (?,?,?,?,?)',
                         (row['id'],namespace,row['known_at'],canonical(row),digest(row)))
    e.tick(enriched(),NOW)
    result=e.latest_meta()
    assert len(result['historical_sample_ids'])==80
    assert all(x.startswith('outcome-') for x in result['historical_sample_ids'])


def test_malformed_json_history_is_recorded_as_fallback(tmp_path,champion):
    e=engine(tmp_path/'corrupt.sqlite3')
    with e.store.transaction() as conn:
        conn.execute('INSERT INTO meta_outcomes VALUES (?,?,?,?,?)',
                     ('bad',e.namespace(champion),stamp(NOW-timedelta(days=100)),'{bad','invalid'))
    decision=e.tick(enriched(),NOW)
    assert decision['direction']=='BUY'
    assert 'CORRUPT_HISTORY' in e.latest_meta()['challenger']['vetoes']
    assert e.replay_meta(decision['decision_id'])['matches']


def test_failure_after_success_does_not_expose_old_action(tmp_path,champion,monkeypatch):
    e=engine(tmp_path/'health.sqlite3')
    seed_history(e,champion)
    e.tick(enriched(),NOW)
    assert e.latest_meta()['challenger']['direction']=='BUY'
    def fail(*args):
        raise ValueError('unavailable companion')
    monkeypatch.setattr(e,'_inputs',fail)
    later=NOW+timedelta(minutes=5)
    e.tick(enriched(later),later)
    result=e.latest_meta()
    assert result['challenger']['direction']=='NO_TRADE'
    assert 'META_JOURNAL_FAILURE' in result['challenger']['vetoes']
    assert result['adaptive_influence']==0


def test_read_time_freshness_blocks_old_challenger(tmp_path,champion):
    e=engine(tmp_path/'fresh.sqlite3')
    seed_history(e,champion)
    e.tick(enriched(),NOW)
    assert e.latest_meta()['challenger']['direction']=='BUY'
    result=e.latest_meta(NOW+timedelta(days=1))
    assert result['challenger']['direction']=='NO_TRADE'
    assert result['routing']['fallback']
    assert result['decision_is_historical']


@pytest.mark.parametrize('defect',['vote_nan','vote_range','prediction_nan','prediction_range','cost_mismatch'])
def test_invalid_learning_evidence_fails_closed(champion,defect):
    row=sample(champion)
    if defect=='vote_nan':row['votes']['technical']=float('nan')
    if defect=='vote_range':row['votes']['technical']=101
    if defect=='prediction_nan':row['prediction']=float('nan')
    if defect=='prediction_range':row['prediction']=1.1
    if defect=='cost_mismatch':row.update(cost_assumptions=asdict(DemoCosts()),risk_points=1,net_r=1)
    result=evaluate_meta(inputs(champion,[row]))
    assert 'CORRUPT_HISTORY' in result['challenger']['vetoes']
    assert result['challenger']['direction']=='NO_TRADE'


def test_top_score_calibration_bin_includes_exactly_100(champion):
    rows=[sample(champion,i,raw_score=100) for i in range(60)]
    assert calibration(rows,champion['analytics_context'],99,MetaPolicy())['sample_count']==60


def test_exact_embargo_boundary_and_late_discovery_excluded(champion):
    cutoff=NOW-timedelta(days=20,hours=1)
    rows=[sample(champion,0,closed_at=stamp(cutoff),known_at=stamp(cutoff)),
          sample(champion,1,known_at=stamp(NOW-timedelta(minutes=5)))]
    result=evaluate_meta(inputs(champion,rows))
    assert result['historical_sample_ids']==[]


def test_cost_policy_change_isolates_learning_namespace(tmp_path,champion):
    e=engine(tmp_path/'namespace.sqlite3')
    seed_history(e,champion)
    changed=engine(e.store.path,costs=DemoCosts(.5,.2,0))
    assert e.namespace(champion)!=changed.namespace(champion)
    changed.tick(enriched(),NOW)
    assert changed.latest_meta()['historical_sample_ids']==[]


def test_conflicting_component_evidence_forces_uncertainty_veto(champion):
    current=deepcopy(champion)
    for k,v in current['components'].items():
        v['buy']=0;v['sell']=100
    current['components']['technical'].update(buy=100,sell=0)
    result=evaluate_meta(inputs(current,[sample(champion,i) for i in range(80)]))
    assert result['disagreement_score']>=70
    assert result['challenger']['direction']=='NO_TRADE'
    assert 'EXTREME_UNCERTAINTY' in result['challenger']['vetoes']


def test_promotion_rejects_active_drift_even_with_good_aggregate(champion):
    rows=[]
    for i in range(120):
        row=sample(champion,i,source='ADAPTIVE_CHALLENGER',net_r=1 if i%4!=3 else -.2)
        row['regime'].update(hidden_state=('RANGE','EXPANSION','COMPRESSION')[i%3],session=('LONDON','NEW_YORK')[i%2])
        rows.append(row)
    assert promotion_report(rows)['status']=='PROMOTION_ELIGIBLE_FOR_HUMAN_REVIEW'
    report=promotion_report(rows,active_guards=['ACTIVE_DRIFT'])
    assert report['status']=='PROMOTION_INELIGIBLE'


def test_promotion_checks_current_decision_integrity(tmp_path):
    e=engine(tmp_path/'promotion.sqlite3')
    e.tick(enriched(),NOW)
    with e.store.transaction() as conn:
        conn.execute("UPDATE meta_decisions SET checksum='broken'")
    report=e.meta_analytics(NOW)['promotion']
    assert 'LEAKAGE_OR_INTEGRITY_NOT_CLEARED' in report['reasons']


def test_fixture_provenance_is_retained_and_default_mode_vetoes(tmp_path):
    from backend.phase3c import Phase3CEngine
    from backend.storage import Store
    e=Phase3CEngine(Store(tmp_path/'real-mode.sqlite3'))
    result=e.tick(enriched(),NOW)
    assert result['direction']=='NO_TRADE'
    assert e.latest_meta()['challenger']['direction']=='NO_TRADE'
    assert e.latest_meta()['champion']==result


def test_response_applies_current_macro_and_slow_guards(tmp_path,champion,monkeypatch):
    e=engine(tmp_path/'macro-response.sqlite3')
    seed_history(e,champion)
    e.tick(enriched(),NOW)
    def guarded(conn,decision,now):
        return dict(decision,veto_codes=['STALE_MACRO_TEST','STALE_SLOW_TEST'])
    monkeypatch.setattr(e,'api_decision',guarded)
    result=e.latest_meta(NOW)
    assert {'STALE_MACRO_TEST','STALE_SLOW_TEST'}<=set(result['challenger']['vetoes'])
    assert result['challenger']['direction']=='NO_TRADE'


def test_closed_champion_outcome_is_ingested_with_first_known_time(tmp_path):
    from test_phase1 import append_minute
    e=engine(tmp_path/'ingestion.sqlite3')
    e.tick(enriched(),NOW)
    data=enriched()
    for minute in (0,1):
        append_minute(data,NOW.replace(second=0)+timedelta(minutes=minute),low=4310)
    e.tick(data,NOW+timedelta(minutes=2))
    later=NOW+timedelta(minutes=5)
    e.tick(enriched(later),later)
    with e.store.connect() as conn:
        records=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM meta_outcomes')]
    champion_rows=[r for r in records if r['source']=='CHAMPION']
    assert len(champion_rows)==1
    assert champion_rows[0]['known_at']==stamp(NOW+timedelta(minutes=2))
    assert champion_rows[0]['net_r']<champion_rows[0]['gross_r']


def test_equivalent_timestamp_offsets_preserve_chronological_selection(champion):
    from backend.meta_council import independent
    first=sample(champion,0,opened_at='2026-01-01T10:00:00+02:00',closed_at='2026-01-01T11:00:00+02:00')
    second=sample(champion,1,opened_at='2026-01-01T09:30:00+00:00',closed_at='2026-01-01T10:30:00+00:00')
    assert [r['id'] for r in independent([second,first])]==['outcome-0','outcome-1']


def test_high_hit_rate_negative_expectancy_cannot_earn_extra_trust(champion):
    rows=[sample(champion,i,net_r=.1 if i%10 else -2) for i in range(80)]
    result=evaluate_meta(inputs(champion,rows))
    assert result['trust']['technical']['trust_index']>50
    assert result['trust']['technical']['net_expectancy']<0
    assert result['trust']['technical']['strength']<=0
