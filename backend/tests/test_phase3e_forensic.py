"""Regression coverage for the Phase 3E forensic review. No network or credentials."""
from copy import deepcopy
from datetime import timedelta
import json,sqlite3
import httpx,pytest

from backend.domain import canonical,digest,parse,stamp,INTERVALS
from backend.forward.contracts import normalize,select
from backend.forward.providers import TwelveDataProvider,HTTPSReader,ProviderFailure
from backend.forward.runner import ForwardRunner,inputs
from backend.forward.collection import Collector,control
from backend.forward.store import ForwardStore,encode,decode
from backend.forward.profile import measure
from test_phase3e import spec,payload,observation,Mock
from test_phase1 import NOW


def test_provenance_and_raw_hash_are_part_of_observation_identity(tmp_path):
    store=ForwardStore(tmp_path/'f.sqlite');a=observation('xau',mode='LIVE_DATA')
    with store.transaction() as conn:
        first=store.archive_observation(conn,'first',a)
        b=dict(a,data_mode='TEST_DATA',health='DEGRADED')
        second=store.archive_observation(conn,'second',b)
        c=dict(a,raw_hash='1'*64)
        third=store.archive_observation(conn,'third',c)
        assert len({r['observation_id'] for r in (first,second,third)})==3


def test_crash_after_provider_receipt_recovers_without_refetch(tmp_path):
    s=spec('xau');mock=Mock('xau');store=ForwardStore(tmp_path/'f.sqlite')
    first=Collector(store,[s],{s.name:mock},lambda:NOW).collect('same-cycle',NOW)[0]
    recovered=Collector(store,[s],{s.name:mock},lambda:NOW).collect('same-cycle',NOW)[0]
    assert mock.calls==1 and recovered[0]['data_mode']=='TEST_DATA'
    assert first[0]['raw_hash']==recovered[0]['raw_hash']
    assert first==recovered


def test_slow_wrapper_cannot_hide_stale_source_timestamp():
    raw=payload('cot',mode='LIVE_DATA');raw['value']['records'][0]['observed_at']=stamp(NOW-timedelta(days=90))
    row=normalize(spec('cot','LIVE_DATA'),raw,NOW,digest(raw))
    assert row['health']=='DEGRADED' and 'STALE_CONTRIBUTING_SOURCE' in row['errors']


def test_old_irrelevant_minute_gap_does_not_disable_exit_input():
    raw=payload('xau',mode='LIVE_DATA');del raw['value']['1min'][3]
    s=spec('xau','LIVE_DATA');row=normalize(s,raw,NOW,digest(raw))
    chosen=select([s],[row],NOW);frames,errors=inputs(chosen,[row],NOW)
    assert frames['1min'] and not errors and chosen['vetoes']


def test_disagreeing_partial_feeds_cannot_monitor_exit():
    from dataclasses import replace
    s=spec('xau','LIVE_DATA');raw=payload('xau',mode='LIVE_DATA');raw['value']['4h']=[]
    a=normalize(s,raw,NOW,digest(raw));b=deepcopy(a);b['provider']='backup'
    b['value']['1min'][-1]['c']+=100
    chosen=select([s,replace(s,name='backup')],[a,b],NOW)
    _,errors=inputs(chosen,[a,b],NOW)
    assert '1min' in errors


