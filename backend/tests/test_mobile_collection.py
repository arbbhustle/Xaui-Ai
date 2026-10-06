"""Offline gate simulations only: no real credentials, approvals or network calls."""
from datetime import timedelta
from pathlib import Path
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from backend.domain import digest, stamp
from backend.mobile.runtime import Runtime
from backend.mobile.api import create_app, system
from backend.mobile.collection import StrictXauAdapter, ReceiptValidatedCollector
from backend.realdata.transport import NativeHTTP, Response
from backend.forward.providers import ProviderFailure
from test_phase1 import NOW
from test_phase3f import body


def approve(monkeypatch):
    for key,value in {'MOBILE_COLLECTION_ENABLED':'true','MOBILE_XAU_APPROVAL_REF':'UNIT_GATE_ONLY',
        'MOBILE_XAU_ENTITLEMENT_REF':'UNIT_GATE_ONLY','MOBILE_XAU_ENTITLEMENT_UNTIL':stamp(NOW+timedelta(days=30)),
        'MOBILE_XAU_SOURCE_TIMEZONE':'UTC','MOBILE_XAU_UNIT':'USD_per_troy_ounce',
        'MOBILE_XAU_LIVE_ENTITLED':'true','TWELVE_DATA_API_KEY':'unit-only-not-a-real-key'}.items():monkeypatch.setenv(key,value)


def wire(runtime,monkeypatch,current,mutate=None,code=None):
    calls=[]
    def read(self,spec,path,params,now,environ=None):
        calls.append(params)
        if code:raise ProviderFailure(code,180 if code=='RATE_LIMITED' else 0)
        data=body('xau',now,params['interval'])
        if mutate:mutate(data,params['interval'])
        # Unit simulation of transport attestation only, not real provider evidence.
        return Response(data,digest(data),True,None)
    monkeypatch.setattr(NativeHTTP,'read',read)
    runtime.collector.clock=lambda:current[0]
    runtime.collector.collector.clock=lambda:current[0]
    return calls


@pytest.mark.parametrize('field',['MOBILE_XAU_APPROVAL_REF','MOBILE_XAU_ENTITLEMENT_REF','MOBILE_XAU_ENTITLEMENT_UNTIL',
    'MOBILE_XAU_SOURCE_TIMEZONE','MOBILE_XAU_UNIT','MOBILE_XAU_LIVE_ENTITLED'])
def test_incomplete_approval_never_requests(tmp_path,monkeypatch,field):
    approve(monkeypatch);monkeypatch.delenv(field)
    with Runtime(tmp_path/'m.db') as runtime:
        calls=wire(runtime,monkeypatch,[NOW])
        runtime.collector._once()
        assert not calls
        assert system(runtime,NOW)['xau']['approval']=='UNAPPROVED'


@pytest.mark.parametrize('case',['missing','expired','invalid','rate'])
def test_failures_create_no_market_receipts(tmp_path,monkeypatch,case):
    approve(monkeypatch)
    if case=='missing':monkeypatch.delenv('TWELVE_DATA_API_KEY')
    if case=='expired':monkeypatch.setenv('MOBILE_XAU_ENTITLEMENT_UNTIL',stamp(NOW))
    with Runtime(tmp_path/'m.db') as runtime:
        current=[NOW]
        calls=wire(runtime,monkeypatch,current,code={'invalid':'AUTH_OR_ENTITLEMENT_DENIED','rate':'RATE_LIMITED'}.get(case))
        runtime.collector._once()
        with runtime.read() as conn:assert conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]==0
        assert system(runtime,NOW)['status']=='NOT_READY'
        assert system(runtime,NOW)['xau']['data_mode']=='UNAVAILABLE'
        if case=='rate':
            count=len(calls);current[0]+=timedelta(minutes=1);runtime.collector._once()
            assert len(calls)==count


@pytest.mark.parametrize('case',['future','stale','synthetic','wrong_symbol','bad_timezone','partial','secret','delayed',
    'fixture_status','delayed_flag','overflow'])
