"""Opt-in, single-owner XAU acquisition; no broker or other vendor activation."""
from dataclasses import replace
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import os
import threading

from ..domain import canonical, parse, stamp, INTERVALS, market_closed
from ..forward.contracts import normalize
from ..forward.providers import ProviderFailure
from ..forward.runner import ForwardRunner
from ..realdata.adapters import NativeAdapter, Acquisition, source_time, finite, synthetic_payload
from ..realdata.collection import RealCollector
from ..realdata.transport import credential
from .xau_alignment_v1 import VERSION as ALIGNMENT_V1, META, normalize_acquisition


def forbidden_marker(value):
    if isinstance(value,list):return any(forbidden_marker(v) for v in value)
    if not isinstance(value,dict):return False
    for key in ('data_mode','data_status'):
        if key in value and value[key] not in ('LIVE','LIVE_DATA'):return True
    for key in ('is_delayed','is_demo','is_test','is_synthetic'):
        if key in value and value[key] is not False:return True
    return synthetic_payload(value) or any(forbidden_marker(v) for v in value.values())


def enabled():
    value=os.environ.get('MOBILE_COLLECTION_ENABLED','false')
    if value not in ('true','false'):raise RuntimeError('INVALID_COLLECTION_FLAG')
    return value=='true'


def xau_spec(candidate):
    """Incomplete or invalid attestations never authorize an HTTP request."""
    try:
        fields={key:os.environ.get('MOBILE_XAU_'+suffix,'') for key,suffix in (
            ('approval_ref','APPROVAL_REF'),('entitlement_ref','ENTITLEMENT_REF'),
            ('entitlement_until','ENTITLEMENT_UNTIL'),('source_timezone','SOURCE_TIMEZONE'),('unit','UNIT'))}
        if not all(fields.values()) or fields['source_timezone']!='UTC' or fields['unit']!='USD_per_troy_ounce':return candidate
        if os.environ.get('MOBILE_XAU_LIVE_ENTITLED')!='true':return candidate
        # Reject accidental secret reuse in operator references, without hashing it.
        token=os.environ.get('TWELVE_DATA_API_KEY','')
        if token and any(token in v for v in fields.values()):return candidate
        return replace(candidate,**fields,approved=True,live_entitled=True)
    except (ValueError,TypeError):return candidate