@pytest.mark.parametrize('defect',['timezone','symbol','future','partial','nested_fixture','nested_delay','rate_limit'])
def test_native_adapter_contract(defect):
    from dataclasses import replace
    import uuid
    token=uuid.uuid4().hex
    s=replace(spec('xau','LIVE_DATA',secret_env='CONTRACT_SECRET'),adapter='twelve_data',endpoint='https://api.twelvedata.com/time_series')
    def response(request):
        assert request.headers['Authorization']=='apikey '+token and request.url.params['timezone']=='UTC'
        interval=request.url.params['interval'];source=payload('xau')['value'][interval]
        if defect=='partial' and interval=='4h':return httpx.Response(503)
        if defect=='rate_limit' and interval=='4h':return httpx.Response(429,headers={'Retry-After':'180'})
        body={'meta':{'symbol':'XAU/USD','interval':interval,'exchange_timezone':'UTC'},'values':[
            {'datetime':r['t'][:19].replace('T',' '),'open':str(r['o']),'high':str(r['h']),'low':str(r['l']),'close':str(r['c'])} for r in source]}
        if defect=='timezone':body['meta']['exchange_timezone']='America/New_York'
        if defect=='symbol':body['meta']['symbol']='XAG/USD'
        if defect=='future':body['values'][-1]['datetime']=stamp(NOW+timedelta(days=1))
        if defect=='nested_fixture':body['values'][0]['data_mode']='TEST_DATA'
        if defect=='nested_delay':body['meta']['delay_seconds']=900
        return httpx.Response(200,json=body)
    provider=TwelveDataProvider(s,HTTPSReader(httpx.MockTransport(response)),{'CONTRACT_SECRET':token})
    if defect=='rate_limit':
        with pytest.raises(ProviderFailure,match='RATE_LIMITED') as error:provider.fetch(NOW)
        assert error.value.retry_after==180
    elif defect in ('timezone','symbol','future'):
        with pytest.raises(ProviderFailure):provider.fetch(NOW)
    else:
        raw,h=provider.fetch(NOW);row=normalize(s,raw,NOW,h)
        assert row['health']!='HEALTHY'
        if defect=='partial':assert row['value']['1min'] and not row['value']['4h']
        if defect=='nested_fixture':assert row['data_mode']=='TEST_DATA'
        if defect=='nested_delay':assert row['data_mode']=='DELAYED_DATA'


def test_circuit_breaker_survives_restart(tmp_path):
    s=spec();mock=Mock('dxy',failure=ProviderFailure('HTTP_PROVIDER_FAILURE'));store=ForwardStore(tmp_path/'f.sqlite')
    for i in range(3):
        at=NOW+timedelta(minutes=i)
        Collector(store,[s],{s.name:mock},lambda:at).collect(str(i),at)
    at=NOW+timedelta(minutes=3)
    rows,audits=Collector(store,[s],{s.name:mock},lambda:at).collect('next',at)
    assert not rows and audits[0]['reason']=='CIRCUIT_OPEN' and mock.calls==3


def test_clock_reversal_naive_clock_and_render_refused(tmp_path,monkeypatch):
    clock=[NOW];runner=ForwardRunner(tmp_path/'f.sqlite',clock=lambda:clock[0]);runner.once()
    clock[0]=NOW-timedelta(minutes=1)
    with pytest.raises(ValueError,match='CLOCK'):runner.once()
    clock[0]=NOW.replace(tzinfo=None)
    with pytest.raises(ValueError,match='CLOCK'):runner.once()
    monkeypatch.setenv('RENDER','true')
    with pytest.raises(RuntimeError,match='LOCAL'):ForwardRunner(tmp_path/'never.sqlite')
    assert not (tmp_path/'never.sqlite').exists()


def test_transaction_failure_rolls_back_entire_engine_and_recovery(tmp_path,monkeypatch):
    s=spec('xau','LIVE_DATA');mock=Mock('xau','LIVE_DATA');path=tmp_path/'f.sqlite'
    runner=ForwardRunner(path,[s],{s.name:mock},lambda:NOW)
    original=runner.engine.after_tick
    def crash(*args):original(*args);raise RuntimeError('offline injected failure')
    monkeypatch.setattr(runner.engine,'after_tick',crash)
    with pytest.raises(RuntimeError):runner.once()
    with runner.store.connect() as conn:
        assert not conn.execute('SELECT 1 FROM decisions').fetchone()
        assert conn.execute('SELECT status FROM forward_cycles').fetchone()[0]=='CAPTURED'
    restart=ForwardRunner(path,[s],{s.name:mock},lambda:NOW);restart.once()
    with restart.store.connect() as conn:
        assert conn.execute('SELECT COUNT(*) FROM decisions').fetchone()[0]==1
    assert mock.calls==1


