"""Separate Phase 3F runner; no mutation of the committed Phase 3E base."""
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
import json,os,threading
from ..domain import canonical,digest,parse,stamp
from ..worker import WorkerLock
from ..forward.runner import ForwardRunner,ForwardEngine,implementation_hash as base_hash
from ..forward.collection import control,set_control
from .adapters import NativeAdapter,synthetic_payload
from .collection import RealCollector
from .store import RealStore
from .outcomes import finalize
from ..forward.runner import KEY
from ..intelligence_providers import BUNDLE_KEY


def code_hash():
    return digest({'phase3e':base_hash(),'native':{p.name:p.read_text(encoding='utf-8') for p in sorted(Path(__file__).parent.glob('*.py'))}})


class RealEngine(ForwardEngine):
    def __init__(self,store,configuration):
        super().__init__(store,configuration)
        self.forward_hash=code_hash()
        self.model_identity=digest([self.model_identity,self.forward_hash])

    def snapshot_context(self,conn,snapshot,checked):
        capture=self.store.get_capture(conn,snapshot['frames'][KEY]['capture_id'])
        bundle=snapshot['frames'][BUNDLE_KEY]
        for channel,target in (('dxy','usd'),('us2y','yields'),('us10y','yields')):
            selected=capture['selection']['selected'].get(channel)
            if not selected:continue
            history=[]
            for row in conn.execute('SELECT payload_id FROM forward_receipts WHERE provider=? AND received_at<? ORDER BY received_at DESC LIMIT 120',
                                    (selected['provider_identity'],snapshot['observed_at'])):
                old=self.store.get_observation_document(conn,row[0])
                if old['data_mode']=='LIVE_DATA' and old.get('native',{}).get('live_verified') and not synthetic_payload(old['value']):
                    history.extend(old['value']['records'])
            # Newest receipt wins per economic observation; each edition remains archived.
            merged={}
            for record in bundle[target]['records']+history:
                identity=(record['source'],record['instrument'],record['observed_at'])
                if parse(record['published_at'])<=parse(snapshot['observed_at']):merged.setdefault(identity,record)
            bundle[target]['records']=sorted(merged.values(),key=canonical)[-1000:]
            # The merged envelope frontier must include both independent yield publications.
            # Each constituent timestamp/freshness was verified before selection.
            if bundle[target]['records']:
                bundle[target]['as_of']=max(r['published_at'] for r in bundle[target]['records'])
        return super().snapshot_context(conn,snapshot,checked)

    def after_tick(self,conn,snapshot,result,now,snapshot_id):
        super().after_tick(conn,snapshot,result,now,snapshot_id)
        conn.execute('SAVEPOINT native_outcome_analytics')
        try:finalize(conn,self.store,now)
        except Exception:
            conn.execute('ROLLBACK TO native_outcome_analytics')
            set_control(conn,'native_outcome_block',{'at':stamp(now),'reason':'NATIVE_OUTCOME_ANALYTICS_FAILED'})
        finally:conn.execute('RELEASE native_outcome_analytics')


