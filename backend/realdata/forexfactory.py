"""Optional official JSON export cross-check. Never an authoritative LIVE feed."""
from dataclasses import dataclass
from datetime import timedelta
from email.utils import parsedate_to_datetime
import json,re,time
import httpx
from ..domain import parse,stamp,digest
from ..intelligence_providers import text
from .adapters import event_kind,synthetic_payload

EXPORT_URL='https://nfs.faireconomy.media/ff_calendar_thisweek.json'


@dataclass(frozen=True)
class ForexFactoryConfig:
    enabled:bool=False
    usage_validated:bool=False
    approval_ref:str=''
    max_age_seconds:int=3600
    timeout_seconds:float=8
    poll_seconds:int=300

    def __post_init__(self):
        if type(self.enabled) is not bool or type(self.usage_validated) is not bool:raise ValueError('INVALID_SECONDARY_FLAGS')
        if self.approval_ref and not re.fullmatch('[A-Za-z0-9_.:-]{1,100}',self.approval_ref):raise ValueError('INVALID_APPROVAL_REFERENCE')
        if type(self.max_age_seconds) is not int or not 60<=self.max_age_seconds<=3600:raise ValueError('INVALID_EXPORT_FRESHNESS')
        if type(self.timeout_seconds) not in (int,float) or not .05<=self.timeout_seconds<=20:raise ValueError('INVALID_TIMEOUT')
        if type(self.poll_seconds) is not int or not 300<=self.poll_seconds<=3600:raise ValueError('INVALID_EXPORT_POLL_INTERVAL')


def event_signature(name):
    """Conservative metric identity: core/headline and monthly/yearly are distinct."""
    name=name.lower();kind=event_kind(name)
    if 'federal funds rate' in name:kind='FOMC'
    period='YOY' if any(t in name for t in ('y/y','yoy','year on year','year-on-year')) else 'MOM' if any(t in name for t in ('m/m','mom','month on month','month-on-month')) else 'UNSPECIFIED'
    if kind=='FOMC':
        if any(t in name for t in ('speak','speech','powell','chair')):
            # Do not merge different speakers at the same time.
            speaker=re.sub(r'\b(fomc|fed|member|chair|chairman|speech|speaks|speak|jerome)\b','',name)
            return (kind,'SPEECH',' '.join(speaker.split()))
        return (kind,'DECISION' if 'rate' in name or 'funds' in name or 'decision' in name else 'STATEMENT',period)
    return (kind,'CORE' if 'core' in name else 'HEADLINE',period)


