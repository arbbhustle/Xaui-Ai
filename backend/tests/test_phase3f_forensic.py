"""Phase 3F replay, storage, isolation and causal report tests; offline only."""
from copy import deepcopy
from datetime import timedelta
import json,sqlite3,threading,uuid
import pytest,httpx
from backend.domain import stamp,parse,digest,canonical
from backend.forward.contracts import select,normalize,CRITICAL
from backend.forward.runner import KEY
from backend.intelligence_providers import BUNDLE_KEY,MARKET_KEY
from backend.slow_context import SLOW_KEY
from backend.realdata.runner import RealRunner
from backend.realdata.adapters import NativeAdapter
from backend.realdata.transport import NativeHTTP
from backend.realdata.collection import RealCollector
from backend.realdata.verification import verify
from backend.realdata.reports import report,capacity
from test_phase3f import spec,body,adapter,verified_row
from test_phase1 import NOW,frames
from intelligence_fixtures import bundle


def offline_cycle(runner,now,large=False):
    """Explicit TEST_DATA captures. Feed fixture candles directly for offline replay profiling.

    Production collector cannot select these fixtures; this helper is never imported by service code.
    """
    observations=[];cycle=digest(['offline-profile',stamp(now)])
    for s in runner.native_specs:
        def respond(request):
            raw=body(s.channel,now,request.url.params.get('interval','1min'))
            if large and s.channel=='xau' and request.url.params.get('interval')=='1min':
                oldest=deepcopy(raw['values'][0])
                raw['values']=[dict(oldest,datetime=stamp(parse(oldest['datetime'])-timedelta(minutes=i))) for i in range(880,0,-1)]+raw['values']
            if large and s.channel=='news':
                raw=[dict(raw[0],id=i,headline=f'Gold dollar macro headline {i}',url=f'https://example.invalid/news/{i}') for i in range(40)]
            if large and s.channel=='calendar':
                raw=[dict(raw[0],CalendarId=i,Event=f'CPI research event {i}',Date=stamp(now+timedelta(hours=i+2))) for i in range(30)]
            return httpx.Response(200,json=raw)
        a=NativeAdapter(s,NativeHTTP(httpx.MockTransport(respond)),{s.secret_env:uuid.uuid4().hex}).acquire(now)
        r=normalize(s.legacy,a.envelope,now,a.raw_hash);r['native']={'verification':a.verification,'details':a.details}
        with runner.store.transaction() as conn:observations.append(runner.store.archive_observation(conn,cycle,r))
    selection=select(runner.specs,observations,now)
    capture={'observed_at':stamp(now),'started_at':stamp(now),'observations':observations,'provider_audit':[],
             'selection':selection,'vetoes':selection['vetoes']}
    with runner.store.transaction() as conn:
        identity=runner.store.put_capture(conn,capture)
        conn.execute('INSERT INTO forward_cycles VALUES (?,?,?,?,?)',(cycle,stamp(now.replace(second=0)), 'CAPTURED',identity,stamp(now)))
        runner.store.event(conn,'capture:'+cycle,'CAPTURE',stamp(now),{'capture_id':identity})
    f=frames(now)
    if large:f['1min']=observations[0]['value']['1min']
    f[BUNDLE_KEY]=bundle(now);f[MARKET_KEY]={'provider':'OFFLINE_TEST','data_mode':'FIXTURE','retrieved_at':stamp(now)}
    f[SLOW_KEY]={};f[KEY]={'cycle':cycle,'capture_id':identity,'code_hash':runner.engine.forward_hash,'vetoes':selection['vetoes']}
    return runner.engine.tick(f,now)


