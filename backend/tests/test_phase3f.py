"""Native-shaped offline fixtures. No approved providers or real credentials."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import json, threading, uuid
import httpx, pytest
from backend.domain import digest, stamp, parse
from backend.realdata.config import NativeSpec, MAPPINGS
from backend.realdata.transport import NativeHTTP, credential
from backend.realdata.adapters import NativeAdapter, source_time, finite, event_kind
from backend.realdata.verification import verify
from backend.realdata.runner import RealRunner
from backend.realdata.store import RealStore
from backend.realdata.reports import report, qualify, capacity
from backend.realdata.outcomes import excursions
from backend.forward.contracts import normalize
from backend.forward.providers import ProviderFailure
from backend.forward.store import ForwardStore
from test_phase1 import NOW, frames


def spec(channel='dxy', approved=True, **kwargs):
    vendor='twelve_data' if channel=='xau' else 'finnhub' if channel=='news' else 'trading_economics'
    values=dict(name='native-'+channel,vendor=vendor,channel=channel,secret_env='OFFLINE_TEST_TOKEN',
        approved=approved,approval_ref='TEST_ONLY',entitlement_ref='TEST_ONLY',
        entitlement_until=stamp(NOW+timedelta(days=30)),live_entitled=True,source_timezone='UTC',
        unit='USD_per_troy_ounce' if channel=='xau' else 'index_points' if channel=='dxy' else 'percent' if channel.startswith('us') else '',
        calendar_complete=True)
    values.update(kwargs);return NativeSpec(**values)


def body(channel,now=NOW,tf='1min'):
    if channel=='xau':
        return {'meta':{'symbol':'XAU/USD','interval':tf,'timezone':'UTC'},'values':[
            {'datetime':r['t'],'open':str(r['o']),'high':str(r['h']),'low':str(r['l']),'close':str(r['c'])} for r in frames(now)[tf]]}
    if channel=='news':return [{'id':7,'datetime':int((now-timedelta(seconds=5)).timestamp()),'headline':'Gold and dollar await Fed decision',
        'source':'Offline source','url':'https://example.invalid/article'}]
    if channel=='calendar':return [{'CalendarId':7,'Country':'United States','Date':stamp(now+timedelta(hours=2)),
        'LastUpdate':stamp(now-timedelta(seconds=5)),'Event':'CPI','Importance':3,'DateSpan':0,'URL':'/united-states/inflation-cpi',
        'Actual':'','Previous':'2.5','Forecast':'2.6'}]
    return [{'Symbol':MAPPINGS[channel],'Country':'United States','Date':stamp(now-timedelta(seconds=5)),
        'LastUpdate':stamp(now-timedelta(seconds=4)),'Last':100 if channel=='dxy' else 4.2,
        'unit':'points' if channel=='dxy' else '%','frequency':'Live'}]


def adapter(s,mutate=None):
    def respond(request):
        raw=body(s.channel,tf=request.url.params.get('interval','1min'))
        if mutate:raw=mutate(raw)
        return httpx.Response(200,json=raw)
    return NativeAdapter(s,NativeHTTP(httpx.MockTransport(respond)),{s.secret_env:uuid.uuid4().hex})


@pytest.mark.parametrize('channel',['xau','dxy','us2y','us10y','calendar','news'])
def test_native_shapes_are_test_data(channel):
    s=spec(channel);a=adapter(s).acquire(NOW)
    row=normalize(s.legacy,a.envelope,NOW,a.raw_hash)
    assert row['data_mode']=='TEST_DATA' and not a.verification['real_transport']


@pytest.mark.parametrize('channel',['xau','dxy','us2y','us10y','calendar','news'])
def test_unapproved_does_not_fetch(channel):
    s=spec(channel,False);calls=[]
    client=NativeHTTP(httpx.MockTransport(lambda r:calls.append(r)))
    with pytest.raises(ProviderFailure,match='PROVIDER_NOT_APPROVED'):client.read(s,'/feed',{},NOW,{s.secret_env:uuid.uuid4().hex})
    assert not calls


@pytest.mark.parametrize('status,code',[(401,'AUTH_OR_ENTITLEMENT_DENIED'),(403,'AUTH_OR_ENTITLEMENT_DENIED'),(429,'RATE_LIMITED'),(500,'HTTP_PROVIDER_FAILURE'),(302,'HTTP_PROVIDER_FAILURE')])
def test_http_failures_sanitized(status,code):
    s=spec();token=uuid.uuid4().hex
    client=NativeHTTP(httpx.MockTransport(lambda r:httpx.Response(status,text=token,headers={'Retry-After':'9999'})))
    with pytest.raises(ProviderFailure) as exc:client.read(s,'/feed',{},NOW,{s.secret_env:token})
    assert exc.value.code==code and token not in str(exc.value)
    if status==429:assert exc.value.retry_after==900


@pytest.mark.parametrize('channel,header',[('xau','authorization'),('dxy','authorization'),('news','x-finnhub-token')])
def test_header_auth_only(channel,header):
    s=spec(channel);token=uuid.uuid4().hex;calls=[]
    def respond(request):
        calls.append(request);return httpx.Response(200,json=[])
    NativeHTTP(httpx.MockTransport(respond)).read(s,'/feed',{},NOW,{s.secret_env:token})
    assert token in calls[0].headers[header] and token not in str(calls[0].url)


def verified_row(s,now):
    # Pure gate unit input, never a real collected fixture or operational feed.
    a=adapter(s).acquire(NOW);raw=deepcopy(a.envelope)
    delta=now-NOW
    raw['observed_at']=stamp(parse(raw['observed_at'])+delta);raw['published_at']=raw['observed_at']
    raw['data_mode']='LIVE_DATA'
    row=normalize(s.legacy,a.envelope,NOW,a.raw_hash)
    row.update(received_at=stamp(now),observed_at=raw['observed_at'],health='HEALTHY',errors=[],data_mode='LIVE_DATA')
    row['native']={'verification':dict(a.verification,real_transport=True),'details':a.details}
    return row


@pytest.mark.parametrize('mode',['TEST_DATA','FIXTURE','HISTORICAL_POINT_IN_TIME','UNAVAILABLE','DELAYED_DATA'])
def test_no_nonlive_upgrade(mode):
    s=spec();previous=[]
    for i in range(3):
        now=NOW+timedelta(minutes=i);r=verified_row(s,now);r['data_mode']=mode
        result=verify(r,previous,s,now);previous.append(result)
        assert result['data_mode']!='LIVE_DATA' and not result['native']['live_verified']


def test_three_causal_samples_required():
    s=spec();previous=[]
    for i in range(3):
        now=NOW+timedelta(minutes=i);r=verify(verified_row(s,now),previous,s,now);previous.append(r)
        assert r['native']['live_verified']==(i==2)


@pytest.mark.parametrize('key',['authenticated','real_transport','entitled','complete'])
@pytest.mark.parametrize('value',[False,'true',1,None])
def test_strict_verification_flags(key,value):
    s=spec();r=verified_row(s,NOW);r['native']['verification'][key]=value
    assert not verify(r,[],s,NOW)['native']['candidate_verified']


@pytest.mark.parametrize('channel',['calendar','news'])
def test_cadence_cannot_bridge_outage(channel):
    s=spec(channel);previous=[]
    for minute in (0,1,20):
        now=NOW+timedelta(minutes=minute);r=verify(verified_row(s,now),previous,s,now);previous.append(r)
    assert not r['native']['live_verified']


def test_relevant_news_clock_not_unrelated_headline():
    def mutate(rows):
        rows[0]['datetime']=int((NOW-timedelta(hours=1)).timestamp())
        rows.append(dict(rows[0],id=8,headline='Local sports results',datetime=int(NOW.timestamp())))
        return rows
    result=adapter(spec('news'),mutate).acquire(NOW)
    assert parse(result.envelope['observed_at'])==NOW-timedelta(hours=1)


@pytest.mark.parametrize('channel',['dxy','us2y','us10y'])
@pytest.mark.parametrize('defect',['symbol','unit','future','nan'])
def test_market_rejects_invalid_native_values(channel,defect):
    def mutate(rows):
        row=rows[0]
        if defect=='symbol':row['Symbol']='WRONG'
        if defect=='unit':row['unit']='basis points'
        if defect=='future':row['LastUpdate']=stamp(NOW+timedelta(seconds=1))
        if defect=='nan':row['Last']='NaN'
        return rows
    with pytest.raises(ValueError):adapter(spec(channel),mutate).acquire(NOW)


@pytest.mark.parametrize('defect',['actual','revision','ambiguous'])
def test_calendar_rejects_future_release(defect):
    def mutate(rows):
        if defect=='actual':rows[0]['Actual']='3.0'
        if defect=='revision':rows[0]['LastUpdate']=stamp(NOW+timedelta(seconds=1))
        if defect=='ambiguous':rows[0]['DateSpan']=1
        return rows
    with pytest.raises(ValueError):adapter(spec('calendar'),mutate).acquire(NOW)


def test_news_future_rejected():
    def mutate(rows):rows[0]['datetime']=int((NOW+timedelta(seconds=1)).timestamp());return rows
    with pytest.raises(ValueError):adapter(spec('news'),mutate).acquire(NOW)


@pytest.mark.parametrize('defect',['symbol','timezone','quote','unit'])
def test_xau_mapping_and_quote_checks(defect):
    def mutate(raw):
        if defect=='symbol':raw['meta']['symbol']='XAG/USD'
        if defect=='timezone':raw['meta']['timezone']='America/New_York'
        if defect=='quote':raw.update(bid=4300,ask=4301,quote_timestamp=stamp(NOW-timedelta(hours=1)))
        return raw
    s=spec('xau',unit='' if defect=='unit' else 'USD_per_troy_ounce')
    with pytest.raises(ValueError):adapter(s,mutate).acquire(NOW)


def test_disabled_runner_no_acquisition(tmp_path):
    s=spec(approved=False)
    runner=RealRunner(tmp_path/'native.sqlite',[s],providers={s.name:object()},clock=lambda:NOW)
    assert runner.once()['status']=='COLLECTION_DISABLED'
    status=runner.status();assert status['status']=='NOT_READY' and status['providers'][0]['approval']=='UNAPPROVED'
    evidence=report(runner.store,NOW)
    assert evidence['real_forward_evaluation_cycles']==0 and not evidence['automatic_promotion']


def test_phase3e_database_cannot_be_reused(tmp_path):
    path=tmp_path/'old.sqlite';ForwardStore(path)
    with pytest.raises(ValueError,match='DEDICATED_PHASE3F'):RealStore(path)


@pytest.mark.parametrize('channel',['calendar','news'])
def test_revisions_are_as_received_and_immutable(tmp_path,channel):
    s=spec(channel);store=RealStore(tmp_path/'native.sqlite',[s]);identity=s.legacy.identity
    for i in range(2):
        a=adapter(s).acquire(NOW);r=normalize(s.legacy,a.envelope,NOW+timedelta(minutes=i),a.raw_hash)
        details=deepcopy(a.details);key='calendar_editions' if channel=='calendar' else 'news_editions'
        details[key][0]['revision_id']=str(i);details[key][0]['test_revision']=i
        r['native']={'verification':a.verification,'details':details}
        with store.transaction() as conn:store.archive_observation(conn,str(i),r)
    past=store.editions_as_of(identity,channel,'7',NOW)
    assert len(past)==1 and past[0]['test_revision']==0
    assert len(store.editions_as_of(identity,channel,'7',NOW+timedelta(minutes=2)))==2
    with pytest.raises(Exception):
        with store.transaction() as conn:conn.execute('DELETE FROM native_editions')


def test_idle_time_does_not_qualify_evidence():
    row={'id':'a','opened_at':stamp(NOW),'closed_at':stamp(NOW+timedelta(minutes=1)), 'net_r':1,'regime':{'hidden_state':'RANGE','session':'NY'}}
    result=qualify([row],NOW+timedelta(days=365))
    assert 'MINIMUM_CALENDAR_TIME' in result['reasons'] and not result['profitability_established']


def test_exit_minute_excursion_only_upper_bound():
    trade={'opened_at':stamp(NOW),'closed_at':stamp(NOW+timedelta(minutes=1)),'entry':100,'sl':99,'direction':'BUY','exit_price':101}
    result=excursions(trade,[{'t':stamp(NOW),'h':110,'l':90}])
    assert result['mfe_r']==1 and result['mae_r']==0 and result['mfe_r_upper']==10


@pytest.mark.parametrize('name,kind',[('CPI','CPI'),('Non Farm Payrolls','NFP'),('Core PCE','PCE'),('Fed Chair Powell Speech','FOMC')])
def test_event_mapping(name,kind):assert event_kind(name)==kind