class ForexFactoryAdapter:
    def __init__(self,config=None,transport=None):
        self.config=config or ForexFactoryConfig();self.transport=transport

    def read(self,now):
        c=self.config
        if not c.enabled:return {'status':'DISABLED','provider':'ForexFactory','data_mode':'UNAVAILABLE','events':[]}
        if not c.usage_validated or not c.approval_ref:raise ValueError('FOREX_FACTORY_USAGE_UNVALIDATED')
        try:
            deadline=time.monotonic()+c.timeout_seconds
            with httpx.Client(transport=self.transport,trust_env=False,follow_redirects=False,timeout=c.timeout_seconds) as client:
                with client.stream('GET',EXPORT_URL) as response:
                    if response.status_code!=200:raise ValueError('EXPORT_HTTP_FAILURE')
                    raw=bytearray()
                    for part in response.iter_bytes():
                        raw.extend(part)
                        if len(raw)>2*1024*1024 or time.monotonic()>deadline:raise ValueError('EXPORT_RESOURCE_LIMIT')
                    updated=parsedate_to_datetime(response.headers['Last-Modified'])
                    return self.normalize(json.loads(raw),now,updated,test_data=self.transport is not None)
        except Exception:raise ValueError('SECONDARY_EXPORT_UNAVAILABLE_OR_INVALID') from None

    def normalize(self,rows,received_at,source_updated_at,*,test_data=True):
        # Public offline normalization defaults to TEST. Only read() can attest its transport.
        if source_updated_at.tzinfo is None or received_at.tzinfo is None:raise ValueError('NAIVE_EXPORT_CLOCK')
        updated=parse(stamp(source_updated_at));age=(received_at-updated).total_seconds()
        test_data=test_data or synthetic_payload(rows)
        if age<0:raise ValueError('FUTURE_EXPORT')
        if not isinstance(rows,list) or not 0<len(rows)<2000:raise ValueError('INVALID_EXPORT')
        events=[];seen=set()
        for row in rows:
            if row.get('country')!='USD':continue
            scheduled=parse(row['date'])  # Reject all-day/tentative/naive times; never guess a timezone.
            title=text(row['title'],500);kind=event_kind(title)
            if 'federal funds rate' in title.lower():kind='FOMC'
            fields={k:None if row.get(k) in ('',None) else text(str(row[k]),100) for k in ('actual','forecast','previous','revised')}
            if fields['actual'] is not None and scheduled>updated:raise ValueError('PRE_RELEASE_EXPORT_ACTUAL')
            impact={'High':'HIGH','Medium':'MEDIUM','Low':'LOW','Holiday':'LOW','Non-Economic':'LOW'}[row['impact']]
            identity=digest(['USD',event_signature(title),stamp(scheduled)])
            event={'id':identity,'provider':'ForexFactory','title':title,'kind':kind,'scheduled_at':stamp(scheduled),
                'importance':impact,'source_updated_at':stamp(updated),'received_at':stamp(received_at),**fields}
            event['revision_id']=digest({k:v for k,v in event.items() if k not in ('received_at','source_updated_at')})
            if event['revision_id'] not in seen:events.append(event);seen.add(event['revision_id'])
        return {'provider':'ForexFactory','role':'SECONDARY_CROSS_CHECK_ONLY','data_mode':'TEST_DATA' if test_data else 'DELAYED_DATA',
            'status':'STALE' if age>self.config.max_age_seconds else 'EXPORT_AVAILABLE_NOT_LIVE',
            'received_at':stamp(received_at),'source_updated_at':stamp(updated),'freshness_seconds':age,
            'real_transport':not test_data,'events':events,'revision_id':digest(events)}


def cross_check(primary,secondary,at):
    """Return matches/conflicts without replacing either source or upgrading a mode."""
    result={'matches':[],'conflicts':[],'unmatched_secondary':[],'vetoes':[],'authoritative_provider':primary.get('provider') if primary else None}
    if not primary or parse(secondary.get('received_at',stamp(at)))>at:return result
    records=primary['value']['records'];editions={e['event_id']:e for e in primary.get('native',{}).get('details',{}).get('calendar_editions',[])}
    for event in secondary.get('events',[]):
        if parse(event['received_at'])>at:continue
        signature=event_signature(event['title']);scheduled=parse(event['scheduled_at'])
        candidates=[r for r in records if event_signature(r['name'])==signature and abs((parse(r['scheduled_at'])-scheduled).total_seconds())<=86400]
        if len(candidates)!=1:
            result['unmatched_secondary'].append({'secondary':event['id'],'reason':'AMBIGUOUS' if candidates else 'NO_MATCH'});continue
        p=candidates[0];fields=[]
        if p['scheduled_at']!=event['scheduled_at']:fields.append('scheduled_at')
        if p['importance']!=event['importance']:fields.append('importance')
        values=editions.get(p['id'],{})
        for name in ('actual','forecast','previous'):
            a,b=values.get(name.title()),event[name]
            if a is not None and b is not None and str(a).strip()!=str(b).strip():fields.append(name)
        evidence={'primary':p['id'],'secondary':event['id'],'primary_event':p,'primary_values':values,'secondary_event':event,'fields':fields}
        result['conflicts' if fields else 'matches'].append(evidence)
        if fields and (p['importance']=='HIGH' or event['importance']=='HIGH') and secondary['status']=='EXPORT_AVAILABLE_NOT_LIVE' and secondary['data_mode']=='DELAYED_DATA' and secondary['real_transport'] is True:
            result['vetoes'].append('SECONDARY_CALENDAR_DISAGREEMENT')
    result['vetoes']=sorted(set(result['vetoes']));return result
