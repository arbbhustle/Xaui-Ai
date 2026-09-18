"""Bounded read-only HTTPS adapters. Credentials never enter URLs or return values."""
from datetime import datetime,timezone,timedelta
from hashlib import sha256
import json,os,threading,time
import httpx
from typing import Protocol

from ..domain import INTERVALS,digest,parse,stamp
from .contracts import SYMBOLS


class Provider(Protocol):
    """Return a source-stamped envelope and SHA-256 raw-body identity, never secrets."""
    def fetch(self,now:datetime) -> tuple[dict,str]: ...


class ProviderFailure(Exception):
    def __init__(self,code,retry_after=0):
        super().__init__(code);self.code=code;self.retry_after=retry_after


class HTTPSReader:
    def __init__(self,transport=None):self.transport=transport

    def read(self,url,headers,params,timeout):
        try:
            with httpx.Client(timeout=httpx.Timeout(timeout),follow_redirects=False,transport=self.transport,
                              trust_env=False) as client:
                with client.stream('GET',url,headers=headers,params=params) as response:
                    if response.status_code==429:
                        try:retry=max(1,min(900,int(response.headers.get('Retry-After','60'))))
                        except (TypeError,ValueError):retry=60
                        raise ProviderFailure('RATE_LIMITED',retry)
                    if response.status_code!=200:raise ProviderFailure('HTTP_PROVIDER_FAILURE')
                    body=bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body)>2*1024*1024:raise ProviderFailure('RESPONSE_TOO_LARGE')
                    payload=json.loads(body)
                    if not isinstance(payload,dict):raise ProviderFailure('INVALID_JSON_OBJECT')
                    return payload,sha256(body).hexdigest()
        except ProviderFailure:raise
        except httpx.TimeoutException:raise ProviderFailure('PROVIDER_TIMEOUT') from None
        except Exception:raise ProviderFailure('PROVIDER_READ_FAILED') from None


def secret(spec,environ=None):
    env=os.environ if environ is None else environ
    if not spec.secret_env:return ''
    value=env.get(spec.secret_env,'').strip()
    if not value:raise ProviderFailure('CREDENTIAL_UNAVAILABLE')
    if '\n' in value or '\r' in value:raise ProviderFailure('INVALID_CREDENTIAL_FORMAT')
    return value


class ApprovedJSONProvider:
    """Adapter for an approved vendor/gateway implementing the documented schema.

    Supports every channel; does not guess unselected vendors' symbol mappings or
    fabricate endpoint-specific adapters. Operators must verify the gateway and
    its source entitlements before configuration approval.
    """
    def __init__(self,spec,reader=None,environ=None):
        self.spec=spec;self.reader=reader or HTTPSReader();self.environ=environ

    def fetch(self,now):
        token=secret(self.spec,self.environ)
        headers={'Authorization':'Bearer '+token} if token else {}
        return self.reader.read(self.spec.endpoint,headers,{'channel':self.spec.channel,'symbol':SYMBOLS[self.spec.channel]},self.spec.timeout_seconds)


class TwelveDataProvider(ApprovedJSONProvider):
    """Five intraday XAU/USD frames; explicit UTC and header authentication.

    Source: https://twelvedata.com/docs and its official timezone documentation.
    LIVE entitlement is operator-attested, never inferred merely from a 200 status.
    """
    def fetch(self,now):
        token=secret(self.spec,self.environ)
        if not token:raise ProviderFailure('CREDENTIAL_UNAVAILABLE')
        def frame(interval):
            body,raw_hash=self.reader.read(self.spec.endpoint,{'Authorization':'apikey '+token},
                {'symbol':'XAU/USD','interval':interval,'timezone':'UTC','outputsize':1000 if interval=='1min' else 125,'format':'JSON'},self.spec.timeout_seconds)
            if body.get('status')=='error':raise ProviderFailure('PROVIDER_REJECTED')
            meta=body.get('meta',{})
            if meta.get('symbol')!='XAU/USD' or meta.get('interval')!=interval:raise ProviderFailure('SYMBOL_OR_INTERVAL_MISMATCH')
            if meta.get('exchange_timezone') not in ('UTC','Etc/UTC'):raise ProviderFailure('PROVIDER_TIMEZONE_MISMATCH')
            rows=[]
            for r in body['values']:
                text=r['datetime'].replace(' ','T')
                if len(text)==19:text+='+00:00'  # Only under the verified explicit UTC response contract.
                at=parse(text)
                if at>now:raise ProviderFailure('FUTURE_PROVIDER_CANDLE')
                if at+timedelta(seconds=INTERVALS[interval])>now:continue
                rows.append({'t':stamp(at),'o':float(r['open']),'h':float(r['high']),
                             'l':float(r['low']),'c':float(r['close']),'v':None})
            if not rows:raise ProviderFailure('NO_CLOSED_CANDLES')
            return interval,sorted(rows,key=lambda r:r['t']),raw_hash,body
        frames={};hashes={};markers=[]
        if not hasattr(self,'_frames_inflight'):self._frames_inflight={}
        pending={};failures=[];deadline=time.monotonic()+self.spec.timeout_seconds*.85
        def read(interval,done,box):
            try:box.append(frame(interval))
            except ProviderFailure as exc:box.append(exc)
            except Exception:pass  # Empty frame is explicit degraded data; no raw exception retained.
            finally:done.set()
        for interval in INTERVALS:
            old=self._frames_inflight.get(interval)
            if old and not old[0].is_set():continue
            done,box=threading.Event(),[]
            self._frames_inflight[interval]=(done,box);pending[interval]=(done,box)
            threading.Thread(target=read,args=(interval,done,box),daemon=True,name='xau-'+interval).start()
        for interval in INTERVALS:
            frames[interval]=[]
            if interval not in pending:continue
            done,box=pending[interval]
            if not done.wait(max(0,deadline-time.monotonic())) or not box:continue
            if isinstance(box[0],ProviderFailure):failures.append(box[0]);continue
            _,rows,raw_hash,body=box[0]
            frames[interval]=rows;hashes[interval]=raw_hash
            def collect(v):
                if isinstance(v,dict):
                    markers.append({k:v[k] for k in ('data_mode','is_synthetic','delay_seconds') if k in v})
                    for child in v.values():collect(child)
                elif isinstance(v,list):
                    for child in v:collect(child)
            collect(body)
        limited=[e.retry_after for e in failures if e.code=='RATE_LIMITED']
        if limited:raise ProviderFailure('RATE_LIMITED',max(limited))
        if not frames['1min']:raise ProviderFailure('NO_CLOSED_CANDLES')
        observed=parse(frames['1min'][-1]['t'])+timedelta(minutes=1)
        return {'symbol':'XAU/USD','data_mode':self.spec.mode,'observed_at':stamp(observed),
                'published_at':stamp(observed),'revision_id':digest(frames),'value':frames,
                'raw_payload_hashes':hashes,'source_markers':markers},digest(hashes)


def build_provider(spec,**kwargs):
    return (TwelveDataProvider if spec.adapter=='twelve_data' else ApprovedJSONProvider)(spec,**kwargs)
