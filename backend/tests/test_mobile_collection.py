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
from backend.mobile.collection import StrictXauAdapter
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


def test_approval_change_requires_new_database(tmp_path,monkeypatch):
    path=tmp_path/'m.db'
    with Runtime(path):pass
    approve(monkeypatch)
    with pytest.raises(RuntimeError,match='ORIGINAL_ENGINE_CONFIGURATION_REQUIRED'):
        with Runtime(path):pass


def test_enabled_render_lifespan_and_gets_are_read_only(tmp_path,monkeypatch):
    from backend.mobile.collection import MobileCollector
    approve(monkeypatch)
    monkeypatch.setenv('RENDER','true');monkeypatch.setenv('MOBILE_DISK_PATH',str(tmp_path))
    monkeypatch.setattr(Path,'is_mount',lambda p:True)
    starts=[]
    monkeypatch.setattr(MobileCollector,'start',lambda self:starts.append(self))
    monkeypatch.setattr(NativeHTTP,'read',lambda *a,**k:pytest.fail('GET acquired data'))
    with TestClient(create_app(tmp_path/'m.db',clock=lambda:NOW)) as client:
        assert len(starts)==1
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
