"""Read-only mobile HTTP API with separate opt-in XAU scheduler ownership.

Deployment entrypoint: backend.mobile.api:app. No import of legacy API applications.
"""
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from copy import deepcopy
import json
import math
import re
import sqlite3

from backend.mobile.research_api import research_view
from backend.mobile.research_runtime import ResearchUS2YRuntime

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from ..domain import Policy, digest, freshness_errors, parse, stamp
from ..forward.contracts import CRITICAL, MAX_AGE, SYMBOLS
from ..realdata.reports import report
from ..realdata.adapters import synthetic_payload
from .runtime import Runtime
from .collection import MOBILE_XAU_FRESHNESS_SECONDS


FIELDS = frozenset('decision_id direction candidate_direction confidence confidence_kind calibrated_confidence raw_score '
                  'buy_score sell_score rsi rsi_state atr efficiency edge structure pressure volatility breakout_bias '
                  'entry sl tp1 tp2 risk_reward rr reasons veto_codes vetoes regime mode session timestamp_utc expires_at '
                  'source_candle_closes components council technical_scores intelligence hidden_state slow_regime '
                  'analytics_context timeframe_alignment signal_candle_close model_version strategy_version'.split())
SECRET = re.compile(r'token|secret|password|api.?key|authorization|credential', re.I)

# Exact allowlists only: exception messages and custom class names are untrusted.
STARTUP_CODES = frozenset('DEDICATED_PERSISTENT_DISK_REQUIRED LEGACY_SERVICE_FORBIDDEN '
    'LEGACY_DATABASE_FORBIDDEN UNKNOWN_MOBILE_VALIDATOR INVALID_STORAGE_BUDGET '
    'STORAGE_BUDGET_EXCEEDED DEDICATED_MOBILE_DATABASE_REQUIRED '
    'ORIGINAL_ENGINE_CONFIGURATION_REQUIRED PENDING_RECOVERY_REQUIRES_OFFLINE_REVIEW '
    'REPLAY_INTEGRITY_FAILURE INVALID_COLLECTION_FLAG INVALID_RESEARCH_US2Y_FLAG '
    'RESEARCH_DB_MUST_USE_PERSISTENT_DISK INCOMPATIBLE_RESEARCH_DATABASE'.split())
STARTUP_TYPES = {cls: cls.__name__ for cls in (
    RuntimeError, ValueError, TypeError, OSError, PermissionError, FileNotFoundError,
    sqlite3.OperationalError, sqlite3.IntegrityError, sqlite3.DatabaseError,
)}


def startup_diagnostic(exc):
    kind = STARTUP_TYPES.get(type(exc), 'Exception')
    code = 'UNCLASSIFIED'
    if type(exc) is RuntimeError and len(exc.args) == 1 and type(exc.args[0]) is str:
        message = exc.args[0]
        if message in STARTUP_CODES:
            code = message
        elif message == 'Only one monitor process may use this database':
            code = 'DATABASE_WORKER_LOCKED'
    return f'type={kind} code={code}'

def mobile_freshness_errors(closes, now):
    errors = freshness_errors(closes, now, Policy())
    try:
        age = (now - parse(closes['1min'])).total_seconds()
        if 0 <= age < MOBILE_XAU_FRESHNESS_SECONDS:
            errors = [e for e in errors if e != 'STALE_DATA:1min']
    except (KeyError, ValueError, TypeError, AttributeError):
        pass
    return errors


def public(value, depth=0):
    """Bounded JSON projection; no raw provider documents, headers or credential fields."""
    if depth > 20:raise ValueError('RESPONSE_DEPTH_LIMIT')
    if isinstance(value, dict):
        return {k:public(v, depth+1) for k,v in value.items() if isinstance(k,str)
                and (not SECRET.search(k) or (k=='credential_configured' and type(v) is bool))}
    if isinstance(value, list):return [public(v, depth+1) for v in value[:200]]
    if isinstance(value, float) and not math.isfinite(value):return None
    if isinstance(value, str):return value[:4000]
    if value is None or type(value) in (bool,int,float):return value
    raise ValueError('NON_JSON_RESPONSE')


def fixture(value):
    if isinstance(value, list):return any(fixture(v) for v in value)
    if isinstance(value, dict):
        return synthetic_payload(value) or any(
            str(value.get(k,'')) in ('TEST_DATA','FIXTURE','FIXTURE_DATA') for k in ('data_mode','data_status')) or any(fixture(v) for v in value.values())
    return False