def test_native_replay_restart_and_isolation(tmp_path):
    specs=[spec(c) for c in CRITICAL];path=tmp_path/'native.sqlite'
    runner=RealRunner(path,specs,clock=lambda:NOW)
    result=offline_cycle(runner,NOW);identity=result['decision_id']
    assert result['direction']=='NO_TRADE'
    first=runner.engine.replay(identity);second=runner.engine.replay_meta(identity)
    assert first['matches'] and second['matches']
    reopened=RealRunner(path,specs,clock=lambda:NOW)
    assert reopened.engine.replay(identity)==first and reopened.engine.replay_meta(identity)==second
    assert reopened.status()['status']=='NOT_READY'
    assert reopened.status()['champion']=='PHASE3B_CHAMPION'
    assert report(reopened.store,NOW)['real_five_minute_decisions']==0
    with runner.store.connect() as source:
        with sqlite3.connect(tmp_path/'restored.sqlite') as dest:source.backup(dest)
    restored=RealRunner(tmp_path/'restored.sqlite',specs,clock=lambda:NOW)
    assert restored.engine.replay(identity)['matches'] and restored.engine.replay_meta(identity)['matches']


def test_challenger_failure_does_not_rewrite_champion(tmp_path,monkeypatch):
    runner=RealRunner(tmp_path/'n.sqlite',[spec(c) for c in CRITICAL],clock=lambda:NOW)
    result=offline_cycle(runner,NOW);identity=result['decision_id'];original=runner.engine.replay(identity)
    monkeypatch.setattr(runner.engine,'replay_meta',lambda _: {'matches':False})
    assert not runner._verify_replay()
    assert runner.engine.replay(identity)==original
    assert runner.status()['status']=='NOT_READY'


def test_collector_secret_echo_not_archived(tmp_path):
    s=spec('news');token=uuid.uuid4().hex
    client=NativeHTTP(httpx.MockTransport(lambda r:httpx.Response(200,json=[dict(body('news')[0],headline='Gold '+token)])))
    a=NativeAdapter(s,client,{s.secret_env:token});runner=RealRunner(tmp_path/'n.sqlite',[s],providers={s.name:a},clock=lambda:NOW)
    rows,audit=runner.collector.collect('offline-secret-check',NOW)
    assert not rows and token not in canonical(audit)
    with runner.store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM forward_observations').fetchone()[0]==0
        assert all(token not in str(r[0]) for r in conn.execute('SELECT payload FROM forward_objects'))


def test_real_runner_mock_responses_never_ready(tmp_path):
    specs=[spec(c) for c in CRITICAL]
    runner=RealRunner(tmp_path/'n.sqlite',specs,collection_enabled=True,providers={s.name:adapter(s) for s in specs},clock=lambda:NOW)
    result=runner.once()
    assert result['direction']=='NO_TRADE' and runner.status()['status']=='NOT_READY'
    assert report(runner.store,NOW)['real_forward_evaluation_cycles']==0


def test_partial_higher_frame_does_not_destroy_minute_data():
    s=spec('xau')
    def mutate(raw):
        if raw['meta']['interval']=='4h':raw['values'][0]['close']='NaN'
        return raw
    a=adapter(s,mutate).acquire(NOW)
    assert a.envelope['value']['1min'] and not a.envelope['value']['4h'] and a.envelope['data_mode']=='TEST_DATA'


def test_partial_verified_minute_still_vetoes_entries():
    s=spec('xau');previous=[]
    for i in range(3):
        now=NOW+timedelta(minutes=i);r=verified_row(s,now)
        r.update(health='DEGRADED',errors=['MISSING_DATA:4h']);r['value']['4h']=[]
        r['native']['verification']['complete']=False
        r=verify(r,previous,s,now);previous.append(r)
    assert r['data_mode']=='LIVE_DATA' and r['health']=='DEGRADED'
    assert select([s.legacy],[r],now)['vetoes']


def test_source_clock_and_expired_entitlement_block():
    s=spec();r=verified_row(s,NOW);r['health']='STALE'
    assert not verify(r,[],s,NOW)['native']['candidate_verified']
    r=verified_row(s,NOW)
    assert not verify(r,[],s,NOW+timedelta(days=31))['native']['candidate_verified']


def test_storage_profile_is_explicit_extrapolation(tmp_path):
    runner=RealRunner(tmp_path/'n.sqlite',[spec(c) for c in CRITICAL],clock=lambda:NOW)
    offline_cycle(runner,NOW,True);result=capacity(runner.store)
    assert result['ticks']==1 and result['mib_per_30_days']==30*result['mib_per_day']
    assert result['integrity']['storage_healthy'] and 'NOT_REAL' in result['scenario']
