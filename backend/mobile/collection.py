"""Opt-in, single-owner XAU acquisition; no broker or other vendor activation."""
from dataclasses import replace
from datetime import datetime, timezone, timedelta
import os
import threading

from ..domain import canonical, parse, stamp, INTERVALS
from ..forward.contracts import normalize
from ..forward.providers import ProviderFailure
from ..forward.runner import ForwardRunner
from ..realdata.adapters import NativeAdapter, synthetic_payload
from ..realdata.collection import RealCollector
from ..realdata.transport import credential


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

    def acquire(self,now):
        if not self.spec.approved_at(now) or not self.spec.live_entitled:raise ProviderFailure('PROVIDER_NOT_APPROVED')
        token=credential(self.spec,self.environ)
        if token.strip().lower() in ('demo','test','fixture'):raise ProviderFailure('INVALID_CREDENTIAL_FORMAT')
        acquisition=super().acquire(now)
        row=normalize(self.spec.legacy,acquisition.envelope,now,acquisition.raw_hash)
        proof=acquisition.verification
        if (row['health']!='HEALTHY' or row['errors'] or row['data_mode']!='LIVE_DATA'
                or not all(proof.get(k) is True for k in ('authenticated','real_transport','entitled','complete'))
                or set(row['value'])!=set(INTERVALS)):
            raise ValueError('XAU_VALIDATION_FAILED')
        return acquisition


class ReceiptValidatedCollector(RealCollector):
    def _read(self,spec,started,done,box):
        inner=[]
        try:
            super()._read(spec,started,threading.Event(),inner)
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
        self.collector=ReceiptValidatedCollector(self.store,self.specs,{xau.name:StrictXauAdapter(xau)},clock)
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
                self._once()
                # No catch-up burst; at most one acquisition per 60 seconds.
                self.stop.wait(60)
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
