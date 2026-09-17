"""Adversarial holdout, cost, calibration, archival and isolation checks."""
from copy import deepcopy
from datetime import timedelta
from dataclasses import asdict
import json
import pytest

from backend.domain import stamp,parse,digest
from backend.meta_council import DemoCosts
from backend.storage import Store
from backend.research.archive import Registry,Archive,storage_profile,database_profile
from backend.research.lab import run,manifest,sensitivity,_report
from backend.research.statistics import metrics,sequence_risk,calibration,compare
from backend.research.execution import CounterfactualExecution
from backend.research.models import make_engine
from backend.research.data import validate_tape
from backend.intelligence_providers import BUNDLE_KEY
from test_phase3d import tape,split,closed,tape_fixture
from test_phase1 import NOW,append_minute


def test_holdout_executes_once_then_locks_even_other_variants(tmp_path,tape,split):
    run(tape,split,tmp_path,models=('NO_TRADE',),allow_test=True)
    report=run(tape,split,tmp_path,stage='holdout',models=('NO_TRADE',),allow_test=True)
    assert report['stage']=='holdout'
    with pytest.raises(ValueError,match='FROZEN'):run(tape,split,tmp_path,models=('EMA_TREND',),allow_test=True)
    with pytest.raises(ValueError,match='FROZEN'):run(tape,split,tmp_path,stage='holdout',models=('NO_TRADE',),allow_test=True)


def test_changed_settings_or_data_cannot_open_validated_holdout(tmp_path,tape,split):
    run(tape,split,tmp_path,models=('NO_TRADE',),allow_test=True)
    with pytest.raises(ValueError,match='MATCHING'):
        run(tape,split,tmp_path,models=('NO_TRADE',),stage='holdout',costs=DemoCosts(.6,.1),allow_test=True)
    tape['ticks'][-1]['frames']['5min'][-1]['c']+=.01
    with pytest.raises(ValueError,match='MATCHING'):
        run(tape,split,tmp_path,models=('NO_TRADE',),stage='holdout',allow_test=True)


def test_frozen_manifest_roundtrip_and_no_overwrite(tmp_path,tape,split):
    r=Registry(tmp_path)
    m=manifest(tape,split,'validation',('NO_TRADE',),('BASE',),DemoCosts(),173,{})
    _,key=r.begin(m)
    assert r.archive.get(key)==m
    assert m['default_policies']['CouncilPolicy']['min_score']==68
    assert m['split']==asdict(split)
    assert m['costs']==asdict(DemoCosts())
    altered=dict(m,seed=174)
    assert r.archive.put(altered)!=key and r.archive.get(key)==m


def test_provider_conflict_cannot_hide_in_scorecard(tmp_path,tape,split):
    for tick in tape['ticks']:
        news=tick['frames'][BUNDLE_KEY]['news']['records']
        row=deepcopy(news[0]);row.update(id=row['id']+'opposite',source='opposite-source',title='Gold falls as dollar rises')
        news.append(row)
    report=run(tape,split,tmp_path,models=('PHASE3A',),allow_test=True)
    flags=report['results']['PHASE3A:BASE']['integrity_flags']
    assert any('CONFLICT' in f['code'] for f in flags)
    assert report['promotion_evidence']=='INSUFFICIENT_EVIDENCE'


def test_perfect_calibration_does_not_report_overconfidence():
    result=calibration([closed(i,1 if i%4 else -1) for i in range(100)])
    assert result['overconfidence']==0 and result['underconfidence']==0


def test_open_position_exposure_is_reported_without_inventing_outcome(split):
    row={'status':'OPEN','opened_at':split.validation_start,'closed_at':None}
    end=parse(split.validation_start)+timedelta(minutes=2)
    result=_report([],[],[row],split,'validation',173,end)
    assert result['metrics']['exposure_seconds']==120
    assert result['metrics']['trade_count']==0
    assert result['unclosed_positions_at_cutoff']==1


def test_cross_boundary_trade_is_purged_not_closed_artificially(split):
    row=closed(0)
    row.update(created_at=stamp(parse(split.validation_start)-timedelta(minutes=2)),
               opened_at=stamp(parse(split.validation_start)-timedelta(minutes=1)),
               closed_at=stamp(parse(split.validation_start)+timedelta(minutes=1)),
               known_at=stamp(parse(split.validation_start)+timedelta(minutes=1)))
    report=_report([row],[],[],split,'validation',173,parse(split.holdout_start))
    assert report['metrics']['trade_count']==0 and report['metrics']['exposure_seconds']==60


def test_delayed_fill_uses_future_open_and_cost_applied_once(tape):
    d={'decision_id':'x','direction':'BUY','timestamp_utc':stamp(NOW),'entry':100,'sl':98,'tp2':104,
       'expires_at':stamp(NOW+timedelta(minutes=5)),'calibrated_confidence':.75,'research_probability_target':'NET'}
    executor=CounterfactualExecution(delay_minutes=1,entry_drift_points=.5)
    executor.submit(d,{})
    assert executor.active['checkpoint']==stamp(NOW.replace(second=0)+timedelta(minutes=2))
    def tick(minute):
        opened=NOW.replace(second=0)+timedelta(minutes=minute)
        return {'at':stamp(opened+timedelta(minutes=1)),'source_errors':{},
                'frames':{'1min':[{'t':stamp(opened),'o':100,'h':105,'l':97,'c':100}]}}
    executor.monitor(tick(1));assert not executor.closed
    executor.monitor(tick(2));row=executor.closed[0]
    assert row['gross_r']==-1.25  # Stop wins ambiguous high/low; adverse drift worsens entry.
    assert DemoCosts().net(row['gross_r'],row['risk'])==-1.5
    assert row['probability_target']=='NET'


