"""Parallel-service integration tests. Synthetic inputs stay in tests, never service code."""
from datetime import timedelta
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3
import gc

import pytest
from fastapi.testclient import TestClient

from backend.mobile.api import create_app, fixture, public
from backend.mobile.runtime import Runtime, configured_path
from backend.mobile.archive import archive
from backend.domain import stamp, canonical, digest
from backend.forward.contracts import CRITICAL
from backend.realdata.transport import NativeHTTP
from test_phase1 import NOW
from test_phase3f import spec
from test_phase3f_forensic import offline_cycle


ENDPOINTS=('/health','/signal','/research-signal','/performance','/history','/trades','/system-status')


def seed(runtime, at=NOW):
    runtime.native_specs=tuple(spec(c) for c in CRITICAL)
    runtime.store.specs={s.name:s for s in runtime.native_specs}
    return offline_cycle(runtime,at)


@pytest.mark.parametrize('endpoint',ENDPOINTS)
def test_empty_modern_api(tmp_path,endpoint,monkeypatch):
    monkeypatch.setattr(NativeHTTP,'read',lambda *a,**k:pytest.fail('Provider network called'))
    with TestClient(create_app(tmp_path/'mobile.sqlite3',clock=lambda:NOW)) as client:
        response=client.get(endpoint)
        assert response.status_code==200
        assert response.headers['cache-control']=='no-store'
        json.dumps(response.json(),allow_nan=False)
        signal=client.get('/signal').json()
        assert signal['direction']==signal['champion']['direction']=='NO_TRADE'
        assert signal['confidence'] is None and 'entry' not in signal
        assert signal['readiness']['status']=='NOT_READY'
        assert signal['data_mode']=='UNAVAILABLE'


def test_candidate_readiness_and_broker_safety(tmp_path,monkeypatch):
    monkeypatch.setenv('TWELVE_DATA_API_KEY','unused-test-placeholder')
    with TestClient(create_app(tmp_path/'m.sqlite3')) as client:
        status=client.get('/system-status').json()
        assert status['collection_enabled'] is False
        assert status['status']=='NOT_READY'
        assert status['promotion']=='PROMOTION_INELIGIBLE'
        assert status['automatic_promotion'] is False
        assert status['champion']=='PHASE3B_CHAMPION'
        assert all(p['approval']=='UNAPPROVED' and p['configuration']=='UNCONFIGURED' for p in status['providers'])
        assert status['forex_factory']['role']=='SECONDARY_CROSS_CHECK_ONLY'
        assert 'unused-test-placeholder' not in client.get('/signal').text
        assert client.post('/signal',json={'direction':'BUY'}).status_code==405
        assert client.post('/trade').status_code==404


@pytest.mark.parametrize('flag',['TRUE','1','yes','invalid'])
def test_collection_rejects_invalid_toggle(tmp_path,monkeypatch,flag):
    monkeypatch.setenv('MOBILE_COLLECTION_ENABLED',flag)
    with pytest.raises(RuntimeError,match='INVALID_COLLECTION_FLAG'):
        Runtime(tmp_path/'m.sqlite3')


def test_fixture_decision_fields_and_isolation(tmp_path):
    app=create_app(tmp_path/'m.sqlite3',clock=lambda:NOW)
    with TestClient(app) as client:
        result=seed(app.state.runtime)
        response=client.get('/signal')
        assert response.status_code==200,response.text
        value=response.json()
        assert value['decision_id']==result['decision_id']
        assert value['direction']=='NO_TRADE' and value['entry'] is None
        assert value['data_mode']==value['champion']['data_mode']=='TEST_DATA'
        for field in ('hidden_state','intelligence','components','buy_score','atr','monitor_price'):
            assert field in value
        assert value['challenger']['source']=='ADAPTIVE_CHALLENGER'
        assert value['champion']['source']=='PHASE3B_CHAMPION'
        assert value['readiness']['status']=='NOT_READY'
        assert app.state.runtime.engine.replay(result['decision_id'])['matches']
        assert app.state.runtime.engine.replay_meta(result['decision_id'])['matches']
        performance=client.get('/performance').json()
        assert performance['real_five_minute_decisions']==0
        assert all(c['metrics']=={} for c in performance['cohorts'].values())
        assert client.get('/history').json()['items'][0]['data_mode']=='TEST_DATA'