class StrictXauAdapter(NativeAdapter):
    """Render/Basic-safe adapter: bootstrap all frames once, then poll only 1min."""

    def __init__(self,spec,client=None,environ=None,alignment_version='legacy'):
        super().__init__(spec,client,environ)
        if alignment_version not in ('legacy',ALIGNMENT_V1):raise ValueError('UNKNOWN_ALIGNMENT_VERSION')
        self.alignment_version=alignment_version
        self._mobile_frames={}
        self._mobile_4h_anchor=None
        self._mobile_anchor_evidence=None

    def request(self,path,params,now):
        response=super().request(path,params,now)
        body=response.payload
        token=credential(self.spec,self.environ)
        if token in canonical(body):raise ValueError('UNSAFE_PROVIDER_RESPONSE')
        if forbidden_marker(body):raise ValueError('NONREAL_PROVIDER_RESPONSE')
        if len(body.get('values',[]))>(1000 if params['interval']=='1min' else 125):
            raise ValueError('CANDLE_COUNT_EXCEEDED')
        # Validate every supplied timestamp, including rows the base parser drops.
        # Explicit UTC is requested; offset-bearing timestamps must also be UTC.
        for row in body.get('values',[]):
            at=datetime.fromisoformat(row['datetime'].replace('Z','+00:00'))
            if at.tzinfo is None:at=at.replace(tzinfo=timezone.utc)
            if at.utcoffset()!=timedelta(0) or at>now:raise ValueError('INVALID_CANDLE_CLOCK')
        return response

    @staticmethod
    def _merge(existing,incoming,limit,replace_existing=False):
        rows={row['t']:row for row in existing}
        for row in incoming:
            if replace_existing or row['t'] not in rows:rows[row['t']]=row
        return sorted(rows.values(),key=lambda row:row['t'])[-limit:]

    @staticmethod
    def _native_4h_anchor(rows,now):
        # UTC specifies the clock, not the provider's session/bucket origin.
        seconds=INTERVALS['4h'];opens=[]
        for row in rows:
            at=parse(row['t'])
            if at.utcoffset()!=timedelta(0) or at.microsecond or int(at.timestamp())%60:
                raise ValueError('INVALID_NATIVE_4H_ANCHOR')
            if at+timedelta(seconds=seconds)>now:raise ValueError('UNCLOSED_NATIVE_4H_ANCHOR')
            opens.append(int(at.timestamp()))
        offsets={at%seconds for at in opens}
        if len(opens)<2 or len(set(opens))!=len(opens) or len(offsets)!=1:
            raise ValueError('AMBIGUOUS_NATIVE_4H_ANCHOR')
        return offsets.pop()

    @staticmethod
    def _aggregate(minutes,interval,anchor_seconds=None):
        seconds=INTERVALS[interval];width=seconds//60;buckets={}
        if interval=='4h':
            if type(anchor_seconds) is not int or not 0<=anchor_seconds<seconds or anchor_seconds%60:
                raise ValueError('NATIVE_4H_ANCHOR_REQUIRED')
        else:anchor_seconds=0 # Preserve existing 5min, 15min and 1h boundaries.
        for row in minutes:
            at=parse(row['t']);epoch=int(at.timestamp())
            opened=epoch-((epoch-anchor_seconds)%seconds)
            buckets.setdefault(opened,[]).append(row)
        result=[]
        for opened,rows in sorted(buckets.items()):
            rows=sorted(rows,key=lambda row:row['t'])
            if len(rows)!=width:continue
            start=datetime.fromtimestamp(opened,timezone.utc)
            if any(parse(row['t'])!=start+timedelta(minutes=i) for i,row in enumerate(rows)):continue
            result.append({'t':stamp(start),'o':rows[0]['o'],'h':max(row['h'] for row in rows),
                           'l':min(row['l'] for row in rows),'c':rows[-1]['c']})
        return result

    def _mobile_xau(self,now):
        # One-time process bootstrap retains the original direct provider coverage.
        # Subsequent cycles use one 1min request and derive newly closed higher bars.
        if not self._mobile_frames:
            first=super()._xau(now)
            anchor=self._native_4h_anchor(first.envelope['value']['4h'],now)
            self._mobile_frames={tf:list(first.envelope['value'][tf]) for tf in INTERVALS}
            self._mobile_4h_anchor=anchor
            if self.alignment_version==ALIGNMENT_V1:
                self._mobile_anchor_evidence={'version':ALIGNMENT_V1,'anchor_seconds':anchor,
                    'provider_identity':self.spec.identity,'bootstrap_at':stamp(now),
                    'native_bars':deepcopy(first.envelope['value']['4h']),'raw_hash':first.raw_hash}
            details=dict(first.details,mobile_request_mode='BASIC_BOOTSTRAP_5_REQUESTS',
                         derived_intervals=[],aggregation_anchors_seconds={'4h':anchor},
                         aggregation_anchor_source='NATIVE_4H_BOOTSTRAP')
            if self._mobile_anchor_evidence:details[META]=self._mobile_anchor_evidence
            return Acquisition(first.envelope,first.raw_hash,first.verification,details)

        response=self.request('/time_series',{'symbol':'XAU/USD','interval':'1min','timezone':'UTC','outputsize':1000},now)
        body=response.payload;meta=body.get('meta',{})
        if meta.get('symbol')!='XAU/USD' or meta.get('interval')!='1min':raise ValueError('WRONG_MARKET_SYMBOL')
        if meta.get('currency_base') not in (None,'Gold','Gold Spot','XAU') or meta.get('currency_quote') not in (None,'US Dollar','USD'):
            raise ValueError('WRONG_CURRENCY_PAIR')
        if self.spec.source_timezone!='UTC' or meta.get('timezone','UTC')!='UTC':raise ValueError('UNVERIFIED_SOURCE_TIMEZONE')
        minutes=[]
        for row in body.get('values',[]):
            at=source_time(row['datetime'],self.spec)
            if at>now:raise ValueError('FUTURE_CANDLE')
            if at+timedelta(minutes=1)>now:continue
            minutes.append({'t':stamp(at),'o':finite(row['open'],0),'h':finite(row['high'],0),
                            'l':finite(row['low'],0),'c':finite(row['close'],0)})
        minutes.sort(key=lambda row:row['t'])
        if not minutes:raise ProviderFailure('NO_CLOSED_CANDLES')
        if len({row['t'] for row in minutes})!=len(minutes):raise ValueError('DUPLICATE_CANDLE')

        self._mobile_frames['1min']=self._merge(self._mobile_frames['1min'],minutes,1000,True)
        for interval in ('5min','15min','1h','4h'):
            derived=self._aggregate(minutes,interval,self._mobile_4h_anchor)
            self._mobile_frames[interval]=self._merge(self._mobile_frames[interval],derived,125,False)
        frames={tf:list(self._mobile_frames[tf]) for tf in INTERVALS}
        if not all(frames.values()):raise ProviderFailure('NO_CLOSED_CANDLES')
        observed=parse(frames['1min'][-1]['t'])+timedelta(minutes=1)
        return self.finish(frames,observed,now,[response],
                           {'quote':None,'quote_kind':'CANDLE_ONLY_NO_EXECUTABLE_QUOTE',
                            'mobile_request_mode':'BASIC_STEADY_1_REQUEST',
                            'aggregation_anchors_seconds':{'4h':self._mobile_4h_anchor},
                            'aggregation_anchor_source':'NATIVE_4H_BOOTSTRAP',
                            **({META:self._mobile_anchor_evidence} if self._mobile_anchor_evidence else {}),
                            'derived_intervals':['5min','15min','1h','4h']},
                           complete=True)

    def acquire(self,now):
        if not self.spec.approved_at(now) or not self.spec.live_entitled:raise ProviderFailure('PROVIDER_NOT_APPROVED')
        token=credential(self.spec,self.environ)
        if token.strip().lower() in ('demo','test','fixture'):raise ProviderFailure('INVALID_CREDENTIAL_FORMAT')
        previous_frames={tf:list(rows) for tf,rows in self._mobile_frames.items()}
        previous_anchor=self._mobile_4h_anchor
        previous_evidence=self._mobile_anchor_evidence
        try:
            acquisition=self._mobile_xau(now)
            row=(normalize_acquisition(self.spec,acquisition,now) if self.alignment_version==ALIGNMENT_V1
                 else normalize(self.spec.legacy,acquisition.envelope,now,acquisition.raw_hash))
            proof=acquisition.verification
            if (row['health']!='HEALTHY' or row['errors'] or row['data_mode']!='LIVE_DATA'
                    or not all(proof.get(k) is True for k in ('authenticated','real_transport','entitled','complete'))
                    or set(row['value'])!=set(INTERVALS)):
                raise ValueError('XAU_VALIDATION_FAILED')
            return acquisition
        except Exception:
            # Rejected native alignment must never seed a later steady-state poll.
            self._mobile_frames=previous_frames
            self._mobile_4h_anchor=previous_anchor
            self._mobile_anchor_evidence=previous_evidence
            raise


