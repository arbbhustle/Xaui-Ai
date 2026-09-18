"""Offline contract mocks only. These tests are not real provider validation."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json,sqlite3,threading,time
import httpx,pytest

from backend.domain import INTERVALS,canonical,digest,parse,stamp
from backend.forward.contracts import ProviderSpec,CHANNELS,MODES,MAX_AGE,SYMBOLS,normalize,restrictive_mode,quality,select,discrepancy
from backend.forward.providers import ProviderFailure,HTTPSReader,ApprovedJSONProvider,TwelveDataProvider
from backend.forward.store import ForwardStore,encode,decode
from backend.forward.collection import Collector,control
from backend.forward.runner import ForwardRunner,inputs
from backend.storage import Store
from backend.phase3c import Phase3CEngine
from backend.intelligence import IntelligencePolicy
from intelligence_fixtures import bundle
from test_phase1 import NOW,frames


def spec(channel='dxy',mode='TEST_DATA',**kwargs):
    return ProviderSpec(name='contract-'+channel,channel=channel,endpoint='https://contract.invalid/feed',
                        approval='offline-contract-test',mode=mode,**kwargs)


def payload(channel='dxy',now=NOW,mode='TEST_DATA'):
    observed=now-timedelta(seconds=5)
    if channel=='xau':
        value=frames(now);observed=parse(value['1min'][-1]['t'])+timedelta(minutes=1)
    elif channel in ('cot','etf','physical','options'):
        value={'records':[{'source':'contract','url':'https://contract.invalid/data','observed_at':stamp(observed),
                           'published_at':stamp(observed),'score':10,'method':'offline mock index'}]}
    else:
        mapped={'dxy':'usd','us2y':'yields','us10y':'yields','fed':'macro'}.get(channel,channel)
        value=bundle(now)[mapped];value['data_mode']=mode
        if channel in ('dxy','us2y','us10y'):
            value['records']=[r for r in value['records'] if r['instrument']==SYMBOLS[channel]]
            observed=max(parse(r['observed_at']) for r in value['records'])
    return {'symbol':SYMBOLS[channel],'observed_at':stamp(observed),'published_at':stamp(observed),
            'data_mode':mode,'revision_id':'edition-1','value':value}


def observation(channel='dxy',now=NOW,mode='TEST_DATA',provider=None):
    raw=payload(channel,now,mode)
    return normalize(provider or spec(channel,mode),raw,now,digest(raw))


class Mock:
    def __init__(self,channel,mode='TEST_DATA',failure=None):self.channel,self.mode,self.failure,self.calls=channel,mode,failure,0
    def fetch(self,now):
        self.calls+=1
        if self.failure:raise self.failure
        raw=payload(self.channel,now,self.mode)
        return raw,digest(raw)


@pytest.mark.parametrize('channel',CHANNELS)
def test_all_provider_contracts(channel):
    row=observation(channel)
    assert row['data_mode']=='TEST_DATA' and row['health']=='DEGRADED'
    assert row['raw_hash'] and row['received_at']==stamp(NOW)


@pytest.mark.parametrize('mode',MODES)
def test_modes_never_upgrade(mode):
    registered=spec(mode=mode)
    assert restrictive_mode({'data_mode':'LIVE_DATA'},registered)==mode
    assert restrictive_mode({'data_mode':'LIVE_DATA','nested':{'is_synthetic':True}},registered)=='TEST_DATA'


@pytest.mark.parametrize('channel',CHANNELS)
def test_nested_fixture_cannot_be_promoted(channel):
    raw=payload(channel,mode='LIVE_DATA');raw['metadata']={'data_mode':'FIXTURE'}
    row=normalize(spec(channel,'LIVE_DATA'),raw,NOW,digest(raw))
    assert row['data_mode']=='FIXTURE' and row['health']!='HEALTHY'


@pytest.mark.parametrize('mode',['TEST_DATA','FIXTURE','DELAYED_DATA','HISTORICAL_POINT_IN_TIME'])
def test_nonforward_inputs_never_selected(mode):
    s=spec(mode=mode);r=observation(mode=mode)
    assert not select([s],[r],NOW)['selected']


@pytest.mark.parametrize('bug',['future','naive','symbol','inverted','future_row','row_inversion','nan','unknown_mode'])
def test_bad_input_rejected(bug):
    raw=payload(mode='LIVE_DATA')
    if bug=='future':raw['published_at']=stamp(NOW+timedelta(seconds=1))
    if bug=='naive':raw['observed_at']='2026-09-15T14:00:00'
    if bug=='symbol':raw['symbol']='BTC/USD'
    if bug=='inverted':raw['published_at']=stamp(NOW-timedelta(days=1))
    if bug=='future_row':raw['value']['records'][0]['published_at']=stamp(NOW+timedelta(seconds=1))
    if bug=='row_inversion':raw['value']['records'][0]['published_at']=stamp(NOW-timedelta(days=1))
    if bug=='nan':raw['value']['records'][0]['value']=float('nan')
    if bug=='unknown_mode':raw['data_mode']='REAL'
    with pytest.raises((ValueError,KeyError)):normalize(spec(mode='LIVE_DATA'),raw,NOW,'0'*64)


@pytest.mark.parametrize('channel',CHANNELS)
def test_source_freshness_recomputed(channel):
    row=observation(channel,mode='LIVE_DATA')
    assert quality(row,NOW+timedelta(seconds=MAX_AGE[channel]))['health']=='STALE'
    assert quality(row,NOW-timedelta(days=1))['health']=='DEGRADED'


@pytest.mark.parametrize('timestamp,expected',[
    ('2026-03-29T01:30:00+01:00','2026-03-29T00:30:00Z'),
    ('2026-03-29T03:30:00+02:00','2026-03-29T01:30:00Z'),
    ('2026-10-25T02:30:00+02:00','2026-10-25T00:30:00Z'),
    ('2026-10-25T02:30:00+01:00','2026-10-25T01:30:00Z')])
def test_dst_explicit_offsets(timestamp,expected):assert parse(timestamp)==parse(expected)


def test_nested_delay_and_entitlement_downgrade():
    assert restrictive_mode({'x':[{'delay_seconds':60}]},spec(mode='LIVE_DATA'))=='DELAYED_DATA'
    assert restrictive_mode({},spec(mode='LIVE_DATA',entitlement_delay_seconds=1))=='DELAYED_DATA'


@pytest.mark.parametrize('channel',['xau','dxy','us2y','us10y','calendar'])
def test_cross_source_conflict(channel):
    a=observation(channel,mode='LIVE_DATA');b=deepcopy(a)
    if channel=='xau':b['value']['1min'][-1]['c']+=100
    elif channel=='calendar':b['value']['records'][0]['scheduled_at']=stamp(NOW+timedelta(days=7))
    else:
        for r in b['value']['records']:r['value']+=1
    assert discrepancy(a,b)
    one=spec(channel,'LIVE_DATA');two=replace(one,name='backup',priority=1)
    b['provider']='backup';b['provider_identity']=two.identity
    selected=select([one,two],[a,b],NOW)
    assert selected['health'][channel]['status']=='CONFLICTING' and channel not in selected['selected']


def test_failover_is_visible_and_approved():
    a=spec(mode='LIVE_DATA');b=replace(a,name='backup',priority=1)
    row=observation(mode='LIVE_DATA',provider=b)
    result=select([a,b],[row],NOW)
    assert result['switches']==[{'channel':'dxy','from':a.name,'to':'backup','reason':'APPROVED_FAILOVER_OR_PRIORITY_RESTORED'}]


@pytest.mark.parametrize('channel',['dxy','news','calendar','fed'])
def test_revisions_duplicates_and_out_of_order_preserved(tmp_path,channel):
    store=ForwardStore(tmp_path/'forward.sqlite');row=observation(channel)
    with store.transaction() as conn:
        first=store.archive_observation(conn,'a',row)
        duplicate=store.archive_observation(conn,'b',row)
        assert first['observation_id']==duplicate['observation_id']
        changed=deepcopy(row);changed['revision_id']='edition-2';changed['value']['records']=[]
        second=store.archive_observation(conn,'c',changed)
        assert second['observation_id']!=first['observation_id']
        changed['revision_id']=row['revision_id']
        assert store.archive_observation(conn,'d',changed)['health']=='CONFLICTING'
        old=dict(row,observed_at=stamp(NOW-timedelta(days=1)))
        assert 'OUT_OF_ORDER_OBSERVATION' in store.archive_observation(conn,'e',old)['errors']
        assert conn.execute('SELECT COUNT(*) FROM forward_receipts').fetchone()[0]==5
    assert store.verify()['storage_healthy']


@pytest.mark.parametrize('table',['forward_objects','forward_observations','forward_receipts','forward_ledger','snapshots'])
@pytest.mark.parametrize('operation',['UPDATE','DELETE'])
def test_immutable_tables(tmp_path,table,operation):
    store=ForwardStore(tmp_path/'f.sqlite')
    with store.transaction() as conn:
        store.archive_observation(conn,'c',observation())
        store.event(conn,'event','TEST',stamp(NOW),{'test':True})
        value={'test':True}
        conn.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?)',(digest(value),stamp(NOW),canonical(value)))
        column='cycle' if table=='forward_receipts' else 'event_key' if table=='forward_ledger' else 'id'
        with pytest.raises(sqlite3.IntegrityError):conn.execute(f'{operation} {table} SET {column}={column}' if operation=='UPDATE' else f'DELETE FROM {table}')


def test_dedup_and_compression(tmp_path):
    store=ForwardStore(tmp_path/'f.sqlite');value={'repeat':['same']*5000}
    assert decode(encode(canonical(value)))==canonical(value)
    with store.transaction() as conn:
        assert store.put(conn,value)==store.put(conn,value)
        assert conn.execute('SELECT COUNT(*) FROM forward_objects').fetchone()[0]==1
    with sqlite3.connect(store.path) as conn:
        assert len(conn.execute('SELECT payload FROM forward_objects').fetchone()[0])<len(canonical(value))/20


def test_legacy_database_refused(tmp_path):
    path=tmp_path/'legacy.sqlite';Store(path)
    with pytest.raises(ValueError,match='DEDICATED'):ForwardStore(path)


def test_unconfigured_runner_no_trade_and_restart(tmp_path):
    clock=[NOW];path=tmp_path/'f.sqlite';runner=ForwardRunner(path,clock=lambda:clock[0])
    assert runner.status()['status']=='NOT_READY'
    assert runner.once()['direction']=='NO_TRADE'
    assert runner.once()['status']=='DUPLICATE_CYCLE'
    clock[0]+=timedelta(minutes=3)
    restarted=ForwardRunner(path,clock=lambda:clock[0]);assert restarted.once()['direction']=='NO_TRADE'
    with restarted.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM forward_ledger WHERE kind='DECISION'").fetchone()[0]==2
        assert conn.execute("SELECT COUNT(*) FROM forward_ledger WHERE kind='MONITOR_GAP'").fetchone()[0]==1
        assert not conn.execute('SELECT 1 FROM trades').fetchone()


def test_fixture_runner_never_ready_or_opens_trade(tmp_path):
    specs=[spec(c) for c in CHANNELS];providers={s.name:Mock(s.channel) for s in specs}
    runner=ForwardRunner(tmp_path/'f.sqlite',specs,providers,lambda:NOW)
    result=runner.once()
    assert result['direction']=='NO_TRADE' and runner.status()['status']=='NOT_READY'
    with runner.store.connect() as conn:assert not conn.execute('SELECT 1 FROM trades').fetchone()


def test_live_contract_mock_replay_and_freshness(tmp_path):
    # LIVE-labelled mocks exercise the contract path; they are never imported by the runner.
    specs=[spec(c,'LIVE_DATA') for c in ('xau','dxy','us2y','us10y','calendar','news')]
    providers={s.name:Mock(s.channel,'LIVE_DATA') for s in specs}
    runner=ForwardRunner(tmp_path/'f.sqlite',specs,providers,lambda:NOW)
    result=runner.once()
    assert result['decision_id'] and result['direction']=='NO_TRADE'
    assert runner.engine.replay(result['decision_id'])['matches']
    assert runner.engine.replay_meta(result['decision_id'])['matches']
    assert runner.status()['status']=='FORWARD_DEMO_READY'
    assert runner.status(NOW+timedelta(minutes=10))['status']=='NOT_READY'
    with runner.store.connect() as conn:
        event=conn.execute("SELECT payload_id FROM forward_ledger WHERE kind='DECISION'").fetchone()
        evidence=runner.store.get(conn,event[0])
        assert evidence['champion']['direction']=='NO_TRADE'
        assert evidence['challenger']['routing']['active']=='PHASE3B_CHAMPION'


def test_crash_after_capture_recovers_exactly_once(tmp_path,monkeypatch):
    path=tmp_path/'f.sqlite';runner=ForwardRunner(path,clock=lambda:NOW)
    original=runner.engine.tick
    def crash(*args,**kwargs):raise RuntimeError('simulated crash')
    monkeypatch.setattr(runner.engine,'tick',crash)
    with pytest.raises(RuntimeError):runner.once()
    restarted=ForwardRunner(path,clock=lambda:NOW)
    assert restarted.once()['status']=='DUPLICATE_CYCLE'
    with restarted.store.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM forward_cycles WHERE status='COMPLETE'").fetchone()[0]==1
        assert conn.execute("SELECT COUNT(*) FROM forward_ledger WHERE kind='DECISION'").fetchone()[0]==1


def test_collector_rate_limit_restart_and_duplicates(tmp_path):
    s=spec();mock=Mock('dxy',failure=ProviderFailure('RATE_LIMITED',180));clock=[NOW]
    store=ForwardStore(tmp_path/'f.sqlite');collector=Collector(store,[s],{s.name:mock},lambda:clock[0])
    assert collector.collect('a',NOW)[1][0]['reason']=='RATE_LIMITED'
    assert collector.collect('b',NOW)[1][0]['reason']=='DUPLICATE_OR_INTERRUPTED_REQUEST'
    clock[0]+=timedelta(minutes=1)
    restart=Collector(store,[s],{s.name:mock},lambda:clock[0])
    assert restart.collect('c',clock[0])[1][0]['reason']=='BACKOFF'
    assert mock.calls==1


def test_provider_timeout_bounded_and_no_thread_accumulation(tmp_path):
    gate=threading.Event()
    class Hung:
        calls=0
        def fetch(self,now):self.calls+=1;gate.wait(2);return {},'0'*64
    s=spec(timeout_seconds=.05);mock=Hung();store=ForwardStore(tmp_path/'f.sqlite');clock=[NOW]
    collector=Collector(store,[s],{s.name:mock},lambda:clock[0]);start=time.monotonic()
    try:
        assert collector.collect('a',NOW)[1][0]['reason']=='PROVIDER_TIMEOUT'
        clock[0]+=timedelta(minutes=1)
        assert collector.collect('b',clock[0])[1][0]['reason']=='PROVIDER_TIMEOUT'
        assert mock.calls==1 and time.monotonic()-start<1.5
    finally:gate.set()


@pytest.mark.parametrize('status,code',[(429,'RATE_LIMITED'),(500,'HTTP_PROVIDER_FAILURE'),(302,'HTTP_PROVIDER_FAILURE')])
def test_https_failures_are_sanitized(status,code):
    transport=httpx.MockTransport(lambda request:httpx.Response(status,headers={'Retry-After':'200'},text='sensitive raw body'))
    with pytest.raises(ProviderFailure,match=code):HTTPSReader(transport).read('https://contract.invalid',{}, {},.1)


def test_secrets_header_only_and_no_archive_leak(tmp_path):
    # Dynamically generated disposable sentinel, never a credential.
    import uuid
    sentinel=uuid.uuid4().hex;s=spec(secret_env='FORWARD_TEST_SECRET')
    def response(request):
        assert request.headers['Authorization']=='Bearer '+sentinel and sentinel not in str(request.url)
        raw=payload();raw['ignored_secret']=sentinel
        return httpx.Response(200,json=raw)
    provider=ApprovedJSONProvider(s,HTTPSReader(httpx.MockTransport(response)),{'FORWARD_TEST_SECRET':sentinel})
    store=ForwardStore(tmp_path/'f.sqlite');collector=Collector(store,[s],{s.name:provider},lambda:NOW)
    rows,_=collector.collect('a',NOW)
    assert rows and sentinel not in canonical(rows)
    with store.connect() as conn:
        assert all(sentinel not in r[0] for r in conn.execute('SELECT payload FROM forward_objects'))


def test_reject_echoed_secret_in_whitelisted_field(tmp_path):
    import uuid
    sentinel=uuid.uuid4().hex;s=spec('news',secret_env='FORWARD_TEST_SECRET')
    raw=payload('news');raw['value']['records'][0]['title']=sentinel
    provider=ApprovedJSONProvider(s,HTTPSReader(httpx.MockTransport(lambda r:httpx.Response(200,json=raw))),{'FORWARD_TEST_SECRET':sentinel})
    store=ForwardStore(tmp_path/'f.sqlite');rows,audits=Collector(store,[s],{s.name:provider},lambda:NOW).collect('a',NOW)
    assert not rows and sentinel not in canonical(audits)


def test_partial_xau_keeps_minute_monitor(tmp_path):
    s=spec('xau','LIVE_DATA');raw=payload('xau',mode='LIVE_DATA');raw['value']['4h']=[]
    row=normalize(s,raw,NOW,digest(raw));selection=select([s],[row],NOW)
    got,errors=inputs(selection,[row],NOW)
    assert got['1min'] and not errors and selection['vetoes']


def test_configuration_change_requires_new_database(tmp_path):
    path=tmp_path/'f.sqlite';ForwardRunner(path,clock=lambda:NOW)
    with pytest.raises(ValueError,match='CONFIGURATION'):ForwardRunner(path,[spec()],clock=lambda:NOW)


def test_compressed_replay_matches_immutable_base(tmp_path):
    from intelligence_fixtures import enriched
    engines=[Phase3CEngine(cls(tmp_path/(name+'.sqlite')),intelligence_policy=IntelligencePolicy(allow_fixture_data=True))
             for cls,name in ((Store,'plain'),(ForwardStore,'compressed'))]
    results=[e.tick(enriched(),NOW) for e in engines]
    assert results[0]==results[1]
    for e in engines:
        assert e.replay(results[0]['decision_id'])['matches']
        assert e.replay_meta(results[0]['decision_id'])['matches']
