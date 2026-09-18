"""Bounded collection with durable request identities and sanitized failures."""
from datetime import datetime,timezone,timedelta
import json,threading,time

from ..domain import canonical,digest,parse,stamp
from .contracts import normalize
from .providers import ProviderFailure,build_provider,secret


def control(conn,key,default=None):
    row=conn.execute('SELECT payload FROM forward_control WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default


def set_control(conn,key,value):
    conn.execute('INSERT OR REPLACE INTO forward_control VALUES (?,?)',(key,canonical(value)))


class Collector:
    def __init__(self,store,specs,providers=None,clock=lambda:datetime.now(timezone.utc)):
        self.store,self.specs,self.clock=store,tuple(specs),clock
        if len(specs)>32 or len({s.name for s in specs})!=len(specs):raise ValueError('INVALID_PROVIDER_SET')
        self.providers=providers if providers is not None else {s.name:build_provider(s) for s in specs}
        self.inflight={}

    def _read(self,spec,started,done,box):
        try:
            provider=self.providers[spec.name]
            raw,raw_hash=provider.fetch(started)
            received=self.clock()
            if received<started:raise ValueError('CLOCK_REVERSED')
            row=normalize(spec,raw,received,raw_hash)
            # Check all configured secret references, not just this provider's token.
            serialized=canonical(row)
            for registered in self.specs:
                try:token=secret(registered,getattr(self.providers.get(registered.name),'environ',None))
                except ProviderFailure:continue
                if token and token in serialized:raise ValueError('SECRET_IN_NORMALIZED_DATA')
            box.append((row,None,0))
        except ProviderFailure as exc:
            allowed={'RATE_LIMITED','HTTP_PROVIDER_FAILURE','PROVIDER_TIMEOUT','PROVIDER_READ_FAILED',
                     'CREDENTIAL_UNAVAILABLE','INVALID_CREDENTIAL_FORMAT','RESPONSE_TOO_LARGE',
                     'INVALID_JSON_OBJECT','PROVIDER_REJECTED','SYMBOL_OR_INTERVAL_MISMATCH',
                     'PROVIDER_TIMEZONE_MISMATCH','FUTURE_PROVIDER_CANDLE','NO_CLOSED_CANDLES'}
            box.append((None,exc.code if exc.code in allowed else 'PROVIDER_FAILED',
                        min(900,max(0,exc.retry_after)) if type(exc.retry_after) is int else 0))
        except Exception:
            box.append((None,'INVALID_OR_UNSAFE_PROVIDER_DATA',0))
        finally:done.set()

    def collect(self,cycle,started):
        pending=[];audits=[];observations=[]
        for spec in self.specs:
            request=digest([spec.identity,stamp(started.replace(second=0,microsecond=0))])
            state_key='provider:'+spec.identity
            with self.store.transaction() as conn:
                state=control(conn,state_key,{})
                existing=conn.execute('SELECT status FROM forward_requests WHERE id=?',(request,)).fetchone()
                reason=None
                if existing:
                    saved=conn.execute('SELECT r.payload_id,r.received_at,r.observation FROM forward_receipts r WHERE r.cycle=? AND r.provider=?',
                                       (cycle,spec.identity)).fetchone()
                    if saved:
                        recovered=self.store.get_observation_document(conn,saved[0]);recovered['received_at']=saved[1]
                        recovered['observation_id']=saved[2]
                        if 'retrieved_at' in recovered['value']:recovered['value']['retrieved_at']=saved[1]
                        observations.append(recovered)
                    reason='DUPLICATE_OR_INTERRUPTED_REQUEST'
                elif state.get('retry_at') and started<parse(state['retry_at']):reason='CIRCUIT_OPEN' if state.get('failures',0)>=3 else 'BACKOFF'
                if reason:
                    audits.append({'provider':spec.name,'channel':spec.channel,'health':'UNAVAILABLE','reason':reason})
                    continue
                conn.execute('INSERT INTO forward_requests VALUES (?,?,?,?,?)',(request,spec.identity,cycle,stamp(started),'STARTED'))
            prior=self.inflight.get(spec.name)
            if prior and not prior[0].is_set():
                pending.append((spec,request,state_key,state,None,None,time.monotonic()))
                continue
            done,box=threading.Event(),[]
            self.inflight[spec.name]=(done,box)
            deadline=time.monotonic()+spec.timeout_seconds
            threading.Thread(target=self._read,args=(spec,started,done,box),daemon=True,name='forward-'+spec.name).start()
            pending.append((spec,request,state_key,state,done,box,deadline))
        for spec,request,state_key,state,done,box,deadline in pending:
            if done and done.wait(max(0,deadline-time.monotonic())) and box:
                row,error,retry=box[0]
                self.inflight.pop(spec.name,None)
            else:row,error,retry=None,'PROVIDER_TIMEOUT',0
            finished=self.clock()
            if finished<started:
                row,error,retry=None,'CLOCK_REVERSED',0
                finished=started
            failures=0 if row else state.get('failures',0)+1
            delay=max(retry,300 if failures>=3 else min(300,5*2**min(failures,6))) if failures else 0
            with self.store.transaction() as conn:
                conn.execute('UPDATE forward_requests SET status=? WHERE id=?',('COMPLETE' if row else error,request))
                set_control(conn,state_key,{'failures':failures,'retry_at':stamp(finished+timedelta(seconds=delay)),
                                           'health':row['health'] if row else 'UNAVAILABLE','error':error})
                if row:
                    row=self.store.archive_observation(conn,cycle,row)
                    observations.append(row)
            audits.append({'provider':spec.name,'channel':spec.channel,'health':row['health'] if row else 'UNAVAILABLE',
                           'reason':error,'request_id':request,'received_at':stamp(finished)})
        return observations,audits
