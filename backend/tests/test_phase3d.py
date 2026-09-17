"""Explicit TEST_DATA only. No provider requests or operational history."""
from copy import deepcopy
from datetime import timedelta
from dataclasses import asdict
import json,sqlite3
import pytest

from backend.domain import stamp,parse,digest,canonical
from backend.intelligence_providers import BUNDLE_KEY
from backend.meta_council import DemoCosts
from backend.research.data import Split,validate_tape,causal_labels
from backend.research.archive import Archive,Registry,storage_profile
from backend.research.statistics import metrics,wilson,sequence_risk
from backend.research.lab import run,manifest,evaluate_variant,code_identity,promotion_evidence
from backend.research.models import MODELS,ABLATIONS,ablated_evaluator,make_engine
from backend.research.execution import STRESSES,stress_tick,CounterfactualExecution
from backend.storage import Store
from intelligence_fixtures import enriched
from test_phase1 import NOW,append_minute


def tape_fixture(count=8,step_minutes=5):
    ticks=[];frames=enriched()
    for i in range(count):
        now=NOW+timedelta(minutes=step_minutes*i)
        if i:
            for minute in range(step_minutes):append_minute(frames,NOW.replace(second=0)+timedelta(minutes=(i-1)*step_minutes+minute),low=4310 if i==4 else None)
            for tf,seconds in (('5min',300),('15min',900),('1h',3600),('4h',14400)):
                last=parse(frames[tf][-1]['t'])
                while last+timedelta(seconds=seconds*2)<=now:
                    last+=timedelta(seconds=seconds);p=frames[tf][-1]['c']+.3
                    frames[tf].append({'t':stamp(last),'o':p,'h':p+.5,'l':p-.4,'c':p+.2})
        context=enriched(now)
        for key in list(context):
            if key.startswith('__'):frames[key]=context[key]
        for envelope in frames[BUNDLE_KEY].values():
            for row in envelope.get('records',[]):row['id']+=f'-capture-{i}'
        ticks.append({'at':stamp(now),'captured_at':stamp(now),'frames':deepcopy(frames)})
    return {'schema':'as-received-captures-v1','dataset_id':'offline-test-gold','provider':'deterministic-fixture',
            'mode':'TEST_DATA','point_in_time_attested':True,'ticks':ticks}


@pytest.fixture
def tape():return tape_fixture()


@pytest.fixture
def split():return Split(*(stamp(NOW+timedelta(minutes=m)) for m in (0,5,10,25,40)),embargo_seconds=60)


def closed(i,value=1):
    opened=NOW-timedelta(days=150-i)
    return {'id':str(i),'status':'CLOSED','created_at':stamp(opened-timedelta(minutes=1)),
            'opened_at':stamp(opened),'closed_at':stamp(opened+timedelta(hours=1)),
            'known_at':stamp(opened+timedelta(hours=1)),'gross_r':value+.1,'net_r':value,
            'probability':.75,'predicted_at':stamp(opened-timedelta(minutes=1)),'probability_target':'NET',
            'cohort':{'hidden_state':'RANGE','session':'LONDON','direction':'BUY','volatility':'NORMAL','macro':'MIXED','alignment':'ALIGNED'}}


def test_split_strict_and_half_open(split):
    assert split.phase(split.train_start)=='training'
    assert split.phase(split.holdout_start)=='holdout'
    assert split.phase(split.end) is None
    with pytest.raises(ValueError):Split(split.train_start,split.warmup_start,split.validation_start,split.holdout_start,split.end)


def test_point_in_time_fixture_and_source_order(tape):
    result,flags=validate_tape(tape,True)
    changed=deepcopy(tape)
    for tick in changed['ticks']:
        for channel in tick['frames'][BUNDLE_KEY].values():channel['records'].reverse()
    assert validate_tape(changed,True)==(result,flags)
    with pytest.raises(ValueError,match='SYNTHETIC'):validate_tape(tape)


