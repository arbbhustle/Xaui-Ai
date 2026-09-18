"""Native vendor schemas. Unapproved, delayed or test responses never assert LIVE."""
from dataclasses import dataclass
from datetime import datetime,timezone,timedelta
import math,re,threading,time
from ..domain import INTERVALS,digest,parse,stamp
from ..forward.contracts import SYMBOLS,restrictive_mode
from ..forward.providers import ProviderFailure
from ..intelligence_providers import source_url,text
from .config import MAPPINGS
from .transport import NativeHTTP


@dataclass
class Acquisition:
    envelope:dict
    raw_hash:str
    verification:dict
    details:dict


def source_time(value,spec):
    if not isinstance(value,str):raise ValueError('MISSING_SOURCE_TIME')
    try:return parse(value)
    except ValueError:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        if parsed.tzinfo is not None or spec.source_timezone!='UTC':raise ValueError('UNVERIFIED_SOURCE_TIMEZONE')
        return parsed.replace(tzinfo=timezone.utc)


def finite(value,low=-5,high=1000000):
    if isinstance(value,bool):raise ValueError('INVALID_NUMBER')
    number=float(value)
    if not math.isfinite(number) or not low<=number<=high:raise ValueError('INVALID_NUMBER')
    return number


def relevance(title):
    words=title.lower()
    return [tag for tag,terms in {'gold':('gold','bullion'), 'usd':('dollar','usd','treasury','yield'),
        'fed':('federal reserve','fed ','powell','inflation','cpi','payroll','pce'),
        'geopolitical':('war','missile','sanction','ceasefire','iran','ukraine')}.items() if any(t in words for t in terms)]


def event_kind(name):
    name=name.lower()
    if 'cpi' in name or 'inflation rate' in name or 'consumer price' in name:return 'CPI'
    if ('non farm' in name or 'nonfarm' in name or 'non-farm' in name or 'payroll' in name) and 'adp' not in name:return 'NFP'
    if 'pce' in name or 'personal consumption expenditure' in name:return 'PCE'
    if any(t in name for t in ('fomc','fed interest','fed rate','powell','fed chair','fed speech')):return 'FOMC'
    return 'OTHER'


def synthetic_payload(value):
    if isinstance(value,list):return any(synthetic_payload(v) for v in value)
    if not isinstance(value,dict):return False
    for flag in ('is_demo','is_test','is_synthetic'):
        if flag in value:
            if type(value[flag]) is not bool:raise ValueError('INVALID_SYNTHETIC_MARKER')
            if value[flag]:return True
    if str(value.get('environment','')).lower() in ('test','demo','sandbox','fixture'):return True
    if value.get('data_mode') in ('TEST_DATA','FIXTURE'):return True
    return any(synthetic_payload(v) for v in value.values())


