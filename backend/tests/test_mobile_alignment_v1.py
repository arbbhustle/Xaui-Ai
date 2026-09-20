"""Explicitly offline provider-attestation simulations; never real-provider proof."""
from copy import deepcopy
from datetime import timedelta
import json
import pytest

from backend.domain import closed_frame as legacy_closed, parse, stamp, digest
from backend.forward.contracts import normalize as legacy_normalize
from backend.mobile.collection import StrictXauAdapter
from backend.mobile.runtime import Runtime
from backend.mobile.archive import archive
from backend.mobile.xau_alignment_v1 import VERSION, META, validate_evidence, normalize_acquisition, closed_frame
from backend.realdata.transport import NativeHTTP, Response
from backend.phase3b import implementation_hash
from backend.phase3c import BASE_HASH
from test_phase1 import NOW
from test_phase3f import body
from test_mobile_collection import approve


def setup(monkeypatch,offset=3600):
    approve(monkeypatch)
    monkeypatch.setenv('MOBILE_XAU_VALIDATOR',VERSION)
    calls=[]
    def read(self,spec,path,params,now,environ=None):
        tf=params['interval'];calls.append(tf);data=body('xau',now,tf)
        if tf=='4h':
            for row in data['values']:row['datetime']=stamp(parse(row['datetime'])+timedelta(seconds=offset))
        return Response(data,digest(data),True,None)
    monkeypatch.setattr(NativeHTTP,'read',read)
    return calls


@pytest.mark.parametrize('offset',[0,3600,7200,10800])
def test_approved_native_offsets_only_scoped_path(tmp_path,monkeypatch,offset):
    setup(monkeypatch,offset)
    with Runtime(tmp_path/'m.db') as runtime:
        adapter=runtime.collector.collector.providers[runtime.specs[0].name]
        acquisition=adapter.acquire(NOW)
        rows=acquisition.envelope['value']['4h']
        row=normalize_acquisition(adapter.spec,acquisition,NOW)
        assert not row['errors'] and row['health']=='HEALTHY'
        assert all(int(parse(r['t']).timestamp())%14400==offset for r in row['value']['4h'])
        original=legacy_normalize(adapter.spec.legacy,acquisition.envelope,NOW,acquisition.raw_hash)
        assert ('INVALID_DATA:4h' in original['errors']) == bool(offset)
        assert bool(legacy_closed(rows,'4h',NOW)[1])==bool(offset)
        # Even the isolated validator without the authenticated scope stays strict.
        assert bool(closed_frame(rows,'4h',NOW)[1])==bool(offset)
        assert implementation_hash()==BASE_HASH


@pytest.mark.parametrize('case',['version','provider','missing','mixed','anchor','unsupported','future','stale',
                               'unapproved','transport','entitlement','wrong_symbol'])
def test_forged_alignment_rejected(tmp_path,monkeypatch,case):
    setup(monkeypatch)
    with Runtime(tmp_path/'m.db') as runtime:
        adapter=runtime.collector.collector.providers[runtime.specs[0].name]
        value=deepcopy(adapter.acquire(NOW));evidence=value.details[META]
        if case=='version':evidence['version']='unknown'
        if case=='provider':evidence['provider_identity']='wrong'
        if case=='missing':value.details.pop(META)
        if case=='mixed':evidence['native_bars'][0]['t']=stamp(parse(evidence['native_bars'][0]['t'])+timedelta(hours=1))
        if case=='anchor':evidence['anchor_seconds']=7200
        if case=='unsupported':evidence['anchor_seconds']=1800
        if case=='future':evidence['bootstrap_at']=stamp(NOW+timedelta(seconds=1))
        if case=='stale':
            for bar in evidence['native_bars']:bar['t']=stamp(parse(bar['t'])-timedelta(days=1))
        if case=='transport':value.verification['real_transport']=False
        if case=='wrong_symbol':value.verification['instrument']='XAG/USD'
        from dataclasses import replace
        spec=adapter.spec
        if case=='unapproved':spec=replace(spec,approved=False)
        if case=='entitlement':spec=replace(spec,entitlement_until=stamp(NOW))
        with pytest.raises(ValueError):normalize_acquisition(spec,value,NOW)