@pytest.mark.parametrize('bug',['future_candle','forming_candle','naive_time','future_news','future_macro','fixture_contamination','revised_candle','capture_time'])
def test_archive_integrity_rejects_leakage(tape,bug):
    if bug in ('future_candle','forming_candle'):
        tape['ticks'][0]['frames']['1min'][-1]['t']=stamp(NOW.replace(second=0)+timedelta(minutes=1 if bug=='future_candle' else 0))
    if bug=='naive_time':tape['ticks'][0]['at']='2026-09-15T14:00:10'
    if bug in ('future_news','future_macro'):
        channel='news' if bug=='future_news' else 'macro'
        tape['ticks'][0]['frames'][BUNDLE_KEY][channel]['records'][0]['published_at']=stamp(NOW+timedelta(days=1))
    if bug=='fixture_contamination':tape['mode']='HISTORICAL'
    if bug=='revised_candle':tape['ticks'][1]['frames']['1min'][0]['c']+=.01
    if bug=='capture_time':tape['ticks'][0]['captured_at']=stamp(NOW+timedelta(seconds=1))
    with pytest.raises(ValueError):validate_tape(tape,True)


@pytest.mark.parametrize('channel',['news','macro'])
def test_revisions_need_actual_revision_reception(tape,channel):
    first=tape['ticks'][0]['frames'][BUNDLE_KEY][channel]['records'][0]
    changed=deepcopy(first)
    changed['title' if channel=='news' else 'expected_rate']='revised' if channel=='news' else 4.0
    tape['ticks'][1]['frames'][BUNDLE_KEY][channel]['records']=[changed]
    with pytest.raises(ValueError,match='UNVERSIONED'):validate_tape(tape,True)
    changed['revision_received_at']=tape['ticks'][1]['at']
    normalized,_=validate_tape(tape,True)
    assert normalized['ticks'][0]['frames'][BUNDLE_KEY][channel]['records'][0]==first


@pytest.mark.parametrize('bug',['duplicate','gap','duplicate_news','stale'])
def test_integrity_defects_are_explicitly_flagged(tape,bug):
    frames=tape['ticks'][0]['frames']
    if bug=='duplicate':frames['1min'].append(deepcopy(frames['1min'][0]))
    if bug=='gap':del frames['1min'][20]
    if bug=='duplicate_news':frames[BUNDLE_KEY]['news']['records']*=2
    if bug=='stale':frames['1min']=frames['1min'][:-5]
    _,flags=validate_tape(tape,True)
    assert flags


def test_purge_and_embargo_known_time():
    row=closed(0);cutoff=parse(row['closed_at'])+timedelta(hours=1)
    assert causal_labels([row],cutoff,NOW,3600)==[]
    assert causal_labels([row],cutoff+timedelta(seconds=1),NOW,3600)==[row]
    row['known_at']=stamp(NOW+timedelta(days=1))
    assert causal_labels([row],cutoff+timedelta(seconds=1),NOW,0)==[]


def test_metrics_costs_intervals_and_no_fake_confidence():
    rows=[closed(i,1 if i%4!=3 else -1) for i in range(100)]
    m=metrics(rows)
    assert m['win_rate']==.75 and m['expectancy']==.5
    assert m['profit_factor']==3 and m['max_drawdown_r']==1
    assert m['longest_winning_streak']==3 and m['longest_losing_streak']==1
    assert m['calibration']['brier']==pytest.approx(.1875) and m['calibration']['ece']==0
    assert m['win_rate_ci95'][0]<.75<m['win_rate_ci95'][1]
    assert metrics([])['win_rate'] is None
    assert wilson(0,0)==[None,None]
    assert DemoCosts().net(1,2)==.75


def test_bootstrap_deterministic_and_sequence_not_resorted():
    rows=[closed(i,1 if i%4 else -1) for i in range(100)]
    a=sequence_risk(rows,seed=1,iterations=50,block_size=1)
    assert a==sequence_risk(rows,seed=1,iterations=50,block_size=1)
    assert a!=sequence_risk(rows,seed=2,iterations=50,block_size=1)
    assert a['drawdown']['ci95'][1]>1
    assert sequence_risk([closed(0)])['status']=='INSUFFICIENT_EVIDENCE'
    rows[0]['status']='OPEN'
    with pytest.raises(ValueError):sequence_risk(rows)


def test_archive_dedup_roundtrip_and_tamper(tmp_path):
    a=Archive(tmp_path/'objects');value={'frames':[{'t':'a','c':1}]*50}
    key=a.pack(value);size=len(list(a.root.glob('*.z')))
    assert a.pack(value)==key and len(list(a.root.glob('*.z')))==size
    assert a.unpack(key)==value
    (a.root/(key+'.z')).write_bytes(b'bad')
    with pytest.raises(Exception):a.unpack(key)
    with pytest.raises(ValueError):a.get('../secret')