class ReceiptValidatedCollector(RealCollector):
    def _scoped_read(self,spec,started,inner):
        adapter=self.providers[spec.name]
        try:
            acquisition=adapter.acquire(started);received=self.clock()
            if received<started:raise ValueError('CLOCK_REVERSED')
            row=normalize_acquisition(adapter.spec,acquisition,received)
            row['native']={'verification':acquisition.verification,'details':acquisition.details}
            if credential(adapter.spec,adapter.environ) in canonical(row):raise ValueError('SECRET_IN_NORMALIZED_DATA')
            with self.store.connect() as conn:
                prior=conn.execute('SELECT payload_id FROM forward_receipts WHERE provider=? ORDER BY received_at DESC LIMIT 1',(spec.identity,)).fetchone()
                if prior:
                    old=self.store.get_observation_document(conn,prior[0])['native']['details'].get(META)
                    if not old or old['anchor_seconds']!=acquisition.details[META]['anchor_seconds']:
                        raise ValueError('ANCHOR_CHANGE_REQUIRES_NEW_EPOCH')
            inner.append((row,None,0))
        except ProviderFailure as exc:
            allowed={'PROVIDER_NOT_APPROVED','CREDENTIAL_UNAVAILABLE','INVALID_CREDENTIAL_FORMAT','AUTH_OR_ENTITLEMENT_DENIED',
                'RATE_LIMITED','HTTP_PROVIDER_FAILURE','PROVIDER_TIMEOUT','PROVIDER_REJECTED','PROVIDER_READ_FAILED',
                'RESPONSE_TOO_LARGE','MALFORMED_PAYLOAD','NO_CLOSED_CANDLES'}
            inner.append((None,exc.code if exc.code in allowed else 'PROVIDER_FAILED',
                          min(900,max(0,exc.retry_after)) if type(exc.retry_after) is int else 0))
        except Exception:inner.append((None,'INVALID_OR_UNSAFE_PROVIDER_DATA',0))

    def _read(self,spec,started,done,box):
        inner=[]
        try:
            if self.providers[spec.name].alignment_version==ALIGNMENT_V1:self._scoped_read(spec,started,inner)
            else:super()._read(spec,started,threading.Event(),inner)
            row,error,retry=inner[0]
            native=self.providers[spec.name].spec
            # Network latency can cross freshness or entitlement boundaries after
            # the adapter's request-time checks. Reject before archive_observation.
            if row and (row['health']!='HEALTHY' or row['errors'] or row['data_mode']!='LIVE_DATA'
                        or not native.approved_at(parse(row['received_at']))):
                row,error,retry=None,'VALIDATION_AT_RECEIPT_FAILED',0
            box.append((row,error,retry))
        except Exception:box.append((None,'INVALID_OR_UNSAFE_PROVIDER_DATA',0))
        finally:done.set()