def test_bad_response_rejected_before_market_storage(tmp_path,monkeypatch,case):
    approve(monkeypatch)
    def mutate(data,tf):
        if case=='future':data['values'][0]['datetime']=stamp(NOW+timedelta(days=1))
        if case=='stale':data.update(body('xau',NOW-timedelta(days=1),tf))
        if case=='synthetic':data['is_demo']=True
        if case=='wrong_symbol':data['meta']['symbol']='XAG/USD'
        if case=='bad_timezone':data['meta']['timezone']='Europe/Berlin'
        if case=='partial' and tf=='4h':data['values']=[]
        if case=='secret':data['message']='unit-only-not-a-real-key'
        if case=='delayed':data['delay_seconds']=900
        if case=='fixture_status':data['data_status']='FIXTURE_DATA'
        if case=='delayed_flag':data['is_delayed']=True
        if case=='overflow':data['values']*=10
    with Runtime(tmp_path/'m.db') as runtime:
        wire(runtime,monkeypatch,[NOW],mutate)
        runtime.collector._once()
        with runtime.read() as conn:
            assert conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]==0
            assert 'unit-only-not-a-real-key' not in '\n'.join(conn.iterdump())
        assert 'unit-only-not-a-real-key' not in json.dumps(system(runtime,NOW))


def test_success_cadence_closed_candles_restart_replay_and_duplicate(tmp_path,monkeypatch):
    approve(monkeypatch);current=[NOW];path=tmp_path/'m.db'
    with Runtime(path) as runtime:
        calls=wire(runtime,monkeypatch,current)
        for i in range(3):
            current[0]=NOW+timedelta(minutes=i)
            result=runtime.collector._once()
            assert result.get('status')!='COLLECTION_OR_INTEGRITY_FAILURE'
        assert len(calls)==7
        assert sum(1 for call in calls if call['interval']=='1min')==3
        assert sum(1 for call in calls if call['interval']!='1min')==4
        state=system(runtime,current[0]);assert state['xau']['data_mode']=='LIVE_DATA'
        assert state['status']=='NOT_READY' and state['collection_enabled']
        assert all(p['approval']=='UNAPPROVED' for p in state['providers'][1:])
        assert runtime.collector._once()['status']=='DUPLICATE_CYCLE'
        assert len(calls)==7
        assert system(runtime,current[0]+timedelta(minutes=4))['xau']['data_mode']=='UNAVAILABLE'
        with runtime.read() as conn:
            identity=conn.execute('SELECT id FROM decisions LIMIT 1').fetchone()[0]
        assert runtime.engine.replay(identity)['matches']
        assert runtime.engine.replay_meta(identity)['matches']
    with Runtime(path) as runtime:
        assert runtime.replay['status']=='PASSED'
        assert runtime.engine.replay(identity)['matches']


def test_basic_safe_steady_state_uses_one_request(tmp_path,monkeypatch):
    approve(monkeypatch);current=[NOW]
    with Runtime(tmp_path/'m.db') as runtime:
        calls=wire(runtime,monkeypatch,current)
        runtime.collector._once()
        assert len(calls)==5
        current[0]=NOW+timedelta(minutes=1)
        runtime.collector._once()
        assert len(calls)==6 and calls[-1]['interval']=='1min'
        with runtime.read() as conn:
            row=conn.execute('SELECT payload_id FROM forward_receipts ORDER BY received_at DESC LIMIT 1').fetchone()
            value=runtime.store.get_observation_document(conn,row[0])
        assert set(value['value'])=={'1min','5min','15min','1h','4h'}
        assert value['native']['details']['mobile_request_mode']=='BASIC_STEADY_1_REQUEST'
        assert value['native']['details']['aggregation_anchors_seconds']=={'4h':0}
        assert value['native']['details']['aggregation_anchor_source']=='NATIVE_4H_BOOTSTRAP'


def test_mock_transport_never_real(tmp_path,monkeypatch):
    approve(monkeypatch)
    with Runtime(tmp_path/'m.db') as runtime:
        adapter=StrictXauAdapter(runtime.specs[0],NativeHTTP(httpx.MockTransport(
            lambda req:httpx.Response(200,json=body('xau',NOW,req.url.params['interval'])))))
        with pytest.raises(ValueError,match='XAU_VALIDATION_FAILED'):adapter.acquire(NOW)