def test_registry_holdout_once_and_failed_runs_retained(tmp_path,tape,split):
    registry=Registry(tmp_path)
    m=manifest(tape,split,'validation',('NO_TRADE',),('BASE',),DemoCosts(),1,{})
    a,_=registry.begin(m);registry.finish(a,m,'FAILED',error='ValueError')
    holdout=dict(m,stage='holdout')
    with pytest.raises(ValueError,match='MATCHING'):registry.begin(holdout)
    a,_=registry.begin(m);registry.finish(a,m,'COMPLETED',{})
    a,_=registry.begin(holdout);registry.finish(a,holdout,'FAILED',error='ValueError')
    with pytest.raises(ValueError,match='FROZEN'):registry.begin(m)
    assert sum(r.get('status')=='FAILED' for r in registry.entries())==2
    with registry.connect() as conn:
        with pytest.raises(sqlite3.IntegrityError):conn.execute('DELETE FROM entries')


def test_no_trade_run_reproducible_and_holdout_not_read(tmp_path,tape,split):
    a=run(tape,split,tmp_path/'a',models=('NO_TRADE',),allow_test=True)
    changed=deepcopy(tape)
    changed['ticks'][-1]['frames']['1min'][-1]['c']=float('inf')
    # The manifest rejects non-JSON data; use a valid but malformed future price instead.
    changed['ticks'][-1]['frames']['1min'][-1]['c']=-100
    b=run(changed,split,tmp_path/'b',models=('NO_TRADE',),allow_test=True)
    assert a['results']==b['results']
    assert a['promotion_evidence']=='INSUFFICIENT_EVIDENCE'
    assert a['results']['NO_TRADE:BASE']['metrics']['no_trade_percent']==100


@pytest.mark.parametrize('model',['PHASE2','PHASE3A','PHASE3B','PHASE3C','EMA_TREND','RSI','BREAKOUT','TREND_5M'])
def test_all_models_run_isolated(tmp_path,tape,split,model):
    report=run(tape,split,tmp_path/model,models=(model,),allow_test=True)
    assert report['champion']=='PHASE3B' and report['automatic_promotion'] is False
    assert report['results'][model+':BASE']['metrics']['trade_count']>=0
    assert report['promotion_evidence']=='INSUFFICIENT_EVIDENCE'
    if model=='PHASE3B':assert len(report['results']['PHASE3B:BASE']['shadows'])==3


@pytest.mark.parametrize('component',ABLATIONS)
def test_ablations_do_not_mutate_champion_globals(tmp_path,tape,split,component):
    from backend.council import WEIGHTS
    before=dict(WEIGHTS)
    engine=make_engine('WITHOUT_'+component,Store(tmp_path/'model.sqlite3'),split,True)
    result=engine.tick(deepcopy(tape['ticks'][0]['frames']),NOW)
    assert result is not None and WEIGHTS==before
    if component in ('technical','momentum','volatility'):assert result['components'][component]['weight']==0
    if component=='entropy':assert result['hidden_state']['entropy']['score']==0
    if component=='event_absorption':assert result['hidden_state']['event_absorption']==[]
    if component=='resilience':assert result['hidden_state']['resilience']['gold_resilience_score'] is None
    if component=='dgfe':assert result['hidden_state']['dgfe']['fracture_score']==0


@pytest.mark.parametrize('scenario',STRESSES)
def test_stresses_do_not_mutate_archive(tape,scenario):
    original=deepcopy(tape['ticks'][0])
    transformed=stress_tick(original,scenario,0)
    assert original==tape['ticks'][0]
    if scenario=='MISSING_DXY':assert transformed['frames'][BUNDLE_KEY]['usd']['status']=='UNAVAILABLE'
    if scenario=='DELAYED_NEWS':assert transformed['frames'][BUNDLE_KEY]['news']['records']==[]


def test_failed_experiment_is_registered(tmp_path,tape,split):
    tape['ticks'][0]['frames']['1min'][-1]['t']=stamp(NOW+timedelta(days=1))
    with pytest.raises(ValueError):run(tape,split,tmp_path,models=('NO_TRADE',),allow_test=True)
    assert Registry(tmp_path).entries()[-1]['status']=='FAILED'
