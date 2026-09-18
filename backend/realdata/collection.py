"""Reuse durable Phase 3E retries while capturing native verification and editions."""
from ..domain import canonical,stamp,parse
from ..forward.collection import Collector
from ..forward.contracts import normalize
from ..forward.providers import ProviderFailure
from .transport import credential


class RealCollector(Collector):
    secondary=None

    def collect(self,cycle,started):
        observations,audits=super().collect(cycle,started)
        if self.secondary and self.secondary.config.enabled:
            # Recovery reuses the immutable first receipt for this cycle.
            with self.store.connect() as conn:
                prior=conn.execute("SELECT 1 FROM forward_ledger WHERE event_key=?",('secondary:'+cycle,)).fetchone()
                latest=conn.execute("SELECT at FROM forward_ledger WHERE kind='SECONDARY_CALENDAR' ORDER BY seq DESC LIMIT 1").fetchone()
            due=not latest or (self.clock()-parse(latest[0])).total_seconds()>=self.secondary.config.poll_seconds
            if not prior and due:
                try:result=self.secondary.read(self.clock())
                except Exception:result={'status':'UNAVAILABLE','data_mode':'UNAVAILABLE','provider':'ForexFactory','events':[]}
                with self.store.transaction() as conn:
                    self.store.event(conn,'secondary:'+cycle,'SECONDARY_CALENDAR',stamp(self.clock()),result)
        return observations,audits

    def _read(self,spec,started,done,box):
        try:
            adapter=self.providers[spec.name]
            acquisition=adapter.acquire(started);received=self.clock()
            if received<started:raise ValueError('CLOCK_REVERSED')
            row=normalize(spec,acquisition.envelope,received,acquisition.raw_hash)
            row['native']={'verification':acquisition.verification,'details':acquisition.details}
            serialized=canonical(row)
            for registered in self.providers.values():
                try:token=credential(registered.spec,registered.environ)
                except ProviderFailure:continue
                if token and token in serialized:raise ValueError('SECRET_IN_NORMALIZED_DATA')
            box.append((row,None,0))
        except ProviderFailure as exc:
            allowed={'PROVIDER_NOT_APPROVED','CREDENTIAL_UNAVAILABLE','INVALID_CREDENTIAL_FORMAT','AUTH_OR_ENTITLEMENT_DENIED',
                     'RATE_LIMITED','HTTP_PROVIDER_FAILURE','PROVIDER_TIMEOUT','PROVIDER_REJECTED','PROVIDER_READ_FAILED',
                     'RESPONSE_TOO_LARGE','MALFORMED_PAYLOAD','NO_CLOSED_CANDLES'}
            box.append((None,exc.code if exc.code in allowed else 'PROVIDER_FAILED',
                        min(900,max(0,exc.retry_after)) if type(exc.retry_after) is int else 0))
        except Exception:box.append((None,'INVALID_OR_UNSAFE_PROVIDER_DATA',0))
        finally:done.set()