def test_render_startup_disabled_and_gets_do_not_collect(tmp_path,monkeypatch):
    approve(monkeypatch);monkeypatch.setenv('MOBILE_COLLECTION_ENABLED','false')
    monkeypatch.setenv('RENDER','true');monkeypatch.setenv('MOBILE_DISK_PATH',str(tmp_path))
    monkeypatch.setattr(Path,'is_mount',lambda p:True)
    monkeypatch.setattr(NativeHTTP,'read',lambda *a,**k:pytest.fail('Unexpected network'))
    with TestClient(create_app(tmp_path/'m.db',clock=lambda:NOW)) as client:
        for route in ('health','signal','system-status'):
            value=client.get('/'+route).json()
            assert 'unit-only-not-a-real-key' not in json.dumps(value)
        assert client.get('/health').json()['collection_enabled'] is False
        assert client.get('/system-status').json()['xau']['credential_configured'] is True
        assert client.get('/signal').json()['direction']=='NO_TRADE'


def test_budget_prevents_requests(tmp_path,monkeypatch):
    approve(monkeypatch)
    with Runtime(tmp_path/'m.db') as runtime:
        calls=wire(runtime,monkeypatch,[NOW]);runtime.limit=1
        assert runtime.collector._once()['status']=='STORAGE_BUDGET_EXCEEDED'
        assert calls==[]


def test_status_exposes_attempt_and_scheduler_diagnostics(tmp_path,monkeypatch):
    approve(monkeypatch);current=[NOW]
    with Runtime(tmp_path/'m.db') as runtime:
        wire(runtime,monkeypatch,current)
        runtime.collector._once()
        xau=system(runtime,current[0])['xau']
        assert xau['last_attempt_at']==stamp(NOW)
        assert xau['last_attempt_age_seconds']==0
        assert xau['last_attempt_status']=='COMPLETE'
        assert xau['last_validation_failure'] is None
        assert xau['validator_version']=='legacy'
        assert xau['collector_thread_alive'] is False
        assert xau['scheduler_status']=='NOT_RUNNING'
        assert isinstance(xau['market_closed'],bool)


def test_status_exposes_safe_xau_validation_failure(tmp_path,monkeypatch):
    approve(monkeypatch)
    monkeypatch.setenv('MOBILE_XAU_VALIDATOR','mobile-twelve-xau-4h-v1')
    def wrong_symbol(data,tf):
        if tf=='1min':data['meta']['symbol']='XAG/USD'
    with Runtime(tmp_path/'m.db') as runtime:
        wire(runtime,monkeypatch,[NOW],wrong_symbol)
        runtime.collector._once()
        xau=system(runtime,NOW)['xau']
        assert xau['last_attempt_status']=='INVALID_OR_UNSAFE_PROVIDER_DATA'
        assert xau['last_validation_failure']=='WRONG_MARKET_SYMBOL'
        assert xau['validator_version']=='mobile-twelve-xau-4h-v1'


def test_approval_change_requires_new_database(tmp_path,monkeypatch):
    path=tmp_path/'m.db'
    with Runtime(path):pass
    approve(monkeypatch)
    with pytest.raises(RuntimeError,match='ORIGINAL_ENGINE_CONFIGURATION_REQUIRED'):
        with Runtime(path):pass


def test_enabled_render_lifespan_and_gets_are_read_only(tmp_path,monkeypatch):
    from backend.mobile.collection import MobileCollector
    from backend.realdata.store import RealStore
    approve(monkeypatch)
    monkeypatch.setenv('RENDER','true');monkeypatch.setenv('MOBILE_DISK_PATH',str(tmp_path))
    monkeypatch.setattr(Path,'is_mount',lambda p:True)
    starts=[]
    monkeypatch.setattr(MobileCollector,'start',lambda self:starts.append(self))
    monkeypatch.setattr(NativeHTTP,'read',lambda *a,**k:pytest.fail('GET acquired data'))
    monkeypatch.setattr(RealStore,'verify',lambda *a,**k:pytest.fail('Render startup performed full history verify'))
    with TestClient(create_app(tmp_path/'m.db',clock=lambda:NOW)) as client:
        assert len(starts)==1
        assert client.app.state.runtime.replay['scope']=='LATEST_TAIL_RENDER_STARTUP'
        for _ in range(3):
            assert client.get('/health').json()['collection_enabled']
            assert client.get('/signal').json()['direction']=='NO_TRADE'
        with client.app.state.runtime.read() as conn:
            assert conn.execute('SELECT count(*) FROM forward_requests').fetchone()[0]==0