class RealRunner(ForwardRunner):
    def __init__(self,path,specs=(),*,collection_enabled=False,providers=None,secondary=None,clock=lambda:datetime.now(timezone.utc)):
        if os.environ.get('RENDER'):raise RuntimeError('LOCAL_PHASE3F_ONLY')
        if type(collection_enabled) is not bool:raise ValueError('INVALID_COLLECTION_FLAG')
        if len({s.name for s in specs})!=len(specs):raise ValueError('DUPLICATE_PROVIDER_NAME')
        self.clock=clock;self.native_specs=tuple(specs);self.specs=tuple(s.legacy for s in specs)
        self.collection_enabled=collection_enabled;self.store=RealStore(path,specs);self.store.verify()
        from .forexfactory import ForexFactoryAdapter
        self.secondary=secondary or ForexFactoryAdapter()
        self.configuration=digest({'providers':[asdict(s) for s in specs],'collection_enabled':collection_enabled,'secondary':asdict(self.secondary.config)})
        self.engine=RealEngine(self.store,self.configuration)
        with self.store.connect() as conn:
            for table in ('decisions','trade_events','phase3b_shadow_decisions','phase3b_shadow_events','phase3b_context',
                          'meta_decisions','meta_outcomes','meta_position_events'):
                for operation in ('UPDATE','DELETE'):
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable evidence'); END")
            self.existing_checkpoint=control(conn,'checkpoint')
        adapters=providers if providers is not None else {s.name:NativeAdapter(s) for s in specs}
        self.collector=RealCollector(self.store,self.specs,adapters,clock)
        self.collector.secondary=self.secondary
        self.lock=WorkerLock(self.store.path+'.forward.lock');self.stop=threading.Event()
        with self.store.transaction() as conn:
            identity={'configuration':self.configuration,'code':self.engine.forward_hash,'model':self.engine.model_identity,'phase':'3F'}
            old=control(conn,'identity')
            if old and old!=identity:raise ValueError('NEW_NATIVE_CONFIG_REQUIRES_NEW_DATABASE')
            set_control(conn,'identity',identity)

    def _once(self):
        if not self.collection_enabled:return {'status':'COLLECTION_DISABLED','readiness':'NOT_READY'}
        result=super()._once()
        if self.existing_checkpoint and self._verify_replay():
            with self.store.transaction() as conn:
                set_control(conn,'native_restart_verified',{'at':stamp(self.clock()),'prior_checkpoint':self.existing_checkpoint})
        return result

    def _evaluate(self,cycle,capture_id,capture):
        # put_capture may append secondary cross-check vetoes; evaluate the archived edition.
        with self.store.connect() as conn:capture=self.store.get_capture(conn,capture_id)
        return super()._evaluate(cycle,capture_id,capture)

    def status(self,now=None):
        now=now or self.clock();base=super().status(now)
        reasons=list(base.get('reasons',[]))
        if 'STORAGE_OR_EVIDENCE_INTEGRITY_FAILURE' in reasons:return base
        if not self.collection_enabled:reasons.append('COLLECTION_DISABLED')
        if any(not s.approved_at(now) for s in self.native_specs):reasons.append('PROVIDER_APPROVAL_OR_ENTITLEMENT_MISSING')
        with self.store.connect() as conn:
            if control(conn,'native_outcome_block'):reasons.append('NATIVE_OUTCOME_ANALYTICS_FAILED')
            latest=conn.execute("SELECT capture_id FROM forward_cycles WHERE status='COMPLETE' ORDER BY observed_at DESC LIMIT 1").fetchone()
            if latest:
                captured=self.store.get_capture(conn,latest[0])
                from .reports import real_capture
                if not real_capture(captured):reasons.append('NATIVE_PROVENANCE_UNVERIFIED')
                reasons.extend(captured.get('secondary_calendar',{}).get('cross_check',{}).get('vetoes',[]))
        base['status']='NOT_READY'
        if not reasons:
            with self.store.connect() as conn:
                rows=conn.execute("SELECT observed_at,capture_id FROM forward_cycles WHERE status='COMPLETE' ORDER BY observed_at DESC LIMIT 16").fetchall()
                restart=control(conn,'native_restart_verified')
                captures=[self.store.get_capture(conn,r[1]) for r in rows]
                decision=conn.execute('SELECT payload FROM decisions ORDER BY candle_close DESC LIMIT 1').fetchone()
                history_ready=bool(decision) and not any('MISSING_MOMENTUM_HISTORY' in v for v in json.loads(decision[0])['veto_codes'])
            from .reports import real_capture
            continuous=len(rows)==16 and all(real_capture(c) for c in captures) and all(
                45<=(parse(a[0])-parse(b[0])).total_seconds()<=90 for a,b in zip(rows,rows[1:]))
            base['status']='FORWARD_DEMO_READY' if continuous and restart and history_ready and base.get('replay',{}).get('ok') else 'DATA_READY'
            base['qualification_checks']={'continuous_15_minutes':continuous,'restart_verified':bool(restart),'momentum_history_ready':history_ready,'replay_verified':base.get('replay',{}).get('ok',False)}
        base.update(reasons=sorted(set(reasons)),collection_enabled=self.collection_enabled,research='INSUFFICIENT_FORWARD_DATA',
                    profitability_claim=False,automatic_promotion=False,
                    providers=[{'name':s.name,'vendor':s.vendor,'channel':s.channel,
                        'approval':'APPROVED_CONFIGURATION_PENDING_LIVE_VALIDATION' if s.approved_at(now) else 'UNAPPROVED',
                        'credential_reference':s.secret_env} for s in self.native_specs],
                    provider_configuration='CONFIGURED_CANDIDATES' if self.native_specs else 'UNCONFIGURED')
        base['forex_factory']={'enabled':self.secondary.config.enabled,'role':'SECONDARY_CROSS_CHECK_ONLY',
            'usage_status':'VALIDATED_CONFIGURATION' if self.secondary.config.usage_validated else 'UNVALIDATED','can_establish_live':False}
        return base