class NativeAdapter:
    def __init__(self,spec,client=None,environ=None):
        self.spec=spec;self.client=client or NativeHTTP();self.environ=environ
        self._workers={}

    def request(self,path,params,now):return self.client.read(self.spec,path,params,now,self.environ)

    def acquire(self,now):
        if self.spec.vendor=='twelve_data':return self._xau(now)
        if self.spec.vendor=='finnhub':return self._news(now)
        return self._calendar(now) if self.spec.channel=='calendar' else self._market(now)

    def finish(self,value,observed,now,responses,details,*,frequency='Live',complete=True):
        s=self.spec
        # Provider markers are inspected before field filtering, including nested rows.
        declared=restrictive_mode({'responses':[r.payload for r in responses]},s.legacy)
        real=all(r.real_transport for r in responses)
        if not real or any(synthetic_payload(r.payload) for r in responses):declared='TEST_DATA'
        if frequency.lower() not in ('live','real-time','realtime') and declared=='LIVE_DATA':declared='DELAYED_DATA'
        if not s.live_entitled and declared=='LIVE_DATA':declared='DELAYED_DATA'
        envelope={'symbol':SYMBOLS[s.channel],'observed_at':stamp(observed),'published_at':stamp(observed),
                  'revision_id':digest(value),'data_mode':declared,'value':value}
        verification={'authenticated':True,'real_transport':real,'provider':s.vendor,'provider_identity':s.identity,
                      'instrument':MAPPINGS[s.channel],'entitled':bool(s.live_entitled and s.approved_at(now)),
                      'entitlement_ref':s.entitlement_ref,'entitlement_until':s.entitlement_until,
                      'approval_ref':s.approval_ref,'frequency':frequency,'complete':bool(complete),
                      'source_timezone':s.source_timezone,'unit':s.unit}
        return Acquisition(envelope,digest([r.raw_hash for r in responses]),verification,details)

    def _xau(self,now):
        if self.spec.unit!='USD_per_troy_ounce':raise ValueError('UNIT_MAPPING_UNAPPROVED')
        responses=[];frames={};quote=None;failures=[]
        def fetch(tf,box,done):
            try:box.append(self.request('/time_series',{'symbol':'XAU/USD','interval':tf,'timezone':'UTC','outputsize':1000 if tf=='1min' else 125},now))
            except Exception as exc:box.append(exc)
            finally:done.set()
        pending={};deadline=time.monotonic()+self.spec.timeout_seconds*.85
        for tf in INTERVALS:
            old=self._workers.get(tf)
            if old and not old[1].is_set():continue
            box=[];done=threading.Event();pending[tf]=(box,done);self._workers[tf]=(box,done)
            threading.Thread(target=fetch,args=(tf,box,done),daemon=True).start()
        for tf,seconds in INTERVALS.items():
            frames[tf]=[]
            if tf not in pending:continue
            box,done=pending[tf]
            if not done.wait(max(0,deadline-time.monotonic())) or not box:continue
            response=box[0]
            if isinstance(response,Exception):failures.append(response);continue
            body=response.payload;meta=body.get('meta',{})
            if meta.get('symbol')!='XAU/USD' or meta.get('interval')!=tf:raise ValueError('WRONG_MARKET_SYMBOL')
            if meta.get('currency_base') not in (None,'Gold','XAU') or meta.get('currency_quote') not in (None,'US Dollar','USD'):
                raise ValueError('WRONG_CURRENCY_PAIR')
            # Explicit requested UTC plus approved UTC contract; no guessing local exchange time.
            if self.spec.source_timezone!='UTC':raise ValueError('UNVERIFIED_SOURCE_TIMEZONE')
            if meta.get('timezone','UTC')!='UTC':raise ValueError('UNVERIFIED_SOURCE_TIMEZONE')
            responses.append(response)
            try:
                for row in body['values']:
                    at=source_time(row['datetime'],self.spec)
                    if at>now:raise ValueError('FUTURE_CANDLE')
                    if at+timedelta(seconds=seconds)>now:continue
                    frames[tf].append({'t':stamp(at),'o':finite(row['open'],0),'h':finite(row['high'],0),
                                       'l':finite(row['low'],0),'c':finite(row['close'],0)})
                frames[tf].sort(key=lambda r:r['t'])
            except (ValueError,KeyError,TypeError):
                frames[tf]=[]
                if tf=='1min':raise
            if tf=='1min' and ('bid' in body or 'ask' in body):
                quote={'bid':finite(body['bid'],0),'ask':finite(body['ask'],0),
                       'at':stamp(source_time(body['quote_timestamp'],self.spec))}
                if quote['bid']>quote['ask'] or not 0<=(now-parse(quote['at'])).total_seconds()<=150:raise ValueError('INVALID_BID_ASK')
        rate_limits=[e.retry_after for e in failures if isinstance(e,ProviderFailure) and e.code=='RATE_LIMITED']
        if rate_limits:raise ProviderFailure('RATE_LIMITED',max(rate_limits))
        if any(isinstance(e,ProviderFailure) and e.code=='AUTH_OR_ENTITLEMENT_DENIED' for e in failures):raise ProviderFailure('AUTH_OR_ENTITLEMENT_DENIED')
        if not frames['1min']:raise ProviderFailure('NO_CLOSED_CANDLES')
        observed=parse(frames['1min'][-1]['t'])+timedelta(minutes=1)
        return self.finish(frames,observed,now,responses,{'quote':quote,'quote_kind':'VENDOR_BID_ASK' if quote else 'CANDLE_ONLY_NO_EXECUTABLE_QUOTE'},complete=all(frames.values()))

    def _market(self,now):
        s=self.spec;symbol=MAPPINGS[s.channel]
        response=self.request('/markets/symbol/'+symbol,{'f':'json'},now)
        rows=response.payload
        if not isinstance(rows,list) or len(rows)!=1:raise ValueError('AMBIGUOUS_MARKET_RESPONSE')
        row=rows[0]
        if row['Symbol'].upper()!=symbol or (s.channel.startswith('us') and row.get('Country')!='United States'):raise ValueError('WRONG_MARKET_SYMBOL')
        expected='index_points' if s.channel=='dxy' else 'percent'
        if s.unit!=expected:raise ValueError('UNIT_MAPPING_UNAPPROVED')
        allowed=('', 'points','index points') if s.channel=='dxy' else ('%','percent')
        if row.get('unit') not in allowed:raise ValueError('YIELD_OR_INDEX_UNIT_ERROR')
        at=source_time(row['Date'],s);updated=source_time(row['LastUpdate'],s)
        if not at<=updated<=now:raise ValueError('INVALID_SOURCE_CLOCK')
        number=finite(row['Last'],.0001 if s.channel=='dxy' else -5,1000 if s.channel=='dxy' else 30)
        record={'id':symbol+':'+stamp(at),'source':'TradingEconomics','url':'https://tradingeconomics.com/'+symbol.lower(),
                'published_at':stamp(updated),'observed_at':stamp(at),'instrument':SYMBOLS[s.channel],'value':number}
        value={'as_of':stamp(updated),'records':[record]}
        return self.finish(value,at,now,[response],{'unit':expected,'provider_unit':row['unit'],'state':text(row.get('State','UNKNOWN'))},frequency=row.get('frequency','Unknown'))

    def _calendar(self,now):
        s=self.spec;start=now-timedelta(days=2);end=now+timedelta(days=7)
        response=self.request('/calendar/country/united%20states/'+start.strftime('%Y-%m-%d')+'/'+end.strftime('%Y-%m-%d'),{'f':'json'},now)
        rows=response.payload
        if not isinstance(rows,list) or not 1<=len(rows)<1000:raise ValueError('INCOMPLETE_CALENDAR')
        records=[];editions=[];updates=[];seen={}
        for row in rows:
            if row['Country']!='United States' or str(row.get('DateSpan','0'))!='0':raise ValueError('AMBIGUOUS_EVENT_TIME')
            at=source_time(row['Date'],s);updated=source_time(row['LastUpdate'],s)
            if updated>now:raise ValueError('FUTURE_CALENDAR_REVISION')
            name=text(row['Event']);kind=event_kind(name);identity=text(str(row['CalendarId']))
            if row.get('Actual') not in (None,'') and at>now:raise ValueError('PRE_RELEASE_ACTUAL')
            record={'id':identity,'source':text(row.get('Source') or 'TradingEconomics'),'url':'https://tradingeconomics.com'+row['URL'],
                    'published_at':stamp(updated),'event_key':'TE:'+identity,'kind':kind,'name':name,'scheduled_at':stamp(at),
                    'end_at':stamp(at+timedelta(minutes=60 if kind=='FOMC' else 0)),
                    'importance':{1:'LOW',2:'MEDIUM',3:'HIGH'}[int(row['Importance'])],'status':'SCHEDULED'}
            record['url']=source_url(record['url']);records.append(record);updates.append(updated)
            fields={k:None if row.get(k) in (None,'') else text(str(row[k]),100) for k in ('Actual','Previous','Forecast','Revised','Unit','Reference')}
            fingerprint=digest([record,fields])
            if identity in seen and seen[identity]!=fingerprint:raise ValueError('CONFLICTING_CALENDAR_EDITION')
            seen[identity]=fingerprint
            editions.append({'event_id':identity,'scheduled_at':stamp(at),'source_updated_at':stamp(updated),
                             'revision_id':digest([identity,stamp(updated),fields]),**fields})
        observed=max(updates)
        value={'as_of':stamp(observed),'records':records,'coverage':{'complete':True,'country':'US',
               'event_types':['CPI','NFP','PCE','FOMC'],'start':stamp(start),'end':stamp(end)}}
        return self.finish(value,observed,now,[response],{'calendar_editions':editions,'coverage_basis':'OPERATOR_ENTITLEMENT_AND_UNTRUNCATED_COUNTRY_QUERY'},complete=s.calendar_complete)

    def _news(self,now):
        response=self.request('/api/v1/news',{'category':'general'},now);rows=response.payload
        if not isinstance(rows,list) or not 1<=len(rows)<1000:raise ValueError('MISSING_OR_TRUNCATED_NEWS')
        records=[];editions=[];updates=[]
        for row in rows:
            at=datetime.fromtimestamp(finite(row['datetime'],0,10**12),timezone.utc)
            if at>now:raise ValueError('FUTURE_NEWS')
            title=text(row['headline'],1000);tags=relevance(title)
            if not tags:continue
            updates.append(at)
            item={'id':text(str(row['id'])),'source':text(row['source']),'url':source_url(row['url']),
                  'published_at':stamp(at),'title':title,'importance':'HIGH' if 'fed' in tags or 'geopolitical' in tags else 'MEDIUM'}
            records.append(item)
            editions.append({'article_id':item['id'],'revision_id':digest(item),'published_at':stamp(at),'relevance':tags,
                             'syndication_key':digest(' '.join(re.findall(r'\w+',title.lower())))})
        if not records:raise ValueError('NO_RELEVANT_NEWS')
        observed=max(updates)
        return self.finish({'as_of':stamp(observed),'records':records},observed,now,[response],{'news_editions':editions,'revision_basis':'AS_RECEIVED_CONTENT_HASH'})