def test_replay_failure_is_persistent_blocker(tmp_path,monkeypatch):
    s=spec('xau','LIVE_DATA');runner=ForwardRunner(tmp_path/'f.sqlite',[s],{s.name:Mock('xau','LIVE_DATA')},lambda:NOW)
    monkeypatch.setattr(runner.engine,'replay',lambda _: {'matches':False})
    runner.once()
    with runner.store.connect() as conn:assert control(conn,'integrity_block')
    assert 'FORWARD_INTEGRITY_BLOCK' in runner.status()['reasons']


def test_checkpoint_corruption_cannot_pass_readiness(tmp_path):
    runner=ForwardRunner(tmp_path/'f.sqlite',clock=lambda:NOW);runner.once()
    with runner.store.transaction() as conn:conn.execute('UPDATE forward_cycles SET observed_at=?',(stamp(NOW+timedelta(days=1)),))
    assert runner.status()['status']=='NOT_READY'
    with pytest.raises(ValueError):runner.store.verify()


def test_bounded_compression_rejects_corruption():
    with pytest.raises(ValueError):decode(b'unknown')
    with pytest.raises(ValueError):decode(encode('{}')+b'extra')
    with pytest.raises(ValueError):decode(encode('{}')[:-1])


def test_future_news_never_archived_or_used(tmp_path):
    s=spec('news','LIVE_DATA')
    class Future(Mock):
        def fetch(self,now):
            raw=payload('news',mode='LIVE_DATA');raw['value']['records'][0]['published_at']=stamp(now+timedelta(seconds=1))
            return raw,digest(raw)
    store=ForwardStore(tmp_path/'f.sqlite');rows,audits=Collector(store,[s],{s.name:Future('news')},lambda:NOW).collect('a',NOW)
    assert not rows
    with store.connect() as conn:assert not conn.execute('SELECT 1 FROM forward_observations').fetchone()


def test_no_change_to_earlier_evidence_after_later_capture(tmp_path):
    clock=[NOW];s=spec('xau','LIVE_DATA');runner=ForwardRunner(tmp_path/'f.sqlite',[s],{s.name:Mock('xau','LIVE_DATA')},lambda:clock[0])
    runner.once()
    with runner.store.connect() as conn:first=list(conn.execute('SELECT * FROM forward_ledger'))
    clock[0]+=timedelta(minutes=5);runner.once()
    with runner.store.connect() as conn:assert [tuple(r) for r in conn.execute('SELECT * FROM forward_ledger LIMIT ?',(len(first),))]==[tuple(r) for r in first]
    assert measure(runner.store)['compressed_snapshot_bytes']<measure(runner.store)['raw_snapshot_bytes']/3


def test_shared_capture_exact_roundtrip_and_single_value_copy(tmp_path):
    store=ForwardStore(tmp_path/'f.sqlite');s=spec('xau','LIVE_DATA');row=observation('xau',mode='LIVE_DATA')
    capture={'observed_at':stamp(NOW),'observations':[row],'selection':select([s],[row],NOW),'vetoes':[]}
    with store.transaction() as conn:
        key=store.put_capture(conn,capture)
        assert store.get_capture(conn,key)==capture
        compact=store.get(conn,key)
        assert compact['observations'][0]==compact['selection']['selected']['xau']
        assert compact['observations'][0]==compact['selection']['health']['xau']['providers'][0]
        value=digest(row['value'])
        assert conn.execute('SELECT COUNT(*) FROM forward_objects WHERE id=?',(value,)).fetchone()[0]==1
    assert store.verify()['storage_healthy']


def test_partial_secondary_still_vetoes_conflicting_minute_price():
    from dataclasses import replace
    s=spec('xau','LIVE_DATA');a=observation('xau',mode='LIVE_DATA');b=deepcopy(a)
    b.update(provider='backup',health='DEGRADED',errors=['INSUFFICIENT_DATA:4h'])
    b['value']['4h']=[];b['value']['1min'][-1]['c']+=100
    selection=select([s,replace(s,name='backup')],[a,b],NOW)
    assert selection['health']['xau']['status']=='CONFLICTING'
    assert inputs(selection,[a,b],NOW)[1]