def test_scheduler_single_start_shutdown_and_pause_preserves_epoch(tmp_path,monkeypatch):
    import threading
    approve(monkeypatch);path=tmp_path/'m.db';ran=threading.Event()
    with Runtime(path) as runtime:
        calls=wire(runtime,monkeypatch,[NOW])
        original=runtime.collector._once
        def once():
            result=original();ran.set();return result
        runtime.collector._once=once
        runtime.collector.start();runtime.collector.start()
        assert ran.wait(20)
        runtime.collector.close()
        assert not runtime.collector.thread.is_alive()
        assert len(calls)==5
    monkeypatch.setenv('MOBILE_COLLECTION_ENABLED','false')
    with Runtime(path) as runtime:
        assert runtime.collector._once()['status']=='COLLECTION_DISABLED'
        assert runtime.replay['status']=='NO_DECISIONS_YET' # First receipt cannot establish cadence.
        with runtime.read() as conn:
            assert conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]==1


def test_forming_bars_not_stored(tmp_path,monkeypatch):
    approve(monkeypatch)
    def add_forming(data,tf):
        if tf=='1min':
            bar=dict(data['values'][-1],datetime=stamp(NOW.replace(second=0,microsecond=0)))
            data['values'].append(bar)
    with Runtime(tmp_path/'m.db') as runtime:
        wire(runtime,monkeypatch,[NOW],add_forming)
        runtime.collector._once()
        with runtime.read() as conn:
            row=conn.execute('SELECT payload_id FROM forward_receipts LIMIT 1').fetchone()
            assert row
            value=runtime.store.get_observation_document(conn,row[0])
        from backend.domain import parse
        assert all(parse(bar['t'])+timedelta(minutes=1)<=NOW for bar in value['value']['1min'])


@pytest.mark.parametrize('case',['freshness','entitlement'])
def test_validation_expiring_in_flight_rejected_before_storage(tmp_path,monkeypatch,case):
    approve(monkeypatch);started=NOW+timedelta(seconds=15)
    if case=='entitlement':monkeypatch.setenv('MOBILE_XAU_ENTITLEMENT_UNTIL',stamp(started+timedelta(seconds=4)))
    def mutate(data,tf):
        if case=='freshness' and tf=='1min':data.update(body('xau',started-timedelta(minutes=2),tf))
    with Runtime(tmp_path/'m.db') as runtime:
        wire(runtime,monkeypatch,[started],mutate)
        runtime.collector.collector.clock=lambda:started+timedelta(seconds=8)
        observations,audits=runtime.collector.collector.collect('receipt-boundary',started)
        assert observations==[]
        assert audits[0]['reason']=='VALIDATION_AT_RECEIPT_FAILED'
        with runtime.read() as conn:assert conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]==0
def test_4h_aggregate_matches_twelve_data_anchor():
    start = NOW.replace(hour=1, minute=0, second=0, microsecond=0)
    minutes = []

    for i in range(480):
        at = start + timedelta(minutes=i)
        price = 2000 + i / 100
        minutes.append({
            't': stamp(at),
            'o': price,
            'h': price + 1,
            'l': price - 1,
            'c': price + 0.5,
        })

    native=[minutes[0],minutes[240]]
    anchor=StrictXauAdapter._native_4h_anchor(native,NOW)
    bars = StrictXauAdapter._aggregate(minutes, '4h',anchor)

    expected = [
        stamp(NOW.replace(hour=1, minute=0, second=0, microsecond=0)),
        stamp(NOW.replace(hour=5, minute=0, second=0, microsecond=0)),
    ]

    assert [bar['t'] for bar in bars] == expected
    assert bars[0]['o']==minutes[0]['o'] and bars[0]['c']==minutes[239]['c']
    assert bars[0]['h']==max(row['h'] for row in minutes[:240])
    assert bars[0]['l']==min(row['l'] for row in minutes[:240])