@pytest.mark.parametrize('scenario',['DELAY_1M','DELAY_2M','ENTRY_DRIFT','WIDE_SPREAD','HIGH_SLIPPAGE','MISSING_DXY','STALE_MACRO'])
def test_integrated_stress_reports_assumptions_and_shadows(tmp_path,tape,split,scenario):
    report=run(tape,split,tmp_path,models=('PHASE3B',),scenarios=(scenario,),allow_test=True)['results']['PHASE3B:'+scenario]
    assert len(report['shadows'])==3
    if scenario.startswith('DELAY') or scenario=='ENTRY_DRIFT':
        assert 'COUNTERFACTUAL' in report['execution_basis']
        assert all('COUNTERFACTUAL' in r['execution_basis'] for r in report['shadows'].values())
    if scenario=='WIDE_SPREAD':assert report['costs']['spread_points']==pytest.approx(.9)
    if scenario=='HIGH_SLIPPAGE':assert report['costs']['slippage_points_per_side']==pytest.approx(.3)


def test_sensitivity_is_validation_only_and_does_not_select_winner(tmp_path,tape,split):
    report=sensitivity(tape,split,tmp_path,values=(77,79),allow_test=True)
    assert report['selected_value'] is None and len(report['variants'])==2
    assert all(r['stage']=='validation' for r in Registry(tmp_path).entries())
    assert report['status']=='LOW_SAMPLE'


def test_storage_profile_counts_only_reachable_objects(tmp_path):
    a=Archive(tmp_path/'objects')
    values=[{'frames':[{'t':'same','c':1,'o':1,'h':2,'l':.5}]*100}]*5
    before=storage_profile(values,a)
    a.put({'unrelated':'x'*5000})
    after=storage_profile(values,a)
    assert after==before and before['roundtrip_verified']
    assert before['compressed_deduplicated_bytes']<before['raw_payload_bytes']


def test_production_champion_hash_and_source_unchanged(tmp_path,tape,split):
    from backend.phase3b import implementation_hash
    from backend.phase3c import BASE_HASH
    assert implementation_hash()==BASE_HASH
    e=make_engine('PHASE3B',Store(tmp_path/'a.sqlite3'),split,True)
    e.tick(tape['ticks'][0]['frames'],NOW)
    with e.store.connect() as c:
        profile=database_profile(c)
    assert profile['ticks']==1 and profile['allocated_bytes_by_object']['snapshots']>0
    assert implementation_hash()==BASE_HASH


def test_credentials_are_rejected_before_archival(tape):
    tape['ticks'][0]['frames']['api_key']='TEST_SENTINEL'
    with pytest.raises(ValueError,match='CREDENTIAL'):validate_tape(tape,True)


def test_exact_archived_experiment_replay(tmp_path,tape,split):
    from backend.research.lab import reproduce
    run(tape,split,tmp_path,models=('NO_TRADE',),allow_test=True)
    assert reproduce(tmp_path,1)['matches']
    entry=Registry(tmp_path).entries()[-1]
    assert entry['status']=='COMPLETED'


def test_duplicate_completion_cannot_fabricate_validation_success(tmp_path,tape,split):
    r=Registry(tmp_path);m=manifest(tape,split,'validation',('NO_TRADE',),('BASE',),DemoCosts(),173,{})
    attempt,_=r.begin(m);r.finish(attempt,m,'FAILED')
    with pytest.raises(ValueError,match='COMPLETION'):r.finish(attempt,m,'COMPLETED',{})


def test_large_document_chunks_roundtrip(tmp_path):
    archive=Archive(tmp_path)
    value={'records':['x'*1000000]*9}
    key=archive.put_document(value)
    assert archive.get(key)['document']=='dict'
    assert archive.get_document(key)==value


def test_registry_context_closes_connection(tmp_path):
    import sqlite3
    registry=Registry(tmp_path)
    with registry.connect() as connection:
        connection.execute('SELECT 1')
    with pytest.raises(sqlite3.ProgrammingError):connection.execute('SELECT 1')


def test_warmup_cannot_create_trade_or_shadow_training_labels(tmp_path,tape,split):
    e=make_engine('PHASE3C',Store(tmp_path/'warmup.sqlite3'),split,True)
    result=e.tick(tape['ticks'][0]['frames'],NOW)
    assert 'RESEARCH_WARMUP_ONLY' in result['veto_codes']
    with e.store.connect() as conn:
        for table in ('trades','phase3b_shadows','meta_positions','meta_outcomes'):
            assert conn.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        assert conn.execute('SELECT count(*) FROM phase3b_context').fetchone()[0]==1


@pytest.mark.parametrize('model',['PHASE2','EMA_TREND'])
def test_legacy_research_models_never_label_fixture_as_live(tmp_path,tape,split,model):
    e=make_engine(model,Store(tmp_path/'legacy.sqlite3'),split,True)
    result=e.tick(tape['ticks'][0]['frames'],NOW)
    assert result['data_status']=='FIXTURE_DATA'
    assert result['research_data_mode']=='TEST_DATA'
    assert result['research_historical_evaluation'] is True


@pytest.mark.parametrize('dependency',['intelligence_journal.py','requirements.txt'])
def test_replay_identity_covers_transitive_helpers_and_dependencies(tmp_path,dependency):
    from backend.research.lab import code_identity
    (tmp_path/dependency).write_text('initial',encoding='utf-8')
    first=code_identity(tmp_path)
    (tmp_path/dependency).write_text('changed',encoding='utf-8')
    assert code_identity(tmp_path)!=first
