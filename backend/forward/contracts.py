"""Strict provider modes, causal normalization and explicit cross-source selection."""
from dataclasses import dataclass,asdict
from datetime import timedelta
import math,re
from urllib.parse import urlsplit

from ..domain import INTERVALS,Policy,canonical,digest,parse,stamp,inspect_frames
from ..intelligence_providers import normalize_envelope
from ..slow_context import normalize_slow

MODES=('LIVE_DATA','DELAYED_DATA','HISTORICAL_POINT_IN_TIME','TEST_DATA','FIXTURE','UNAVAILABLE')
CHANNELS=('xau','dxy','us2y','us10y','calendar','news','fed','cot','etf','physical','options')
CRITICAL=('xau','dxy','us2y','us10y','calendar','news')
SYMBOLS={'xau':'XAU/USD','dxy':'DXY','us2y':'US2Y','us10y':'US10Y','calendar':'US_CALENDAR','news':'GOLD_USD_NEWS',
         'fed':'FED_EXPECTATIONS','cot':'GOLD_COT','etf':'GOLD_ETF','physical':'GOLD_PHYSICAL','options':'GOLD_OPTIONS'}
MAX_AGE={'xau':150,'dxy':300,'us2y':900,'us10y':900,'calendar':3600,'news':900,'fed':86400,
         'cot':864000,'etf':259200,'physical':604800,'options':86400}
LEGACY={'LIVE_DATA':'LIVE','DELAYED_DATA':'DELAYED','TEST_DATA':'FIXTURE','FIXTURE':'FIXTURE',
        'HISTORICAL_POINT_IN_TIME':'DELAYED','UNAVAILABLE':'UNAVAILABLE'}


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    channel: str
    endpoint: str
    secret_env: str = ''
    adapter: str = 'approved_json'
    mode: str = 'UNAVAILABLE'
    approval: str = ''
    priority: int = 0
    timeout_seconds: float = 8
    entitlement_delay_seconds: int = 0

    def __post_init__(self):
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',self.name) or self.channel not in CHANNELS or self.mode not in MODES:
            raise ValueError('INVALID_PROVIDER_SPEC')
        if self.adapter not in ('approved_json','twelve_data') or (self.adapter=='twelve_data' and self.channel!='xau'):
            raise ValueError('INVALID_ADAPTER')
        url=urlsplit(self.endpoint)
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError('UNSAFE_PROVIDER_ENDPOINT')
        if self.secret_env and not re.fullmatch(r'[A-Z][A-Z0-9_]{1,100}',self.secret_env):raise ValueError('INVALID_SECRET_REFERENCE')
        if type(self.priority) is not int or type(self.entitlement_delay_seconds) is not int or self.entitlement_delay_seconds<0:
            raise ValueError('INVALID_PROVIDER_CONFIGURATION')
        if type(self.timeout_seconds) not in (int,float) or not math.isfinite(self.timeout_seconds) or not .05<=self.timeout_seconds<=20:
            raise ValueError('INVALID_TIMEOUT')
        if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}',self.approval):raise ValueError('PROVIDER_APPROVAL_REQUIRED')
        if self.adapter=='twelve_data' and self.endpoint!='https://api.twelvedata.com/time_series':raise ValueError('INVALID_TWELVE_ENDPOINT')

    @property
    def identity(self):return digest(asdict(self))


def restrictive_mode(value,spec):
    aliases={'LIVE':'LIVE_DATA','DELAYED':'DELAYED_DATA'}
    found=[spec.mode]
    def walk(v):
        if isinstance(v,dict):
            if 'delay_seconds' in v:
                delay=v['delay_seconds']
                if type(delay) not in (int,float) or not math.isfinite(delay) or delay<0:raise ValueError('INVALID_DELAY_METADATA')
                if delay:found.append('DELAYED_DATA')
            if 'data_mode' in v:
                mode=aliases.get(v['data_mode'],v['data_mode'])
                if mode not in MODES:raise ValueError('UNKNOWN_DATA_MODE')
                found.append(mode)
            if 'is_synthetic' in v:
                if type(v['is_synthetic']) is not bool:raise ValueError('INVALID_SYNTHETIC_MARKER')
                if v['is_synthetic']:found.append('TEST_DATA')
            for child in v.values():walk(child)
        elif isinstance(v,list):
            for child in v:walk(child)
    walk(value)
    if spec.entitlement_delay_seconds:found.append('DELAYED_DATA')
    delay=value.get('delay_seconds',0)
    if type(delay) not in (int,float) or not math.isfinite(delay) or delay<0:raise ValueError('INVALID_DELAY_METADATA')
    if delay:found.append('DELAYED_DATA')
    for mode in ('TEST_DATA','FIXTURE','UNAVAILABLE','HISTORICAL_POINT_IN_TIME','DELAYED_DATA','LIVE_DATA'):
        if mode in found:return mode
    raise ValueError('MISSING_DATA_MODE')