def checked_decision(payload):
    value = json.loads(payload)
    checksum = value.pop('decision_checksum', None)
    if checksum != digest(value):raise ValueError('DECISION_INTEGRITY_FAILURE')
    return value


def decision_view(runtime, conn, row, now, historical=False):
    if row is None:
        return {'direction':'NO_TRADE', 'confidence':None, 'confidence_kind':'UNAVAILABLE',
                'data_mode':'UNAVAILABLE', 'data_status':'UNAVAILABLE', 'timestamp_utc':None,
                'source':'PHASE3B_CHAMPION', 'reasons':['No decision available; required providers are not ready.'],
                'veto_codes':(['COLLECTION_DISABLED'] if not runtime.collection_enabled else [])+['NO_DECISION_AVAILABLE'], 'freshness':{'status':'UNAVAILABLE'}}
    original = checked_decision(row['payload'])
    snapshot_row = conn.execute('SELECT payload FROM snapshots WHERE id=?',(row['snapshot_id'],)).fetchone()
    if not snapshot_row:raise ValueError('MISSING_SNAPSHOT')
    snapshot = json.loads(snapshot_row[0])
    if digest(snapshot) != row['snapshot_id']:raise ValueError('SNAPSHOT_INTEGRITY_FAILURE')
    # All timestamps must predate this response; known-at is distinct from candle close.
    if parse(snapshot['observed_at']) > now or parse(original['timestamp_utc']) > now:
        raise ValueError('FUTURE_DECISION')
    capture_id = snapshot.get('forward',{}).get('capture_id')
    capture = runtime.store.get_capture(conn,capture_id) if capture_id else None
    if capture and parse(capture['observed_at']) > now:raise ValueError('FUTURE_CAPTURE')
    errors = mobile_freshness_errors(original.get('source_candle_closes',{}), now)
    sources=[]
    for observation in (capture or {}).get('observations',[]):
        observed, received = parse(observation['observed_at']), parse(observation['received_at'])
        age=(now-observed).total_seconds()
        max_age = (
            MOBILE_XAU_FRESHNESS_SECONDS
            if observation['channel'] == 'xau'
            else MAX_AGE[observation['channel']]
        )
        if not observed <= received <= now or not 0 <= age < max_age:
            errors.append('STALE_PROVIDER:' + observation['channel'])
        sources.append({'name':observation['provider'], 'channel':observation['channel'],
                        'symbol':observation['symbol'], 'observed_at':stamp(observed), 'received_at':stamp(received),
                        'age_seconds':age, 'reported_mode_at_capture':observation['data_mode']})
    if not capture:errors.append('MISSING_PROVIDER_PROVENANCE')
    synthetic=fixture(snapshot) or fixture(capture)
    mode='TEST_DATA' if synthetic else 'STALE' if errors else 'HISTORICAL_POINT_IN_TIME'
    value={k:deepcopy(v) for k,v in original.items() if k in FIELDS}
    minute_bars = [bar for bar in snapshot.get('frames',{}).get('1min',[])
                   if parse(bar['t'])+timedelta(minutes=1) <= now]
    if minute_bars:
        observed=max(minute_bars,key=lambda bar:parse(bar['t']))
        value['monitor_price']=observed['c']
        value['price_observed_at']=stamp(parse(observed['t'])+timedelta(minutes=1))
    value.update(source='PHASE3B_CHAMPION', data_mode=mode, data_status=mode,
                 freshness={'status':'STALE' if errors else 'FRESH_AT_RESPONSE', 'errors':sorted(set(errors))},
                 provenance={'kind':'ARCHIVED_DECISION', 'sources':sources, 'capture_id':capture_id,
                             'live_validated_for_service':False}, served_at=stamp(now))
    if historical:
        value['historical']=True
    else:
        guards=(['COLLECTION_DISABLED'] if not runtime.collection_enabled else [])+['REQUIRED_PROVIDERS_NOT_READY']+errors
        if original.get('expires_at') and now >= parse(original['expires_at']):guards.append('SIGNAL_EXPIRED')
        value.update(direction='NO_TRADE', action='WAIT', entry=None, sl=None, tp1=None, tp2=None,
                     veto_codes=sorted(set(value.get('veto_codes',[])+guards)))
        value['reasons']=list(value.get('reasons',[]))+['Required providers are not ready; no active trade setup.']
    return public(value)