@pytest.mark.parametrize('tf',['1min','5min','15min','1h','4h'])
def test_invalid_candle_alignment_not_relaxed(tmp_path,monkeypatch,tf):
    setup(monkeypatch)
    with Runtime(tmp_path/'m.db') as runtime:
        adapter=runtime.collector.collector.providers[runtime.specs[0].name]
        value=deepcopy(adapter.acquire(NOW))
        row=value.envelope['value'][tf][0]
        row['t']=stamp(parse(row['t'])+timedelta(seconds=1))
        assert 'INVALID_DATA:'+tf in normalize_acquisition(adapter.spec,value,NOW)['errors']


def test_scoped_collection_restart_archive_replay_and_anchor_change(tmp_path,monkeypatch):
    calls=setup(monkeypatch);path=tmp_path/'m.db';current=[NOW]
    with Runtime(path) as runtime:
        runtime.collector.clock=lambda:current[0];runtime.collector.collector.clock=lambda:current[0]
        for i in range(3):
            current[0]=NOW+timedelta(minutes=i)
            result=runtime.collector._once()
            assert result.get('status')!='COLLECTION_OR_INTEGRITY_FAILURE'
        assert len(calls)==7 # Native bootstrap then exactly one request per cycle.
        with runtime.read() as conn:
            row=conn.execute('SELECT id,payload FROM decisions LIMIT 1').fetchone();assert row
            identity=row['id'];decision=json.loads(row['payload'])
            assert decision['direction']=='NO_TRADE'
            assert 'INVALID_DATA:4h' not in decision['veto_codes']
            snapshot=json.loads(conn.execute('SELECT payload FROM snapshots WHERE id=?',(decision['snapshot_id'],)).fetchone()[0])
            assert snapshot['mobile_validator_version']==VERSION
            assert snapshot['frames']['4h'] and all(int(parse(r['t']).timestamp())%14400==3600 for r in snapshot['frames']['4h'])
        before=runtime.engine.replay(identity);meta=runtime.engine.replay_meta(identity)
        assert before['matches'] and meta['matches']
    with Runtime(path) as runtime:
        assert runtime.replay['status']=='PASSED'
        assert runtime.engine.replay(identity)==before
        assert runtime.engine.replay_meta(identity)==meta
        current[0]+=timedelta(minutes=2)
        runtime.collector.clock=lambda:current[0];runtime.collector.collector.clock=lambda:current[0]
        runtime.collector._once();assert len(calls)==12 # Re-bootstrap on restart.
    assert archive(path,tmp_path/'archive.db')['replay']['status']=='PASSED'
    setup(monkeypatch,7200)
    with Runtime(path) as runtime:
        current[0]+=timedelta(minutes=2)
        runtime.collector.clock=lambda:current[0];runtime.collector.collector.clock=lambda:current[0]
        with runtime.read() as conn:count=conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]
        runtime.collector._once()
        with runtime.read() as conn:assert conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]==count


def test_legacy_database_identity_replay_unchanged(tmp_path,monkeypatch):
    from test_mobile_api import seed
    monkeypatch.delenv('MOBILE_XAU_VALIDATOR',raising=False)
    path=tmp_path/'old.db'
    with Runtime(path) as runtime:
        identity=seed(runtime)['decision_id'];before=runtime.engine.replay(identity)
        with runtime.read() as conn:metadata=conn.execute('SELECT identity FROM mobile_metadata').fetchone()[0]
        assert 'validator_version' not in json.loads(metadata)
    monkeypatch.setenv('MOBILE_XAU_VALIDATOR',VERSION)
    with pytest.raises(RuntimeError,match='ORIGINAL_ENGINE_CONFIGURATION_REQUIRED'):
        with Runtime(path):pass
    monkeypatch.setenv('MOBILE_XAU_VALIDATOR','legacy')
    with Runtime(path) as runtime:
        assert runtime.engine.replay(identity)==before
        with runtime.read() as conn:assert conn.execute('SELECT identity FROM mobile_metadata').fetchone()[0]==metadata


def test_unknown_version_fails_startup(tmp_path,monkeypatch):
    monkeypatch.setenv('MOBILE_XAU_VALIDATOR','unknown')
    with pytest.raises(RuntimeError,match='UNKNOWN_MOBILE_VALIDATOR'):Runtime(tmp_path/'m.db')