def normalize(spec,raw,received_at,raw_hash):
    """Whitelist legacy inputs, never archive an unfiltered provider response."""
    if received_at.tzinfo is None or received_at.utcoffset() is None:raise ValueError('NAIVE_RECEIPT_CLOCK')
    if not isinstance(raw_hash,str) or not re.fullmatch('[0-9a-f]{64}',raw_hash):raise ValueError('INVALID_RAW_HASH')
    if raw.get('symbol')!=SYMBOLS[spec.channel]:raise ValueError('SYMBOL_MISMATCH')
    if raw.get('data_mode') not in MODES:raise ValueError('EXPLICIT_MODE_REQUIRED')
    observed=parse(raw['observed_at']);published=parse(raw.get('published_at',raw['observed_at']))
    if not observed<=published<=received_at:raise ValueError('FUTURE_OR_INVERTED_SOURCE_CLOCK')
    revision=raw['revision_id']
    if not isinstance(revision,str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}',revision):raise ValueError('INVALID_REVISION_ID')
    mode=restrictive_mode(raw,spec);channel=spec.channel;value=raw['value'];errors=[]
    def legacy(v):
        if isinstance(v,dict):return {k:LEGACY.get(x,x) if k=='data_mode' else legacy(x) for k,x in v.items()}
        if isinstance(v,list):return [legacy(x) for x in v]
        return v
    value=legacy(value)
    if mode=='UNAVAILABLE':raise ValueError('PROVIDER_UNAVAILABLE')
    if channel=='xau':
        frames={tf:value.get(tf,[]) for tf in INTERVALS}
        inspected,errors=inspect_frames({'observed_at':stamp(received_at),'frames':frames},Policy())
        # Rebuild whitelisted candle rows; forming OHLC is never archived as final.
        normalized={tf:[dict(asdict(c),t=stamp(parse(c.t))) for c in bars] for tf,bars in inspected.items()}
        if inspected['1min']:
            latest=parse(inspected['1min'][-1].t)+timedelta(minutes=1)
            if observed!=latest:errors.append('XAU_SOURCE_TIMESTAMP_MISMATCH')
    elif channel in ('cot','etf','physical','options'):
        envelope=dict(value,status='OK',provider=spec.name,retrieved_at=stamp(received_at),
                      data_mode='TEST_DATA' if mode in ('TEST_DATA','FIXTURE') else LEGACY[mode])
        normalized=normalize_slow({channel:envelope},received_at)[channel]
        if normalized['status']!='OK':errors.append('INVALID_SLOW_ENVELOPE')
        if normalized['records'] and max(parse(r['observed_at']) for r in normalized['records'])!=observed:
            errors.append('SOURCE_TIMESTAMP_MISMATCH')
    else:
        mapped={'dxy':'usd','us2y':'yields','us10y':'yields','fed':'macro'}.get(channel,channel)
        envelope=dict(value,status='OK',data_mode=LEGACY[mode])
        normalized=normalize_envelope(mapped,envelope,received_at,spec.name)
        if parse(normalized['as_of'])!=observed and channel not in ('dxy','us2y','us10y'):
            errors.append('SOURCE_TIMESTAMP_MISMATCH')
        if normalized['status']!='OK':errors.append('INVALID_NORMALIZED_ENVELOPE')
        if channel in ('dxy','us2y','us10y'):
            if not normalized['records'] or any(r['instrument']!=SYMBOLS[channel] for r in normalized['records']):raise ValueError('INSTRUMENT_MISMATCH')
            latest=max(parse(r['observed_at']) for r in normalized['records'])
            if latest!=observed:errors.append('SOURCE_TIMESTAMP_MISMATCH')
    # Check all observation/publication fields, allowing only future scheduled events.
    def check(v):
        if isinstance(v,dict):
            if v.get('observed_at') and v.get('published_at') and parse(v['observed_at'])>parse(v['published_at']):
                raise ValueError('INVERTED_RECORD_CLOCK')
            for k,x in v.items():
                if k in ('observed_at','published_at','retrieved_at','as_of','first_published_at') and x is not None and parse(x)>received_at:
                    raise ValueError('FUTURE_NORMALIZED_RECORD')
                check(x)
        elif isinstance(v,list):
            for x in v:check(x)
    check(normalized)
    if channel in ('dxy','us2y','us10y','cot','etf','physical','options'):
        latest={}
        for record in normalized.get('records',[]):
            key=(record['source'],record.get('instrument'))
            latest[key]=max(latest.get(key,parse(record['observed_at'])),parse(record['observed_at']))
        if any((received_at-at).total_seconds()>=MAX_AGE[channel] for at in latest.values()):errors.append('STALE_CONTRIBUTING_SOURCE')
    age=(received_at-observed).total_seconds()
    health='STALE' if age>=MAX_AGE[channel] else 'DEGRADED' if errors or mode!='LIVE_DATA' else 'HEALTHY'
    return {'provider':spec.name,'provider_identity':spec.identity,'approval':spec.approval,'channel':channel,
            'symbol':SYMBOLS[channel],'observed_at':stamp(observed),'published_at':stamp(published),
            'received_at':stamp(received_at),'revision_id':revision,'raw_hash':raw_hash,'data_mode':mode,
            'freshness_seconds':age,'health':health,'errors':sorted(set(errors)),'value':normalized,
            'entitlement_delay_seconds':spec.entitlement_delay_seconds,'provenance':'APPROVED_PROVIDER_ATTESTATION_NOT_PROOF_OF_TRUTH'}