def system(runtime, now):
    storage=runtime.storage()
    xau=runtime.collector.status(now)
    state={'status':'NOT_READY', 'mode':'DEMO_ONLY', 'collection_enabled':runtime.collection_enabled,
            'reasons':(['COLLECTION_DISABLED'] if not runtime.collection_enabled else [])+['REQUIRED_PROVIDERS_NOT_READY']+([] if storage['within_budget'] else ['STORAGE_BUDGET_EXCEEDED']),
            'champion':'PHASE3B_CHAMPION', 'challenger':'ISOLATED_DEMO_ONLY_NO_AUTO_PROMOTION',
            'promotion':'PROMOTION_INELIGIBLE', 'automatic_promotion':False,
            'research_status':'INSUFFICIENT_FORWARD_DATA', 'predictive_edge_established':False,
            'provider_health':{channel:{'status':'UNAVAILABLE','providers':[]} for channel in CRITICAL},
            'providers':[{'name':s.name,'vendor':s.vendor,'channel':s.channel,'symbol':SYMBOLS[s.channel],
                          'approval':'UNAPPROVED','configuration':'UNCONFIGURED','data_mode':'UNAVAILABLE'} for s in runtime.specs],
            'forex_factory':{'enabled':False,'role':'SECONDARY_CROSS_CHECK_ONLY','usage_status':'UNVALIDATED','can_establish_live':False},
            'storage':storage, 'replay':runtime.replay, 'served_at':stamp(now)}
    state['xau']=xau
    state['providers'][0].update(xau,configuration='CONFIGURED' if xau['approval']=='APPROVED_CONFIGURATION' else 'UNCONFIGURED')
    state['provider_health']['xau']={'status':xau['status'],'providers':[xau]}
    return state


