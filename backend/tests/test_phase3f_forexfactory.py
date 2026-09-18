"""Offline official-export-shaped secondary calendar regressions."""
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import httpx,pytest
from backend.domain import parse,stamp
from backend.realdata.forexfactory import ForexFactoryConfig,ForexFactoryAdapter,cross_check,event_signature
from test_phase3f import NOW,spec,adapter
from backend.forward.contracts import normalize


def event(**kwargs):
    value={'title':'CPI','country':'USD','date':'2026-09-15T12:00:10-04:00','impact':'High','forecast':'2.6','previous':'2.5'}
    value.update(kwargs);return value


def export(rows=None,now=NOW,updated=NOW,real=False):
    return ForexFactoryAdapter().normalize(rows or [event()],now,updated,test_data=not real)


def primary():
    s=spec('calendar');a=adapter(s).acquire(NOW);r=normalize(s.legacy,a.envelope,NOW,a.raw_hash)
    r['native']={'verification':a.verification,'details':a.details};return r


def test_disabled_never_fetches():
    calls=[];a=ForexFactoryAdapter(transport=httpx.MockTransport(lambda r:calls.append(r)))
    assert a.read(NOW)['status']=='DISABLED' and not calls


def test_enabled_but_unvalidated_never_fetches():
    calls=[];a=ForexFactoryAdapter(ForexFactoryConfig(enabled=True),httpx.MockTransport(lambda r:calls.append(r)))
    with pytest.raises(ValueError,match='UNVALIDATED'):a.read(NOW)
    assert not calls


@pytest.mark.parametrize('date,utc',[('2026-09-15T12:00:10-04:00','2026-09-15T16:00:10+00:00'),('2026-01-15T08:30:00-05:00','2026-01-15T13:30:00+00:00'),('2026-09-15T18:00:10+02:00','2026-09-15T16:00:10+00:00')])
def test_offset_conversion(date,utc):assert export([event(date=date)])['events'][0]['scheduled_at']==utc


@pytest.mark.parametrize('date',['2026-09-15T12:00:10','Tentative','All Day'])
def test_ambiguous_times_rejected(date):
    with pytest.raises(ValueError):export([event(date=date)])


def test_primary_duplicate_matching_preserves_both():
    p=primary();before=deepcopy(p);s=export();result=cross_check(p,s,NOW)
    assert len(result['matches'])==1 and not result['conflicts'] and p==before
    assert result['matches'][0]['primary']!=result['matches'][0]['secondary']


@pytest.mark.parametrize('field,value',[('forecast','9'),('previous','9'),('impact','Low'),('date','2026-09-15T13:00:10-04:00')])
def test_conflicts_never_overwrite(field,value):
    p=primary();before=deepcopy(p);s=export([event(**{field:value})],real=True)
    result=cross_check(p,s,NOW)
    assert result['conflicts'] and result['vetoes']==['SECONDARY_CALENDAR_DISAGREEMENT'] and p==before


def test_fixture_conflicts_cannot_affect_real_decision():
    assert not cross_check(primary(),export([event(forecast='9')]),NOW)['vetoes']


def test_stale_export_conflict_recorded_without_fresh_claim():
    s=export([event(forecast='9')],updated=NOW-timedelta(hours=2),real=True)
    result=cross_check(primary(),s,NOW)
    assert s['status']=='STALE' and s['data_mode']!='LIVE_DATA' and result['conflicts'] and not result['vetoes']


def test_secondary_alone_never_authoritative():
    s=export(real=True);result=cross_check(None,s,NOW)
    assert s['data_mode']!='LIVE_DATA' and result['authoritative_provider'] is None


def test_future_revision_excluded():
    s=export([event(forecast='9')],now=NOW+timedelta(minutes=1))
    assert not cross_check(primary(),s,NOW)['conflicts']


def test_revisions_preserve_values_and_change_identity():
    a=export();b=export([event(forecast='9')],now=NOW+timedelta(minutes=1))
    assert a['events'][0]['id']==b['events'][0]['id']
    assert a['events'][0]['revision_id']!=b['events'][0]['revision_id']
    assert a['events'][0]['forecast']=='2.6'


def test_core_and_period_not_false_duplicates():
    assert event_signature('Core CPI m/m')!=event_signature('CPI m/m')
    assert event_signature('Core CPI m/m')!=event_signature('Core CPI y/y')
    assert event_signature('Non-Farm Employment Change')==event_signature('Non Farm Payrolls')


def test_exact_duplicate_exports_deduplicated():
    assert len(export([event(),event()])['events'])==1


def test_no_html_fallback_and_mock_never_live():
    cfg=ForexFactoryConfig(enabled=True,usage_validated=True,approval_ref='OFFLINE_TEST')
    def respond(r):
        assert r.url.path.endswith('.json')
        return httpx.Response(200,json=[event()],headers={'Last-Modified':'Tue, 15 Sep 2026 14:00:00 GMT'})
    result=ForexFactoryAdapter(cfg,httpx.MockTransport(respond)).read(NOW)
    assert result['data_mode']=='TEST_DATA'


def test_http_date_does_not_replace_missing_export_timestamp():
    cfg=ForexFactoryConfig(enabled=True,usage_validated=True,approval_ref='OFFLINE_TEST')
    a=ForexFactoryAdapter(cfg,httpx.MockTransport(lambda r:httpx.Response(200,json=[event()],headers={'Date':'Tue, 15 Sep 2026 14:00:00 GMT'})))
    with pytest.raises(ValueError):a.read(NOW)


def test_pre_release_actual_and_future_export_rejected():
    with pytest.raises(ValueError):export([event(actual='3')])
    with pytest.raises(ValueError):export(updated=NOW+timedelta(seconds=1))