class MobileCollector(ForwardRunner):
    """Reuse causal cycle/recovery algorithms, never local-only runner constructors."""
    def __init__(self,runtime,clock=lambda:datetime.now(timezone.utc)):
        self.runtime=runtime;self.clock=clock;self.store=runtime.store;self.engine=runtime.engine
        self.configuration=self.engine.configuration
        xau=next(s for s in runtime.specs if s.channel=='xau')
        self.specs=(xau.legacy,)
        self.collector=ReceiptValidatedCollector(self.store,self.specs,
            {xau.name:StrictXauAdapter(xau,alignment_version=runtime.validator_version)},clock)
        self.stop=threading.Event();self.thread=None
        self.failure=None

    def _once(self):
        if not self.runtime.collection_enabled:return {'status':'COLLECTION_DISABLED'}
        # Reserve additional headroom for a bounded cycle; never prune evidence.
        state=self.runtime.storage()
        if not state['within_budget'] or state['allocated_bytes']+16*1024*1024>=state['budget_bytes']:
            self.failure='STORAGE_BUDGET_EXCEEDED'
            return {'status':self.failure}
        try:
            result=super()._once()
            if not self.runtime.storage()['within_budget']:raise RuntimeError('STORAGE_BUDGET_EXCEEDED')
            self.failure=None
            return result
        except Exception:
            self.failure='COLLECTION_OR_INTEGRITY_FAILURE'
            # Never include exception messages/provider documents in logs or APIs.
            return {'status':self.failure}

    def start(self):
        if self.thread is not None:return
        def loop():
            while not self.stop.is_set():
                now=self.clock()
                if not market_closed(now):self._once()
                # Basic-safe cadence: one steady-state API request every two minutes,
                # aligned just after a UTC 1min candle close. No catch-up bursts.
                now=self.clock();epoch=now.timestamp()
                next_tick=((int(epoch)//120)+1)*120+5
                self.stop.wait(max(1,next_tick-epoch))
        self.thread=threading.Thread(target=loop,name='mobile-xau',daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:self.thread.join() # Retain ownership until the writer exits.

    def status(self,now):
        with self.store.connect() as conn:
            row=conn.execute('SELECT payload_id FROM forward_receipts ORDER BY received_at DESC LIMIT 1').fetchone()
            value=self.store.get_observation_document(conn,row[0]) if row else None
            request=conn.execute('SELECT status,at FROM forward_requests ORDER BY at DESC LIMIT 1').fetchone()
        spec=next(s for s in self.runtime.specs if s.channel=='xau')
        age=(now-parse(value['observed_at'])).total_seconds() if value else None
        valid=bool(value and 0<=age<150 and parse(value['received_at'])<=now and value.get('native',{}).get('live_verified')
                   and spec.approved_at(now) and request and request['status']=='COMPLETE' and not self.failure
                   and self.runtime.collection_enabled and bool(os.environ.get('TWELVE_DATA_API_KEY','').strip())
                   and self.runtime.storage()['within_budget'])
        return {'approval':'APPROVED_CONFIGURATION' if spec.approved_at(now) else 'UNAPPROVED',
                'credential_configured':bool(os.environ.get('TWELVE_DATA_API_KEY','').strip()),
                'last_observed_at':value['observed_at'] if value else None,
                'received_at':value['received_at'] if value else None,'age_seconds':age,
                'freshness':'FRESH' if age is not None and 0<=age<150 else 'STALE' if value else 'UNAVAILABLE',
                'data_mode':'LIVE_DATA' if valid else 'UNAVAILABLE',
                'status':'HEALTHY' if valid else 'UNAVAILABLE',
                'last_attempt_status':self.failure or (request['status'] if request else 'NOT_ATTEMPTED'),
                'validation_checks':value.get('native',{}).get('checks',[]) if value else [],
                'symbol':'XAU/USD','vendor':'twelve_data','channel':'xau'}