def create_app(
    db_path=None,
    clock=lambda: datetime.now(timezone.utc),
    runtime_factory=Runtime,
    research_runtime_factory=ResearchUS2YRuntime,
):
    @asynccontextmanager
    async def lifespan(app):
        phase = 'MAIN_RUNTIME'
        try:
            with runtime_factory(db_path) as runtime:
                app.state.runtime = runtime

                phase = 'RESEARCH_RUNTIME'
                research_runtime = research_runtime_factory(
                    main_path=runtime.path,
                    clock=clock,
                )
                app.state.research_runtime = research_runtime

                if runtime.collection_enabled:
                    phase = 'COLLECTOR_START'
                    runtime.collector.start()
                    phase = 'RESEARCH_START'
                    research_runtime.start()

                try:
                    phase = 'SERVING'
                    yield
                finally:
                    phase = 'SHUTDOWN'
                    research_runtime.close()

        except Exception as exc:
            # Never print exception bodies that could contain provider secrets or DB paths.
            raise RuntimeError(
                f'MOBILE_STARTUP_OR_STORAGE_FAILURE phase={phase} {startup_diagnostic(exc)}'
            ) from None
    app=FastAPI(title='DardaniaXAUTRADE AI parallel mobile API',version='mobile-v2-1',lifespan=lifespan,
                docs_url=None,redoc_url=None,openapi_url=None)

    @app.exception_handler(Exception)
    async def failure(request, exc):
        return JSONResponse({'status':'UNAVAILABLE','direction':'NO_TRADE','readiness':'NOT_READY',
                             'data_mode':'UNAVAILABLE','error':'STORAGE_OR_EVIDENCE_UNAVAILABLE'},status_code=503)

    @app.middleware('http')
    async def safe_headers(request:Request, call_next):
        try:
            response=await call_next(request)
        except Exception:
            # Handle here as well as in FastAPI: do not rethrow a raw DB/provider
            # exception through Starlette's outer server-error logger.
            response=JSONResponse({'status':'UNAVAILABLE','direction':'NO_TRADE','readiness':'NOT_READY',
                                   'data_mode':'UNAVAILABLE','error':'STORAGE_OR_EVIDENCE_UNAVAILABLE'},status_code=503)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        return response

    def runtime():
        result=app.state.runtime
        # Integrity is verified at startup and around collector cycles. HTTP reads
        # must stay read-only and responsive while collection owns the database.
        return result

    @app.get('/health')
    def health():
        # Render health checks are liveness probes. Keep this path independent of
        # collector/provider status and full database verification so a busy
        # acquisition cycle cannot block deployment health checks. Readiness and
        # provider details remain available from /system-status.
        r = app.state.runtime
        storage = r.storage()
        healthy = storage['within_budget']
        return JSONResponse({'status':'OK' if healthy else 'DEGRADED',
                             'mode':'DEMO_ONLY','readiness':'NOT_READY',
                             'collection_enabled':r.collection_enabled,
                             'storage_healthy':healthy,'within_storage_budget':healthy},
                            status_code=200 if healthy else 503)

    @app.get('/system-status')
    def status():return public(system(runtime(),clock()))

    @app.get('/signal')
    def signal():
        r=runtime();now=clock();state=system(r,now)
        with r.read() as conn:
            row=conn.execute('SELECT d.* FROM decisions d JOIN snapshots s ON s.id=d.snapshot_id WHERE s.observed_at<=? ORDER BY d.candle_close DESC LIMIT 1',(stamp(now),)).fetchone()
            champion=decision_view(r,conn,row,now)
            result=dict(champion, champion=deepcopy(champion), readiness=state,
                        provider_health=state['provider_health'], providers=state['providers'],
                        forex_factory=state['forex_factory'], execution='DEMO_ONLY', served_at=stamp(now))
            if row:
                meta=conn.execute('SELECT inputs,result,checksum FROM meta_decisions WHERE id=? AND at<=?',(row['id'],stamp(now))).fetchone()
                if meta:
                    _, verified=r.engine._verified_decision(meta)
                    result['challenger']=dict(public(verified.get('challenger',{})), source='ADAPTIVE_CHALLENGER',
                                              execution='ISOLATED_DEMO_ONLY', historical=True, automatic_promotion=False)
                    result['promotion']='PROMOTION_INELIGIBLE'
            return result
    @app.get('/research-signal')
    def research_signal():
        r = runtime()
        now = clock()
        return research_view(
            r,
            app.state.research_runtime,
            now,
        )

    @app.get('/performance')
    def performance():
        r=runtime();value=report(r.store,clock())
        value.pop('provider_health_history',None) # Full audit belongs in offline reports, not repeated mobile downloads.
        value['promotion']='PROMOTION_INELIGIBLE'
        for cohort in value['cohorts'].values():
            if cohort['qualification']['status']=='INSUFFICIENT_FORWARD_DATA':
                cohort['metrics']={}
                cohort['by_regime']={};cohort['by_session']={};cohort['by_variant']={}
        return public(value)

    @app.get('/history')
    def history(limit:int=Query(30,ge=1,le=200), before_row:int|None=Query(None,ge=1)):
        r=runtime();now=clock()
        with r.read() as conn:
            rows=conn.execute('SELECT d.rowid AS cursor,d.* FROM decisions d JOIN snapshots s ON s.id=d.snapshot_id WHERE d.rowid<? AND s.observed_at<=? ORDER BY d.rowid DESC LIMIT ?',
                              (before_row or 9223372036854775807,stamp(now),limit)).fetchall()
            return {'items':[dict(decision_view(r,conn,row,now,True),cursor=row['cursor']) for row in rows],
                    'next_cursor':rows[-1]['cursor'] if rows else None, 'source':'PHASE3B_CHAMPION'}

    @app.get('/trades')
    def trades(limit:int=Query(50,ge=1,le=200), before_row:int|None=Query(None,ge=1)):
        r=runtime();now=clock();items=[]
        with r.read() as conn:
            # Serve immutable, finalized Champion outcomes only. Never attach research outcomes to Champion history.
            rows=conn.execute("SELECT seq,at,payload_id FROM forward_ledger WHERE kind='NATIVE_OUTCOME' AND seq<? AND at<=? ORDER BY seq DESC LIMIT ?",
                              (before_row or 9223372036854775807,stamp(now),limit)).fetchall()
            for row in rows:
                outcome=r.store.get(conn,row['payload_id'])
                if outcome['source']!='CHAMPION':continue
                trade=outcome['trade']
                if parse(outcome['known_at'])>now or parse(trade['closed_at'])>now:continue
                origin=conn.execute('SELECT * FROM decisions WHERE id=?',(trade['decision_id'],)).fetchone()
                if not origin:raise ValueError('MISSING_TRADE_PROVENANCE')
                provenance=decision_view(r,conn,origin,now,True)
                value={k:trade[k] for k in ('id','decision_id','direction','entry','sl','tp1','tp2','result','status','opened_at','closed_at','r_multiple') if k in trade}
                value.update({k:outcome[k] for k in ('gross_r','simulated_net_r','cost_assumptions','duration_seconds','mfe_r','mae_r') if k in outcome})
                # This is archived demo history, never a live price source.
                value.update(source='CHAMPION',data_mode=provenance['data_mode'],cursor=row['seq'])
                items.append(public(value))
        return {'items':items,'next_cursor':rows[-1]['seq'] if rows else None,'source':'CHAMPION','execution':'DEMO_ONLY'}

    return app


app=create_app()