@pytest.mark.parametrize('offset',[0,3600,7200,10800])
def test_provider_anchor_bootstrap_steady_and_restart(monkeypatch,offset):
    from backend.domain import parse, INTERVALS
    from test_phase3f import spec
    calls=[]
    def read(self,spec,path,params,now,environ=None):
        tf=params['interval'];calls.append(tf);data=body('xau',now,tf)
        if tf=='4h':
            for row in data['values']:row['datetime']=stamp(parse(row['datetime'])+timedelta(seconds=offset))
        if tf=='1min':
            template=data['values'][-1]
            end=now.replace(second=0,microsecond=0)
            data['values']=[dict(template,datetime=stamp(end-timedelta(minutes=1000-i))) for i in range(1000)]
        return Response(data,digest(data),True,None) # Offline boundary test, not real evidence.
    monkeypatch.setattr(NativeHTTP,'read',read)
    adapter=StrictXauAdapter(spec('xau'),environ={'OFFLINE_TEST_TOKEN':'offline-anchor-placeholder'})
    bootstrap=adapter._mobile_xau(NOW)
    assert len(calls)==5
    assert bootstrap.details['aggregation_anchors_seconds']=={'4h':offset}
    later=NOW+timedelta(hours=4)
    steady=adapter._mobile_xau(later)
    assert len(calls)==6 and calls[-1]=='1min'
    bars=steady.envelope['value']['4h']
    assert all(int(parse(row['t']).timestamp())%14400==offset for row in bars)
    expected=int(later.timestamp())-((int(later.timestamp())-offset)%14400)-14400
    assert int(parse(bars[-1]['t']).timestamp())==expected
    assert steady.details['aggregation_anchors_seconds']=={'4h':offset}
    for tf in ('5min','15min','1h'):
        assert all(int(parse(row['t']).timestamp())%INTERVALS[tf]==0 for row in steady.envelope['value'][tf])
    restarted=StrictXauAdapter(spec('xau'),environ={'OFFLINE_TEST_TOKEN':'offline-anchor-placeholder'})
    replay_bootstrap=restarted._mobile_xau(later)
    assert len(calls)==11 # Fresh native bootstrap re-establishes the origin; no guessed default.
    assert restarted._mobile_4h_anchor==offset
    assert replay_bootstrap.details['aggregation_anchors_seconds']==steady.details['aggregation_anchors_seconds']


@pytest.mark.parametrize('case',['missing','single','mixed','duplicate','future','fractional'])
def test_ambiguous_native_anchor_fails_closed(case):
    start=NOW.replace(hour=1,minute=0,second=0,microsecond=0)
    rows=[{'t':stamp(start)},{'t':stamp(start+timedelta(hours=4))}]
    if case=='missing':rows=[]
    if case=='single':rows=rows[:1]
    if case=='mixed':rows[1]['t']=stamp(start+timedelta(hours=5))
    if case=='duplicate':rows[1]=rows[0]
    if case=='future':rows[1]['t']=stamp(NOW+timedelta(hours=4))
    if case=='fractional':rows[1]['t']=stamp(start+timedelta(hours=4,seconds=1))
    with pytest.raises(ValueError):StrictXauAdapter._native_4h_anchor(rows,NOW)


def test_4h_requires_explicit_native_anchor():
    with pytest.raises(ValueError,match='NATIVE_4H_ANCHOR_REQUIRED'):
        StrictXauAdapter._aggregate([],'4h')


def test_native_4h_diagnostics_expose_only_anchor_shape():
    from backend.domain import parse
    rows=[
        {'t':stamp(NOW.replace(hour=8,minute=0,second=0,microsecond=0))},
        {'t':stamp(NOW.replace(hour=9,minute=0,second=0,microsecond=0))},
        {'t':stamp(NOW.replace(hour=12,minute=0,second=0,microsecond=0))},
    ]
    value=StrictXauAdapter._native_4h_diagnostic(rows,NOW+timedelta(hours=5))
    assert value['row_count']==3
    assert value['valid_clock_rows']==3
    assert value['anchor_counts']=={'0':2,'3600':1}
    assert value['latest_4h_open']==stamp(parse(rows[-1]['t']))
    assert set(value)=={'row_count','valid_clock_rows','anchor_counts','latest_4h_open','latest_4h_close_age_seconds'}