def test_candle_offsets_normalize_to_utc():
    from datetime import timezone
    raw=payload('xau',mode='LIVE_DATA')
    for bars in raw['value'].values():
        for row in bars:row['t']=parse(row['t']).astimezone(timezone(timedelta(hours=2))).isoformat()
    normalized=normalize(spec('xau','LIVE_DATA'),raw,NOW,digest(raw))
    assert all(r['t'].endswith('+00:00') for bars in normalized['value'].values() for r in bars)


def test_challenger_failure_cannot_change_champion_decision(tmp_path,monkeypatch):
    specs=[spec(c,'LIVE_DATA') for c in ('xau','dxy','us2y','us10y','calendar','news')]
    clock=[NOW]
    good=ForwardRunner(tmp_path/'good.sqlite',specs,{s.name:Mock(s.channel,'LIVE_DATA') for s in specs},lambda:clock[0])
    broken=ForwardRunner(tmp_path/'broken.sqlite',specs,{s.name:Mock(s.channel,'LIVE_DATA') for s in specs},lambda:clock[0])
    import backend.phase3c as companion
    original=companion.evaluate_meta
    def fail(_):raise RuntimeError('offline companion failure')
    good_first=good.once()
    with monkeypatch.context() as scoped:
        scoped.setattr(companion,'evaluate_meta',fail)
        broken_first=broken.once()
    assert good_first==broken_first
    clock[0]+=timedelta(minutes=5)
    good_second=good.once()
    with monkeypatch.context() as scoped:
        scoped.setattr(companion,'evaluate_meta',fail)
        broken_second=broken.once()
    assert good_second==broken_second
    assert broken.status()['status']=='NOT_READY'
    assert 'CHALLENGER_EVIDENCE_DEGRADED' in broken.status()['reasons']
    with broken.store.connect() as conn:
        assert control(conn,'replay')['champion_ok']
        assert not control(conn,'integrity_block')


def test_no_trade_monitor_has_stable_evidence_identity(tmp_path):
    runner=ForwardRunner(tmp_path/'f.sqlite',clock=lambda:NOW);runner.once()
    with runner.store.connect() as conn:
        row=conn.execute("SELECT payload_id FROM forward_ledger WHERE kind='DECISION'").fetchone()
        evidence=runner.store.get(conn,row[0])
        assert evidence['decision_id'] and evidence['engine_decision_id'] is None


def test_crash_before_capture_then_next_minute_records_acquisition_gap(tmp_path,monkeypatch):
    s=spec('xau','LIVE_DATA');clock=[NOW];path=tmp_path/'f.sqlite';mock=Mock('xau','LIVE_DATA')
    runner=ForwardRunner(path,[s],{s.name:mock},lambda:clock[0])
    def fail(*args):raise RuntimeError('offline capture failure')
    monkeypatch.setattr(runner.store,'put_capture',fail)
    with pytest.raises(RuntimeError):runner.once()
    clock[0]+=timedelta(minutes=1)
    recovered=ForwardRunner(path,[s],{s.name:mock},lambda:clock[0]);recovered.once()
    with recovered.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM forward_ledger WHERE kind='ACQUISITION_GAP'").fetchone()[0]==1
        assert conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]==2


def test_repeated_old_edition_keeps_receipt_specific_out_of_order_veto(tmp_path):
    s=spec('dxy','LIVE_DATA');store=ForwardStore(tmp_path/'f.sqlite');clock=[NOW]
    class Editions(Mock):
        def fetch(self,now):
            raw=payload('dxy',NOW if now>=NOW+timedelta(minutes=2) else now,'LIVE_DATA')
            return raw,digest(raw)
    mock=Editions('dxy');collector=Collector(store,[s],{s.name:mock},lambda:clock[0])
    collector.collect('first',clock[0])
    clock[0]+=timedelta(minutes=1);collector.collect('newer',clock[0])
    clock[0]+=timedelta(minutes=1);original=collector.collect('old-again',clock[0])[0]
    recovered=Collector(store,[s],{s.name:mock},lambda:clock[0]).collect('old-again',clock[0])[0]
    assert original==recovered
    assert 'OUT_OF_ORDER_OBSERVATION' in recovered[0]['errors']
    assert recovered[0]['health']=='DEGRADED'


