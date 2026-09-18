"""Final adversarial regressions, isolated TEST fixtures only."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json,threading,uuid
import httpx,pytest
from backend.domain import stamp,parse,digest,canonical
from backend.realdata.store import RealStore
from backend.realdata.runner import RealRunner
from backend.realdata.outcomes import finalize
from backend.realdata.transport import NativeHTTP
from backend.realdata.adapters import NativeAdapter,synthetic_payload
from backend.realdata.verification import verify
from backend.realdata.reports import real_capture,report,metrics
from backend.realdata.forexfactory import event_signature
from backend.forward.contracts import CRITICAL,normalize,select,quality
from backend.forward.providers import ProviderFailure
from test_phase3f import spec,adapter,body,verified_row,NOW
from test_phase3f_forensic import offline_cycle


@pytest.mark.parametrize('marker',[{'is_demo':True},{'is_test':True},{'is_synthetic':True},{'environment':'sandbox'},{'data_mode':'FIXTURE'}])
def test_nested_synthetic_flags(marker):assert synthetic_payload({'meta':[marker]})


def test_timeout_sanitized():
    token=uuid.uuid4().hex;s=spec()
    def fail(r):raise httpx.ReadTimeout(token)
    with pytest.raises(ProviderFailure) as exc:NativeHTTP(httpx.MockTransport(fail)).read(s,'/feed',{},NOW,{s.secret_env:token})
    assert exc.value.code=='PROVIDER_TIMEOUT' and token not in str(exc.value)


@pytest.mark.parametrize('raw',['not json','null','3','"text"'])
def test_malformed_http_payload(raw):
    s=spec()
    with pytest.raises(ProviderFailure):NativeHTTP(httpx.MockTransport(lambda r:httpx.Response(200,text=raw))).read(s,'/feed',{},NOW,{s.secret_env:uuid.uuid4().hex})


def test_out_of_order_and_duplicate_cadence():
    s=spec();first=verify(verified_row(s,NOW),[],s,NOW)
    for minute in (1,2):
        row=verified_row(s,NOW+timedelta(minutes=minute));row['observed_at']=first['observed_at']
        assert not verify(row,[first],s,NOW+timedelta(minutes=minute))['native']['live_verified']
    row=verified_row(s,NOW);row['observed_at']=stamp(NOW-timedelta(hours=1))
    assert 'OUT_OF_ORDER_SOURCE' in verify(row,[first],s,NOW+timedelta(minutes=1))['errors']


def test_delayed_native_frequency_never_live():
    s=spec();a=adapter(s).acquire(NOW)
    # Pure classification unit: HTTP success/provenance alone cannot override daily cadence.
    from backend.realdata.transport import Response
    response=Response(body('dxy'),digest(body('dxy')),True,None)
    result=NativeAdapter(s).finish(a.envelope['value'],NOW,NOW,[response],{},frequency='Daily')
    assert result.envelope['data_mode']=='DELAYED_DATA'


def test_old_gap_trust_does_not_remove_entry_veto():
    s=spec('xau');previous=[]
    for i in range(3):
        at=NOW+timedelta(minutes=i);row=verified_row(s,at);row.update(errors=['CANDLE_GAP:1min'],health='DEGRADED')
        result=verify(row,previous,s,at);previous.append(result)
    assert result['data_mode']=='LIVE_DATA' and result['health']=='DEGRADED'
    assert select([s.legacy],[result],at)['vetoes']


def test_xau_jump_and_repetition_are_vetoed():
    s=spec('xau');row=verified_row(s,NOW);old=deepcopy(row);old['received_at']=stamp(NOW-timedelta(minutes=11))
    old['observed_at']=stamp(NOW-timedelta(minutes=11))
    assert 'REPEATED_GOLD_PRICE_WINDOW' in verify(row,[old],s,NOW)['errors']
    row['value']['1min'][-1]['c']*=1.1
    assert 'ABNORMAL_GOLD_JUMP' in verify(row,[old],s,NOW)['errors']


def test_weekend_is_not_live():
    s=spec('xau');sat=parse('2026-09-19T14:00:00+00:00');row=verified_row(s,NOW)
    assert 'GOLD_MARKET_CLOSED' in verify(row,[],s,sat)['errors']


def test_duplicate_calendar_id_conflicts_rejected():
    def mutate(raw):return raw+[dict(raw[0],Forecast='9')]
    with pytest.raises(ValueError,match='CONFLICTING_CALENDAR_EDITION'):adapter(spec('calendar'),mutate).acquire(NOW)


@pytest.mark.parametrize('a,b',[('Inflation Rate MoM','CPI m/m'),('Core PCE Price Index MoM','Core PCE Price Index m/m'),('Nonfarm Payrolls','Non-Farm Employment Change'),('Fed Interest Rate Decision','Federal Funds Rate'),('Fed Chairman Jerome Powell Speech','Fed Chair Powell Speaks')])
def test_cross_vendor_event_aliases(a,b):assert event_signature(a)==event_signature(b)


def test_adp_not_merged_with_nfp():assert event_signature('ADP Nonfarm Payrolls')!=event_signature('Nonfarm Payrolls')


def test_native_outcome_idempotent_and_no_forming_bar_leakage(tmp_path):
    store=RealStore(tmp_path/'n.sqlite');opened=NOW-timedelta(minutes=2)
    trade={'id':'offline','direction':'BUY','entry':100,'sl':99,'opened_at':stamp(opened),'closed_at':stamp(NOW),'exit_price':102}
    outcome={'trade':trade,'source':'CHAMPION','known_at':stamp(NOW),'gross_r':2,'simulated_net_r':1.5,
             'cost_assumptions':{'spread_points':.3,'slippage_points_per_side':.1}}
    with store.transaction() as conn:
        for observed,bars in [(opened+timedelta(seconds=30),[{'t':stamp(opened),'h':999,'l':1}]),
                              (NOW,[{'t':stamp(opened),'h':101,'l':99.5},{'t':stamp(opened+timedelta(minutes=1)),'h':105,'l':95}])]:
            snap={'observed_at':stamp(observed),'source_errors':{},'frames':{'1min':bars}}
            conn.execute('INSERT OR IGNORE INTO snapshots VALUES (?,?,?)',(digest(snap),stamp(observed),canonical(snap)))
        store.event(conn,'outcome:offline','OUTCOME',stamp(NOW),outcome)
        finalize(conn,store,NOW);finalize(conn,store,NOW+timedelta(hours=1))
        rows=conn.execute("SELECT payload_id FROM forward_ledger WHERE kind='NATIVE_OUTCOME'").fetchall()
        assert len(rows)==1;result=store.get(conn,rows[0][0])
        assert result['mfe_r']==2 and result['mae_r']==.5 and result['mfe_r_upper']==5
        assert result['duration_seconds']==120 and result['outcome_timestamp']==stamp(NOW)
        assert result['cost_assumptions']==outcome['cost_assumptions'] and result['simulated_net_r']==1.5
    assert store.verify()['storage_healthy']


def test_native_analytics_failure_isolated_from_champion(tmp_path,monkeypatch):
    runner=RealRunner(tmp_path/'n.sqlite',[spec(c) for c in CRITICAL],clock=lambda:NOW)
    def fail(*args):raise ValueError('offline failure')
    monkeypatch.setattr('backend.realdata.runner.finalize',fail)
    result=offline_cycle(runner,NOW)
    assert runner.engine.replay(result['decision_id'])['matches']
    assert 'NATIVE_OUTCOME_ANALYTICS_FAILED' in runner.status()['reasons']


def test_probability_metric_not_percent_scaled():
    row={'id':'a','opened_at':stamp(NOW),'closed_at':stamp(NOW+timedelta(minutes=1)),
        'gross_r':1,'net_r':.5,'prediction':.8,'prediction_at':stamp(NOW-timedelta(seconds=1)),'prediction_target':'POSITIVE_GROSS_R'}
    result=metrics([row]);assert result['brier_score']==pytest.approx(.04) and result['calibration_sample_count']==1


def test_calendar_first_observed_actual_and_revision_retained(tmp_path):
    s=spec('calendar');store=RealStore(tmp_path/'n.sqlite',[s])
    for i,actual in enumerate(('2.7','2.8')):
        def mutate(raw):
            raw[0].update(Date=stamp(NOW-timedelta(hours=1)),Actual=actual);return raw
        a=adapter(s,mutate).acquire(NOW);row=normalize(s.legacy,a.envelope,NOW+timedelta(minutes=i),a.raw_hash)
        row['native']={'verification':a.verification,'details':a.details}
        with store.transaction() as conn:store.archive_observation(conn,str(i),row)
    first=store.editions_as_of(s.legacy.identity,'calendar','7',NOW)
    later=store.editions_as_of(s.legacy.identity,'calendar','7',NOW+timedelta(minutes=1))
    assert first[0]['Actual']=='2.7' and later[-1]['Actual']=='2.8' and later[-1]['first_observed_actual']=='2.7'


def test_freshness_recalculated_on_response():
    s=spec();row=verified_row(s,NOW)
    assert quality(row,NOW+timedelta(minutes=10))['health']=='STALE'


def test_conflicting_source_selection_and_failover_explicit():
    s=spec();backup=replace(s,name='backup',priority=1)
    a=verified_row(s,NOW);b=verified_row(backup,NOW)
    b['value']['records'][-1]['value']+=10
    result=select([s.legacy,backup.legacy],[a,b],NOW)
    assert result['health']['dxy']['status']=='CONFLICTING'
    result=select([s.legacy,backup.legacy],[b],NOW,{'dxy':s.name})
    assert result['selected']['dxy']['provider']=='backup' and result['switches']


def test_later_real_cycle_cannot_reclassify_fixture_decision(tmp_path,monkeypatch):
    runner=RealRunner(tmp_path/'n.sqlite',[spec(c) for c in CRITICAL],clock=lambda:NOW)
    result=offline_cycle(runner,NOW)
    with runner.store.transaction() as conn:
        row=conn.execute("SELECT payload_id FROM forward_ledger WHERE kind='DECISION'").fetchone()
        evidence=runner.store.get(conn,row[0]);capture=runner.store.get_capture(conn,evidence['capture_id'])
        capture['unit_verified']=True
        evidence['capture_id']=runner.store.put_capture(conn,capture)
        runner.store.event(conn,'later-real-unit','DECISION',stamp(NOW+timedelta(minutes=1)),evidence)
    # Isolate the report's origin-check logic; no fixture is reclassified by provider code.
    monkeypatch.setattr('backend.realdata.reports.real_capture',lambda c:c.get('unit_verified',False))
    result=report(runner.store,NOW+timedelta(minutes=1))
    assert result['real_forward_evaluation_cycles']==1 and result['real_five_minute_decisions']==0


def test_no_native_proof_cannot_reach_data_ready(tmp_path,monkeypatch):
    runner=RealRunner(tmp_path/'n.sqlite',[spec(c) for c in CRITICAL],collection_enabled=True,clock=lambda:NOW)
    offline_cycle(runner,NOW)
    monkeypatch.setattr('backend.forward.runner.ForwardRunner.status',lambda *a,**k:{'status':'FORWARD_DEMO_READY','reasons':[],'replay':{'ok':True}})
    result=runner.status()
    assert result['status']=='NOT_READY' and 'NATIVE_PROVENANCE_UNVERIFIED' in result['reasons']


def test_readiness_requires_continuity_restart_and_replay(tmp_path,monkeypatch):
    from backend.forward.collection import set_control
    at=NOW
    runner=RealRunner(tmp_path/'n.sqlite',[spec(c) for c in CRITICAL],collection_enabled=True,clock=lambda:at)
    offline_cycle(runner,NOW)
    # State-machine unit test; provider validation is tested independently, never bypassed in runtime.
    monkeypatch.setattr('backend.realdata.reports.real_capture',lambda c:True)
    monkeypatch.setattr('backend.forward.runner.ForwardRunner.status',lambda *a,**k:{'status':'DATA_READY','reasons':[],'replay':{'ok':True}})
    assert runner.status()['status']=='DATA_READY'
    for i in range(1,16):
        at=NOW+timedelta(minutes=i);offline_cycle(runner,at)
    assert runner.status()['status']=='DATA_READY'
    with runner.store.transaction() as conn:set_control(conn,'native_restart_verified',{'at':stamp(at)})
    assert runner.status()['status']=='FORWARD_DEMO_READY'
    monkeypatch.setattr('backend.forward.runner.ForwardRunner.status',lambda *a,**k:{'status':'DATA_READY','reasons':[],'replay':{'ok':False}})
    assert runner.status()['status']=='DATA_READY'


def test_empty_credential_and_expired_approval_never_fetch():
    s=spec();calls=[];client=NativeHTTP(httpx.MockTransport(lambda r:calls.append(r)))
    with pytest.raises(ProviderFailure):client.read(s,'/feed',{},NOW,{})
    with pytest.raises(ProviderFailure):client.read(s,'/feed',{},NOW+timedelta(days=31),{s.secret_env:uuid.uuid4().hex})
    assert not calls


def test_duplicate_cycle_does_not_duplicate_receipts(tmp_path):
    s=spec('dxy');runner=RealRunner(tmp_path/'n.sqlite',[s],collection_enabled=True,providers={s.name:adapter(s)},clock=lambda:NOW)
    runner.once();assert runner.once()['status']=='DUPLICATE_CYCLE'
    with runner.store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM forward_receipts').fetchone()[0]==1


def test_secondary_conflict_archived_into_capture(tmp_path):
    from test_phase3f_forexfactory import primary,export,event
    from backend.realdata.forexfactory import cross_check
    store=RealStore(tmp_path/'n.sqlite');p=primary();secondary=export([event(forecast='9')],real=True)
    capture={'observed_at':stamp(NOW),'observations':[p],'selection':{'selected':{'calendar':p},'health':{},'vetoes':[]},'vetoes':[]}
    with store.transaction() as conn:
        store.event(conn,'secondary:unit','SECONDARY_CALENDAR',stamp(NOW),secondary)
        identity=store.put_capture(conn,capture);saved=store.get_capture(conn,identity)
    assert saved['vetoes']==['SECONDARY_CALENDAR_DISAGREEMENT']
    assert saved['secondary_calendar']['cross_check']['conflicts'][0]['secondary_event']['forecast']=='9'
    assert p['native']['details']['calendar_editions'][0]['Forecast']=='2.6'


def test_unchanged_editions_do_not_duplicate_archived_payloads(tmp_path):
    s=spec('news');store=RealStore(tmp_path/'n.sqlite',[s]);a=adapter(s).acquire(NOW)
    for i in range(3):
        row=normalize(s.legacy,a.envelope,NOW+timedelta(minutes=i),a.raw_hash)
        row['native']={'verification':a.verification,'details':a.details}
        with store.transaction() as conn:store.archive_observation(conn,str(i),row)
    with store.connect() as conn:
        assert conn.execute('SELECT count(*) FROM native_editions').fetchone()[0]==1
        payloads=[store.get(conn,r[0]) for r in conn.execute('SELECT id FROM forward_objects')]
    assert sum('article_id' in p and 'received_at' in p for p in payloads)==1


def test_momentum_history_excludes_future_receipts_and_fixture_values(tmp_path,monkeypatch):
    from backend.forward.store import ForwardStore
    from backend.forward.runner import KEY
    from backend.intelligence_providers import BUNDLE_KEY
    s=spec();runner=RealRunner(tmp_path/'n.sqlite',[s],clock=lambda:NOW)
    current=verified_row(s,NOW);current['value']['data_mode']='LIVE'
    with runner.store.transaction() as conn:
        for i,(minutes,number,mode) in enumerate(((-60,99,'LIVE'),(1,999,'LIVE'),(-30,777,'FIXTURE'))):
            row=deepcopy(current);at=NOW+timedelta(minutes=minutes)
            row.update(observed_at=stamp(at),published_at=stamp(at),received_at=stamp(at),revision_id=str(i))
            row['value'].update(data_mode=mode,as_of=stamp(at),retrieved_at=stamp(at))
            row['value']['records'][0].update(value=number,observed_at=stamp(at),published_at=stamp(at))
            row['native']['live_verified']=True
            # Pure storage-unit inputs, deliberately adversarial; never adapter output.
            ForwardStore.archive_observation(conn,str(i),row)
        capture={'observed_at':stamp(NOW),'observations':[current],
            'selection':{'selected':{'dxy':current},'health':{},'vetoes':[]},'vetoes':[]}
        identity=runner.store.put_capture(conn,capture)
        snapshot={'observed_at':stamp(NOW),'frames':{KEY:{'capture_id':identity},BUNDLE_KEY:{'usd':deepcopy(current['value'])}}}
        monkeypatch.setattr('backend.forward.runner.ForwardEngine.snapshot_context',lambda self,c,s,k:s)
        result=runner.engine.snapshot_context(conn,snapshot,{})
    values=[r['value'] for r in result['frames'][BUNDLE_KEY]['usd']['records']]
    assert sorted(values)==[99,100]