def test_freshness_recomputed_and_no_future_decisions(tmp_path):
    current=[NOW]
    app=create_app(tmp_path/'m.sqlite3',clock=lambda:current[0])
    with TestClient(app) as client:
        seed(app.state.runtime)
        before=client.get('/signal').json()
        current[0]+=timedelta(hours=7)
        after=client.get('/signal').json()
        assert after['freshness']['status']=='STALE'
        assert len(after['freshness']['errors'])>len(before['freshness']['errors'])
        assert after['data_mode']=='TEST_DATA'
        current[0]=NOW-timedelta(minutes=1)
        assert client.get('/signal').json()['data_mode']=='UNAVAILABLE'
        assert client.get('/history').json()['items']==[]


def test_restart_persistence_and_archive_exact_replay(tmp_path):
    path=tmp_path/'m.sqlite3'
    with Runtime(path) as runtime:
        identity=seed(runtime)['decision_id']
        before=runtime.engine.replay(identity)
        companion=runtime.engine.replay_meta(identity)
    output=tmp_path/'archive.sqlite3'
    assert archive(path,output)['replay']['status']=='PASSED'
    for db in (path,output):
        with Runtime(db) as runtime:
            assert runtime.engine.replay(identity)==before
            assert runtime.engine.replay_meta(identity)==companion
            assert runtime.replay['checked_decisions']==1
    with pytest.raises(ValueError,match='ARCHIVE_MUST_BE_NEW'):archive(path,output)


def test_reads_do_not_grow_evidence_or_change_history(tmp_path):
    app=create_app(tmp_path/'m.sqlite3',clock=lambda:NOW)
    with TestClient(app) as client:
        runtime=app.state.runtime
        with runtime.read() as conn:before=list(conn.iterdump())
        for _ in range(8):
            for endpoint in ENDPOINTS:assert client.get(endpoint).status_code==200
        with runtime.read() as conn:assert list(conn.iterdump())==before


def test_storage_budget_and_database_failures_fail_closed(tmp_path):
    app=create_app(tmp_path/'m.sqlite3',clock=lambda:NOW)
    with TestClient(app,raise_server_exceptions=False) as client:
        runtime=app.state.runtime
        runtime.limit=1
        assert client.get('/health').status_code==503
        assert 'STORAGE_BUDGET_EXCEEDED' in client.get('/system-status').json()['reasons']
        runtime.limit=512*1024*1024
        with runtime.store.connect() as conn:conn.execute('DROP TABLE forward_ledger')
        # Liveness and signal reads intentionally avoid a full integrity audit on
        # every HTTP request. Startup/collector ownership performs that audit.
        response=client.get('/signal')
        assert response.status_code==200
        assert response.json()['direction']=='NO_TRADE' and str(tmp_path) not in response.text


def test_dedicated_database_and_single_owner(tmp_path):
    path=tmp_path/'foreign.sqlite3'
    with sqlite3.connect(path) as conn:conn.execute('CREATE TABLE untouched(x)')
    before=path.read_bytes()
    with pytest.raises(RuntimeError,match='DEDICATED_MOBILE'):
        with Runtime(path):pass
    assert path.read_bytes()==before
    with Runtime(tmp_path/'m.sqlite3'):
        with pytest.raises(RuntimeError,match='Only one'):
            with Runtime(tmp_path/'m.sqlite3'):pass


def test_render_requires_mount_and_separate_service(tmp_path,monkeypatch):
    monkeypatch.setenv('RENDER','true')
    with pytest.raises(RuntimeError,match='PERSISTENT_DISK'):configured_path(tmp_path/'m.sqlite3')
    monkeypatch.setattr(Path,'is_mount',lambda _:True)
    monkeypatch.setenv('MOBILE_DISK_PATH',str(tmp_path))
    monkeypatch.setenv('RENDER_SERVICE_NAME','xau-ai-trader-android')
    with pytest.raises(RuntimeError,match='LEGACY_SERVICE'):configured_path(tmp_path/'m.sqlite3')
    monkeypatch.setenv('RENDER_SERVICE_NAME','dardania-xautrade-ai-v2')
    with Runtime(tmp_path/'m.sqlite3') as runtime:assert runtime.engine.strategy_version=='phase3b-1'