def test_eventual_outcome_is_append_only_and_costs_are_separate(tmp_path):
    specs=[spec(c,'LIVE_DATA') for c in ('xau','dxy','us2y','us10y','calendar','news')]
    class ExitMarket(Mock):
        trade=None
        def fetch(self,now):
            raw=payload('xau',now,'LIVE_DATA')
            if self.trade:
                candle=raw['value']['1min'][-1]
                if self.trade['direction']=='BUY':candle['l']=min(candle['l'],self.trade['sl']-1)
                else:candle['h']=max(candle['h'],self.trade['sl']+1)
            return raw,digest(raw)
    market=ExitMarket('xau');providers={s.name:market if s.channel=='xau' else Mock(s.channel,'LIVE_DATA') for s in specs}
    clock=[NOW];runner=ForwardRunner(tmp_path/'f.sqlite',specs,providers,lambda:clock[0])
    for i in range(20):
        clock[0]=NOW+timedelta(minutes=i);runner.once()
        with runner.store.connect() as conn:
            active=conn.execute("SELECT payload FROM trades WHERE status='OPEN'").fetchone()
            if active:market.trade=json.loads(active[0])
            outcome=conn.execute("SELECT payload_id FROM forward_ledger WHERE kind='OUTCOME' AND event_key LIKE 'outcome:CHAMPION:%'").fetchone()
            if outcome:
                evidence=runner.store.get(conn,outcome[0]);break
    else:pytest.fail('Mock path must exercise a filled and closed champion demo trade')
    assert evidence['gross_r']==evidence['trade']['r_multiple']
    risk=abs(evidence['trade']['entry']-evidence['trade']['sl'])
    assert evidence['simulated_net_r']==pytest.approx(round(evidence['gross_r']-.5/risk,8))
    assert parse(evidence['known_at'])>=parse(evidence['trade']['closed_at'])
    assert runner.once()['status']=='DUPLICATE_CYCLE'
    with runner.store.connect() as conn:
        assert conn.execute("SELECT count(*) FROM forward_ledger WHERE event_key LIKE 'outcome:CHAMPION:%'").fetchone()[0]==1


def test_concurrent_once_cannot_replace_scheduler_lock_handle(tmp_path):
    runner=ForwardRunner(tmp_path/'f.sqlite',clock=lambda:NOW)
    with runner.lock:
        owner=runner.lock.handle
        with pytest.raises(RuntimeError,match='one monitor'):runner.once()
        assert runner.lock.handle is owner and not owner.closed
    assert runner.once()['direction']=='NO_TRADE'


def test_cold_backup_restore_preserves_both_replays(tmp_path):
    specs=[spec(c,'LIVE_DATA') for c in ('xau','dxy','us2y','us10y','calendar','news')]
    providers={s.name:Mock(s.channel,'LIVE_DATA') for s in specs}
    runner=ForwardRunner(tmp_path/'original.sqlite',specs,providers,lambda:NOW)
    result=runner.once();decision=result['decision_id']
    with runner.store.connect() as source:
        with sqlite3.connect(tmp_path/'restored.sqlite') as target:source.backup(target)
    restored=ForwardRunner(tmp_path/'restored.sqlite',specs,providers,lambda:NOW)
    assert restored.engine.replay(decision)==runner.engine.replay(decision)
    assert restored.engine.replay_meta(decision)==runner.engine.replay_meta(decision)
    assert restored.store.verify()==runner.store.verify()
    assert restored.once()['status']=='DUPLICATE_CYCLE'


def test_full_audit_rejects_corrupted_compressed_companion_journal(tmp_path):
    s=spec('xau','LIVE_DATA');runner=ForwardRunner(tmp_path/'f.sqlite',[s],{s.name:Mock('xau','LIVE_DATA')},lambda:NOW)
    runner.once()
    # Simulate storage corruption outside normal immutable-table write protections.
    with runner.store.transaction() as conn:
        conn.execute('DROP TRIGGER immutable_meta_decisions_UPDATE')
        conn.execute('UPDATE meta_decisions SET result=?',(encode('{"corrupted":true}'),))
    with pytest.raises(ValueError,match='CHALLENGER_JOURNAL'):runner.store.verify()
    assert runner.status()['status']=='NOT_READY'