def quality(observation,now):
    row=dict(observation)
    age=(now-parse(row['observed_at'])).total_seconds();receipt_age=(now-parse(row['received_at'])).total_seconds()
    if age<0 or receipt_age<0:row.update(health='DEGRADED',errors=sorted(set(row['errors']+['CLOCK_INTEGRITY'])))
    elif age>=MAX_AGE[row['channel']]:row['health']='STALE'
    row['freshness_seconds']=age
    return row


def discrepancy(a,b):
    channel=a['channel'];errors=[]
    if abs((parse(a['observed_at'])-parse(b['observed_at'])).total_seconds())>90:errors.append('SOURCE_TIMESTAMP_DRIFT')
    def latest(row):
        if channel=='xau':return row['value']['1min'][-1]['c']
        records=row['value']['records'];return max(records,key=lambda r:parse(r['observed_at']))['value']
    if channel in ('xau','dxy','us2y','us10y'):
        left,right=latest(a),latest(b)
        tolerance=max(2,abs(left)*.001) if channel=='xau' else .15 if channel=='dxy' else .05
        if abs(left-right)>tolerance:errors.append('CROSS_SOURCE_VALUE_CONFLICT')
    if channel=='calendar':
        left={r['event_key']:r['scheduled_at'] for r in a['value']['records']}
        right={r['event_key']:r['scheduled_at'] for r in b['value']['records']}
        if any(left[k]!=right[k] for k in left.keys()&right.keys()):errors.append('CROSS_SOURCE_EVENT_TIME_CONFLICT')
    return errors


def select(specs,observations,now,previous=None):
    previous=previous or {};selected={};health={};vetoes=[];switches=[]
    specs_by_name={s.name:s for s in specs}
    for channel in CHANNELS:
        rows=[quality(r,now) for r in observations if r['channel']==channel]
        eligible=sorted((r for r in rows if r['health']=='HEALTHY' and r['data_mode']=='LIVE_DATA'),
                        key=lambda r:(specs_by_name[r['provider']].priority,r['provider']))
        comparable=eligible
        if channel=='xau':
            comparable=[r for r in rows if r['data_mode']=='LIVE_DATA' and 0<=r['freshness_seconds']<MAX_AGE['xau']
                        and r['value'].get('1min') and not any(e in r['errors'] for e in
                        ('INVALID_DATA:1min','STALE_DATA:1min','XAU_SOURCE_TIMESTAMP_MISMATCH',
                         'OUT_OF_ORDER_OBSERVATION','REVISION_ID_REUSED','CLOCK_INTEGRITY'))]
        conflicts=[]
        for i,a in enumerate(comparable):
            for b in comparable[i+1:]:conflicts.extend(discrepancy(a,b))
        if any(r['health']=='CONFLICTING' for r in rows):conflicts.append('OBSERVATION_INTEGRITY_CONFLICT')
        if conflicts:
            health[channel]={'status':'CONFLICTING','providers':rows,'reasons':sorted(set(conflicts))}
            vetoes.append('PROVIDER_CONFLICT:'+channel)
        elif eligible:
            chosen=eligible[0];selected[channel]=chosen
            old=previous.get(channel)
            primary=min((s for s in specs if s.channel==channel),key=lambda s:(s.priority,s.name)).name
            if not old and chosen['provider']!=primary:old=primary
            if old and old!=chosen['provider']:switches.append({'channel':channel,'from':old,'to':chosen['provider'],'reason':'APPROVED_FAILOVER_OR_PRIORITY_RESTORED'})
            health[channel]={'status':'HEALTHY','selected':chosen['provider'],'providers':rows,'reasons':[]}
        else:
            health[channel]={'status':'STALE' if any(r['health']=='STALE' for r in rows) else 'DEGRADED' if rows else 'UNAVAILABLE',
                             'providers':rows,'reasons':['NO_ELIGIBLE_LIVE_PROVIDER']}
            if channel in CRITICAL or any(s.channel==channel for s in specs):vetoes.append('PROVIDER_NOT_READY:'+channel)
        if any(r['data_mode'] in ('TEST_DATA','FIXTURE','HISTORICAL_POINT_IN_TIME') for r in rows):
            vetoes.append('MIXED_OR_NONFORWARD_PROVENANCE:'+channel)
    return {'selected':selected,'health':health,'vetoes':sorted(set(vetoes)),'switches':switches,
            'at':stamp(now),'mode':'FORWARD_DEMO_ONLY'}