@pytest.mark.parametrize('endpoint',['/history?limit=201','/trades?limit=0','/history?before_row=-1'])
def test_bounded_queries(tmp_path,endpoint):
    with TestClient(create_app(tmp_path/'m.sqlite3')) as client:assert client.get(endpoint).status_code==422


def test_no_secrets_or_nonfinite_json_projection():
    value=public({'secret':'redacted','api_key':'redacted','value':float('nan'),'nested':{'authorization':'redacted'}})
    assert value=={'value':None,'nested':{}}
    assert fixture({'a':{'data_mode':'TEST_DATA'}})


def test_forged_live_without_provenance_cannot_promote(tmp_path):
    app=create_app(tmp_path/'m.sqlite3',clock=lambda:NOW)
    with TestClient(app) as client:
        seed(app.state.runtime)
        response=client.get('/signal').json()
        # Even a fresh archived source is never promoted by this disabled service.
        assert response['provenance']['live_validated_for_service'] is False
        assert response['readiness']['status']=='NOT_READY'
        assert response['direction']=='NO_TRADE'


def test_archive_closes_handles_without_garbage_collection(tmp_path):
    path=tmp_path/'source.sqlite3';output=tmp_path/'verified.sqlite3'
    with Runtime(path):pass
    gc.disable()
    try:
        assert archive(path,output)['status']=='ARCHIVE_VERIFIED'
        output.unlink()  # Windows catches leaked backup/verification handles here.
    finally:
        gc.enable();gc.collect()


def test_finalized_fixture_outcomes_remain_test_and_challenger_excluded(tmp_path):
    app=create_app(tmp_path/'m.sqlite3',clock=lambda:NOW)
    with TestClient(app) as client:
        runtime=app.state.runtime;decision=seed(runtime)
        trade={'id':'offline-outcome','decision_id':decision['decision_id'],'status':'CLOSED','direction':'BUY',
               'entry':4300,'sl':4290,'opened_at':stamp(NOW-timedelta(minutes=1)),
               'closed_at':stamp(NOW),'result':'TP2','r_multiple':2}
        with runtime.store.transaction() as conn:
            for role in ('CHAMPION','ADAPTIVE_CHALLENGER'):
                runtime.store.event(conn,'offline-outcome:'+role,'NATIVE_OUTCOME',stamp(NOW),
                    {'source':role,'trade':trade,'known_at':stamp(NOW),'gross_r':2,'simulated_net_r':1.8})
        response=client.get('/trades').json()
        assert len(response['items'])==1
        assert response['items'][0]['source']=='CHAMPION'
        assert response['items'][0]['data_mode']=='TEST_DATA'
        assert response['items'][0]['simulated_net_r']==1.8


def test_future_finalization_is_not_visible(tmp_path):
    app=create_app(tmp_path/'m.sqlite3',clock=lambda:NOW)
    with TestClient(app) as client:
        runtime=app.state.runtime;decision=seed(runtime)
        with runtime.store.transaction() as conn:
            runtime.store.event(conn,'future-outcome','NATIVE_OUTCOME',stamp(NOW+timedelta(minutes=1)),
                {'source':'CHAMPION','trade':{'id':'future','decision_id':decision['decision_id']},
                 'known_at':stamp(NOW+timedelta(minutes=1))})
        assert client.get('/trades').json()['items']==[]


@pytest.mark.parametrize('marker',[{'is_test':True},{'is_demo':True},{'environment':'sandbox'},{'data_status':'FIXTURE_DATA'}])
def test_all_fixture_markers_are_preserved(marker):
    assert fixture({'nested':marker})


def test_mobile_xau_freshness_window():
    closes = {
        '1min': stamp(NOW - timedelta(seconds=190)),
        '5min': stamp(NOW),
        '15min': stamp(NOW),
        '1h': stamp(NOW),
        '4h': stamp(NOW),
    }

    from backend.mobile.api import mobile_freshness_errors

    assert 'STALE_DATA:1min' not in mobile_freshness_errors(closes, NOW)

    closes['1min'] = stamp(NOW - timedelta(seconds=241))
    assert 'STALE_DATA:1min' in mobile_freshness_errors(closes, NOW)