def test_anchor_change_requires_evidenced_alignment_epoch():
    source='NATIVE_1H_REBUCKETED_TO_CURRENT_NATIVE_4H_ANCHOR'
    rows=[]
    start=NOW-timedelta(hours=4*60)
    for i in range(60):
        at=start+timedelta(hours=4*i)
        rows.append({'t':stamp(at),'o':2000+i,'h':2002+i,'l':1999+i,'c':2001+i})
    old={'version':'mobile-twelve-xau-4h-v1','anchor_seconds':3600}
    new={'version':'mobile-twelve-xau-4h-v1','anchor_seconds':0,
         'provider_identity':'provider-id','bootstrap_at':stamp(NOW),
         'native_bars':rows,'raw_hash':'a'*64,'epoch_source':source}
    new['epoch_id']=digest([new['version'],new['provider_identity'],0,source,
                            rows[0]['t'],rows[-1]['t'],new['raw_hash']])
    details={'aggregation_anchor_source':source}
    diagnostics={'anchor_counts':{'0':10,'3600':114}}
    assert ReceiptValidatedCollector._valid_anchor_epoch_transition(old,new,details,diagnostics)
    assert not ReceiptValidatedCollector._valid_anchor_epoch_transition(
        old,dict(new,epoch_id='bad'),details,diagnostics)
    assert not ReceiptValidatedCollector._valid_anchor_epoch_transition(
        old,new,details,{'anchor_counts':{'0':1,'3600':123}})


def test_mixed_recent_anchor_rebuilds_4h_from_native_1h():
    from backend.domain import parse, INTERVALS
    latest=NOW.replace(hour=8,minute=0,second=0,microsecond=0)
    current=[]
    for i in range(7):
        at=latest-timedelta(hours=4*(6-i))
        current.append({'t':stamp(at),'o':2100+i,'h':2102+i,'l':2099+i,'c':2101+i})
    old_last=parse(current[0]['t'])-timedelta(hours=3)
    old=[]
    for i in range(117):
        at=old_last-timedelta(hours=4*(116-i))
        old.append({'t':stamp(at),'o':1900+i,'h':1902+i,'l':1899+i,'c':1901+i})
    raw_4h=old+current
    with pytest.raises(ValueError,match='AMBIGUOUS_NATIVE_4H_ANCHOR'):
        StrictXauAdapter._native_4h_bootstrap_window(raw_4h,NOW)
    anchor=StrictXauAdapter._current_native_4h_anchor(raw_4h,NOW)
    assert anchor==0
    hourly=[]
    end=NOW.replace(hour=13,minute=0,second=0,microsecond=0)
    for i in range(300):
        at=end-timedelta(hours=299-i)
        hourly.append({'t':stamp(at),'o':2000+i,'h':2002+i,'l':1999+i,'c':2001+i})
    rebuilt=StrictXauAdapter._aggregate_from_base(hourly,'4h',INTERVALS['1h'],anchor)
    assert len(rebuilt)>=60
    assert all(int(parse(row['t']).timestamp())%INTERVALS['4h']==0 for row in rebuilt)
    assert parse(rebuilt[-1]['t'])+timedelta(hours=4)<=NOW


def test_mixed_native_4h_anchor_selects_recent_verified_window():
    from backend.domain import parse
    last=NOW.replace(hour=8,minute=0,second=0,microsecond=0)
    rows=[]
    for i in range(61):
        at=last-timedelta(hours=4*(60-i))
        rows.append({'t':stamp(at),'o':2000+i,'h':2002+i,'l':1999+i,'c':2001+i})
    extra=parse(rows[0]['t'])-timedelta(hours=3)
    rows.insert(0,{'t':stamp(extra),'o':1990,'h':1992,'l':1989,'c':1991})
    selected,anchor=StrictXauAdapter._native_4h_bootstrap_window(rows,NOW)
    assert anchor==0
    assert len(selected)==61
    assert all(int(parse(row['t']).timestamp())%14400==0 for row in selected)


def test_non_epoch_native_anchor_still_fails_closed_in_frozen_validator(tmp_path,monkeypatch):
    # The shared frozen domain validator requires epoch alignment. Do not silently
    # shift provider timestamps or weaken that gate in this aggregation-only patch.
    from backend.domain import parse
    approve(monkeypatch)
    def shifted(data,tf):
        if tf=='4h':
            for row in data['values']:row['datetime']=stamp(parse(row['datetime'])+timedelta(hours=1))
    with Runtime(tmp_path/'m.db') as runtime:
        wire(runtime,monkeypatch,[NOW],shifted)
        adapter=runtime.collector.collector.providers[runtime.specs[0].name]
        with pytest.raises(ValueError,match='XAU_VALIDATION_FAILED'):adapter.acquire(NOW)
        assert adapter._mobile_frames=={} and adapter._mobile_4h_anchor is None