def test_scope_cannot_relax_legacy_or_leak_between_threads(tmp_path,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from backend.mobile.xau_alignment_v1 import alignment_scope
    setup(monkeypatch)
    with Runtime(tmp_path/'m.db') as runtime:
        adapter=runtime.collector.collector.providers[runtime.specs[0].name]
        value=adapter.acquire(NOW);rows=value.envelope['value']['4h']
        with alignment_scope(3600):
            assert not closed_frame(rows,'4h',NOW)[1]
            assert legacy_closed(rows,'4h',NOW)[1]==['INVALID_DATA:4h']
            with ThreadPoolExecutor(max_workers=1) as pool:
                assert pool.submit(closed_frame,rows,'4h',NOW).result()[1]==['INVALID_DATA:4h']
        assert closed_frame(rows,'4h',NOW)[1]==['INVALID_DATA:4h']


@pytest.mark.parametrize('case',['freshness','entitlement'])
def test_scoped_receipt_expiry_rejects_before_storage(tmp_path,monkeypatch,case):
    from test_mobile_collection import wire
    setup(monkeypatch);started=NOW+timedelta(seconds=15)
    if case=='entitlement':monkeypatch.setenv('MOBILE_XAU_ENTITLEMENT_UNTIL',stamp(started+timedelta(seconds=4)))
    def mutate(data,tf):
        if case=='freshness' and tf=='1min':data.update(body('xau',started-timedelta(minutes=2),tf))
        if tf=='4h':
            for row in data['values']:row['datetime']=stamp(parse(row['datetime'])+timedelta(hours=1))
    with Runtime(tmp_path/'m.db') as runtime:
        wire(runtime,monkeypatch,[started],mutate)
        runtime.collector.collector.clock=lambda:started+timedelta(seconds=8)
        observations,_=runtime.collector.collector.collect('receipt-boundary',started)
        assert observations==[]
        with runtime.read() as conn:assert conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]==0


def test_snapshot_wrong_version_and_frames_reject_replay(tmp_path,monkeypatch):
    from backend.domain import Policy
    setup(monkeypatch);current=[NOW]
    with Runtime(tmp_path/'m.db') as runtime:
        runtime.collector.clock=lambda:current[0];runtime.collector.collector.clock=lambda:current[0]
        for i in range(3):
            current[0]=NOW+timedelta(minutes=i);runtime.collector._once()
        with runtime.read() as conn:
            snapshot=json.loads(conn.execute('SELECT s.payload FROM snapshots s JOIN decisions d ON d.snapshot_id=s.id LIMIT 1').fetchone()[0])
        invalid=deepcopy(snapshot);invalid['mobile_validator_version']='wrong'
        with pytest.raises(ValueError,match='VALIDATOR_VERSION_REQUIRED'):runtime.engine.evaluate_snapshot(invalid,Policy())
        invalid=deepcopy(snapshot);invalid['frames']['4h'][0]['t']=stamp(NOW)
        with pytest.raises(ValueError,match='CAPTURE_FRAME_MISMATCH'):runtime.engine.evaluate_snapshot(invalid,Policy())


def test_newly_derived_native_4h_bar_passes_scoped_validation(tmp_path,monkeypatch):
    setup(monkeypatch)
    original=NativeHTTP.read;calls=[]
    def read(self,spec,path,params,now,environ=None):
        calls.append(params['interval'])
        response=original(self,spec,path,params,now,environ)
        if params['interval']=='1min':
            data=deepcopy(response.payload);template=data['values'][-1]
            end=now.replace(second=0,microsecond=0)
            data['values']=[dict(template,datetime=stamp(end-timedelta(minutes=1000-i))) for i in range(1000)]
            return Response(data,digest(data),True,None)
        return response
    monkeypatch.setattr(NativeHTTP,'read',read)
    with Runtime(tmp_path/'m.db') as runtime:
        adapter=runtime.collector.collector.providers[runtime.specs[0].name]
        first=adapter.acquire(NOW)
        later=NOW+timedelta(hours=4)
        second=adapter.acquire(later)
        assert len(calls)==6 and calls[-1]=='1min'
        assert second.details[META]==first.details[META]
        assert second.envelope['value']['4h'][-1]['t']==stamp(NOW.replace(hour=13,minute=0,second=0,microsecond=0))
        assert normalize_acquisition(adapter.spec,second,later)['health']=='HEALTHY'
