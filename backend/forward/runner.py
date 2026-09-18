"""Local-only forward DEMO orchestration over the unchanged Phase 3B/3C code."""
from dataclasses import asdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
import json,logging,os,threading

from ..domain import INTERVALS,apply_veto,canonical,digest,parse,stamp
from ..intelligence_providers import BUNDLE_KEY,MARKET_KEY,unavailable
from ..slow_context import SLOW_KEY
from ..phase3c import Phase3CEngine
from ..storage import Store
from ..worker import WorkerLock,seconds_to_next_tick
from .collection import Collector,control,set_control
from .contracts import CRITICAL,select,quality
from .store import ForwardStore

KEY='__forward__'


def implementation_hash():
    return digest({p.name:p.read_text(encoding='utf-8') for p in sorted(Path(__file__).parent.glob('*.py'))})


def inputs(selection,observations,now):
    chosen=selection['selected'];market=chosen.get('xau')
    # Missing higher frames cannot freeze exits with independently valid minute bars.
    if market is None and selection['health']['xau']['status']!='CONFLICTING':
        candidates=[quality(r,now) for r in observations if r['channel']=='xau' and r['data_mode']=='LIVE_DATA'
                    and not any(e in r['errors'] for e in ('OUT_OF_ORDER_OBSERVATION','REVISION_ID_REUSED','XAU_SOURCE_TIMESTAMP_MISMATCH'))]
        candidates=[r for r in candidates if r['freshness_seconds']>=0 and r['freshness_seconds']<150
                    and r['value'].get('1min') and 'INVALID_DATA:1min' not in r['errors'] and 'STALE_DATA:1min' not in r['errors']]
        # Partial feeds must still agree on the minute price before they can monitor exits.
        from .contracts import discrepancy
        if any(discrepancy(a,b) for i,a in enumerate(candidates) for b in candidates[i+1:]):candidates=[]
        if candidates:market=sorted(candidates,key=lambda r:r['provider'])[0]
    frames=dict(market['value']) if market else {tf:[] for tf in INTERVALS}
    bundle={}
    for channel,target in (('dxy','usd'),('calendar','calendar'),('news','news'),('fed','macro')):
        bundle[target]=chosen[channel]['value'] if channel in chosen else unavailable(target,now)
    if 'us2y' in chosen and 'us10y' in chosen:
        a,b=chosen['us2y']['value'],chosen['us10y']['value']
        bundle['yields']=dict(a,provider=a['provider']+'+'+b['provider'],records=a['records']+b['records'],
                              as_of=min(a['as_of'],b['as_of']),retrieved_at=max(a['retrieved_at'],b['retrieved_at']))
    else:bundle['yields']=unavailable('yields',now)
    frames[BUNDLE_KEY]=bundle
    frames[MARKET_KEY]={'provider':market['provider'] if market else 'unavailable','data_mode':'LIVE' if market else 'UNAVAILABLE',
                        'retrieved_at':market['received_at'] if market else stamp(now)}
    frames[SLOW_KEY]={c:chosen[c]['value'] for c in ('cot','etf','physical','options') if c in chosen}
    return frames,{} if market else {'1min':'NO_TRUSTWORTHY_MARKET_SOURCE'}


class ForwardEngine(Phase3CEngine):
    def __init__(self,store,configuration):
        super().__init__(store)
        self.forward_hash=implementation_hash()
        self.configuration=configuration
        self.model_identity=digest([self.model_identity,self.forward_hash,configuration])

    def snapshot_context(self,conn,snapshot,checked):
        metadata=snapshot['frames'].pop(KEY)
        if metadata['code_hash']!=self.forward_hash:raise ValueError('FORWARD_CODE_MISMATCH')
        snapshot['forward']=metadata
        return super().snapshot_context(conn,snapshot,checked)

    def evaluate_snapshot(self,snapshot,policy):
        if snapshot['forward']['code_hash']!=self.forward_hash:raise ValueError('FORWARD_REPLAY_CODE_MISMATCH')
        result=super().evaluate_snapshot(snapshot,policy)
        result=apply_veto(result,snapshot['forward']['vetoes'])
        result['forward']={'capture_id':snapshot['forward']['capture_id'],'risk_vetoes':snapshot['forward']['vetoes'],
                           'champion':'PHASE3B_CHAMPION','execution':'DEMO_ONLY'}
        return result

    def after_tick(self,conn,snapshot,result,now,snapshot_id):
        super().after_tick(conn,snapshot,result,now,snapshot_id)
        metadata=snapshot['forward'];cycle=metadata['cycle']
        meta=conn.execute('SELECT result FROM meta_decisions WHERE id=?',(result.get('decision_id'),)).fetchone()
        shadow_health=Store.get_state(conn,'phase3b_shadow_health',{})
        if result.get('decision_id') and not meta:
            set_control(conn,'companion_block',{'reason':'MISSING_CHALLENGER_EVIDENCE','at':stamp(now)})
        shadows=[json.loads(r[0]) for r in conn.execute('SELECT payload FROM phase3b_shadow_decisions WHERE decision_id=?',(result.get('decision_id'),))]
        self.store.event(conn,'decision:'+cycle,'DECISION',stamp(now),{
            'decision_id':result.get('decision_id') or digest(['monitor',cycle]),
            'engine_decision_id':result.get('decision_id'),'snapshot_id':snapshot_id,'capture_id':metadata['capture_id'],
            'champion':result,'challenger':json.loads(meta[0]) if meta else {'status':'NO_NEW_5M_DECISION'},
            'shadows':shadows,'shadow_health':shadow_health,'vetoes':result['veto_codes'],'timestamp':stamp(now),
            'promotion':'PROMOTION_INELIGIBLE','execution':'DEMO_ONLY'})
        # Append outcomes when first observable; never backfill old decision evidence.
        for table,source in (('trades','CHAMPION'),('phase3b_shadows','SHADOW'),('meta_positions','ADAPTIVE_CHALLENGER')):
            for row in conn.execute(f"SELECT id,payload FROM {table} WHERE status='CLOSED'"):
                key='outcome:'+source+':'+row[0]
                if conn.execute('SELECT 1 FROM forward_ledger WHERE event_key=?',(key,)).fetchone():continue
                trade=json.loads(row[1]);risk=trade.get('risk',abs(trade['entry']-trade['sl']))
                if parse(trade['closed_at'])>now:raise ValueError('FUTURE_OUTCOME')
                self.store.event(conn,key,'OUTCOME',stamp(now),{'source':source,'trade':trade,'known_at':stamp(now),
                    'gross_r':trade['r_multiple'],'simulated_net_r':self.costs.net(trade['r_multiple'],risk),
                    'cost_assumptions':asdict(self.costs),'cost_kind':'DEMO_ASSUMPTIONS_NOT_EXECUTABLE_QUOTES'})
        conn.execute("UPDATE forward_cycles SET status='COMPLETE' WHERE id=? AND status='CAPTURED'",(cycle,))
        set_control(conn,'checkpoint',{'cycle':cycle,'at':stamp(now),'snapshot_id':snapshot_id,'decision_id':result.get('decision_id')})


class ForwardRunner:
    def __init__(self,path,specs=(),providers=None,clock=lambda:datetime.now(timezone.utc)):
        if os.environ.get('RENDER'):raise RuntimeError('LOCAL_FORWARD_DEMO_ONLY')
        self.clock,self.specs=clock,tuple(specs)
        self.store=ForwardStore(path)
        self.store.verify()
        self.configuration=digest([asdict(s) for s in self.specs])
        self.engine=ForwardEngine(self.store,self.configuration)
        with self.store.connect() as conn:
            for table in ('decisions','trade_events','phase3b_shadow_decisions','phase3b_shadow_events','phase3b_context',
                          'meta_decisions','meta_outcomes','meta_position_events'):
                for operation in ('UPDATE','DELETE'):
                    conn.execute(f"CREATE TRIGGER IF NOT EXISTS immutable_{table}_{operation} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT,'immutable evidence'); END")
        self.collector=Collector(self.store,self.specs,providers,clock)
        self.lock=WorkerLock(self.store.path+'.forward.lock')
        self.stop=threading.Event()
        with self.store.transaction() as conn:
            identity={'configuration':self.configuration,'code':self.engine.forward_hash,'model':self.engine.model_identity}
            prior=control(conn,'identity')
            if prior and prior!=identity:raise ValueError('NEW_CONFIGURATION_REQUIRES_NEW_FORWARD_DATABASE')
            set_control(conn,'identity',identity)

    def once(self):
        # Separate handle: a concurrent call must not replace the scheduler's lock handle.
        with WorkerLock(self.store.path+'.forward.lock'):return self._once()

    def _recover(self):
        with self.store.connect() as conn:
            pending=conn.execute("SELECT id,capture_id FROM forward_cycles WHERE status='CAPTURED' ORDER BY observed_at").fetchall()
        for cycle,capture_id in pending:
            with self.store.connect() as conn:capture=self.store.get_capture(conn,capture_id)
            self._evaluate(cycle,capture_id,capture)

    def _evaluate(self,cycle,capture_id,capture):
        now=parse(capture['observed_at'])
        frames,errors=inputs(capture['selection'],capture['observations'],now)
        frames[KEY]={'cycle':cycle,'capture_id':capture_id,'code_hash':self.engine.forward_hash,'vetoes':capture['vetoes']}
        result=self.engine.tick(frames,now,errors)
        if result is None:raise ValueError('UNCHECKPOINTED_ENGINE_STATE')
        self._verify_replay()
        return result

    def _verify_replay(self):
        with self.store.connect() as conn:
            row=conn.execute('SELECT id FROM decisions ORDER BY candle_close DESC LIMIT 1').fetchone()
        champion_ok=False;challenger_ok=False
        try:
            self.store.verify(full=False)
            if row:champion_ok=self.engine.replay(row[0])['matches']
        except Exception:
            with self.store.transaction() as conn:set_control(conn,'integrity_block',{'reason':'REPLAY_OR_STORAGE_FAILURE'})
        if row:
            try:challenger_ok=self.engine.replay_meta(row[0])['matches']
            except Exception:pass
        with self.store.transaction() as conn:
            set_control(conn,'replay',{'ok':bool(champion_ok and challenger_ok),'champion_ok':bool(champion_ok),
                'challenger_ok':bool(challenger_ok),'decision_id':row[0] if row else None})
            if row and not challenger_ok:set_control(conn,'companion_block',{'reason':'CHALLENGER_REPLAY_FAILURE'})
        if row and not champion_ok:
            with self.store.transaction() as conn:set_control(conn,'integrity_block',{'reason':'REPLAY_OR_STORAGE_FAILURE'})
        return champion_ok and challenger_ok

    def _once(self):
        self.store.verify(full=False)
        self._recover()
        started=self.clock();bucket=started.replace(second=0,microsecond=0)
        # parse enforces timezone awareness even for an injected clock.
        if started.tzinfo is None or started.utcoffset() is None:raise ValueError('NAIVE_CLOCK')
        cycle=digest([self.configuration,stamp(bucket)])
        with self.store.transaction() as conn:
            checkpoint=control(conn,'checkpoint',{})
            if checkpoint.get('at') and started<parse(checkpoint['at']):raise ValueError('CLOCK_REVERSED')
            if conn.execute('SELECT 1 FROM forward_cycles WHERE id=?',(cycle,)).fetchone():return {'status':'DUPLICATE_CYCLE'}
            lost=conn.execute("""SELECT q.cycle,min(q.at) AS started FROM forward_requests q
                LEFT JOIN forward_cycles c ON c.id=q.cycle WHERE c.id IS NULL AND q.at<?
                AND NOT EXISTS(SELECT 1 FROM forward_ledger l WHERE l.event_key='acquisition-gap:'||q.cycle)
                GROUP BY q.cycle""",(stamp(bucket),)).fetchall()
            for orphan in lost:
                self.store.event(conn,'acquisition-gap:'+orphan['cycle'],'ACQUISITION_GAP',stamp(started),
                    {'cycle':orphan['cycle'],'started_at':orphan['started'],'reason':'INTERRUPTED_BEFORE_CAPTURE_COMMIT',
                     'handling':'RECEIPTS_PRESERVED_NO_BACKDATED_DECISION'})
            if checkpoint and bucket-parse(checkpoint['at']).replace(second=0,microsecond=0)>timedelta(minutes=1):
                self.store.event(conn,'gap:'+cycle,'MONITOR_GAP',stamp(started),{'after':checkpoint['at'],'resumed_at':stamp(started),
                    'reason':'MISSED_SCHEDULE_NO_SYNTHETIC_BACKFILL'})
            previous=control(conn,'selected',{})
        observations,audits=self.collector.collect(cycle,started)
        now=self.clock()
        if now<started:raise ValueError('CLOCK_REVERSED')
        selection=select(self.specs,observations,now,previous)
        with self.store.transaction() as conn:
            vetoes=list(selection['vetoes'])
            if not control(conn,'replay',{}).get('champion_ok'):vetoes.append('BOOTSTRAP_REPLAY_REQUIRED')
            if control(conn,'integrity_block'):vetoes.append('FORWARD_INTEGRITY_BLOCK')
            capture={'observed_at':stamp(now),'started_at':stamp(started),'observations':observations,
                     'provider_audit':audits,'selection':selection,'vetoes':sorted(set(vetoes))}
            capture_id=self.store.put_capture(conn,capture)
            conn.execute('INSERT INTO forward_cycles VALUES (?,?,?,?,?)',(cycle,stamp(bucket),'CAPTURED',capture_id,stamp(now)))
            self.store.event(conn,'capture:'+cycle,'CAPTURE',stamp(now),{'capture_id':capture_id,'provider_audit':audits,
                'switches':selection['switches'],'vetoes':capture['vetoes']})
            set_control(conn,'selected',{c:r['provider'] for c,r in selection['selected'].items()})
        return self._evaluate(cycle,capture_id,capture)

    def status(self,now=None):
        now=now or self.clock()
        try:
            storage=self.store.verify()
            with self.store.connect() as conn:
                row=conn.execute("SELECT capture_id FROM forward_cycles WHERE status='COMPLETE' ORDER BY observed_at DESC LIMIT 1").fetchone()
                capture=self.store.get_capture(conn,row[0]) if row else None
                replay=control(conn,'replay',{})
                blocked=control(conn,'integrity_block')
                companion_block=control(conn,'companion_block')
                shadow_health=Store.get_state(conn,'phase3b_shadow_health',{})
                checkpoint=control(conn,'checkpoint',{})
                pending=conn.execute("SELECT 1 FROM forward_cycles WHERE status!='COMPLETE'").fetchone()
            selection=select(self.specs,capture['observations'] if capture else [],now)
            reasons=list(selection['vetoes'])
            if blocked:reasons.append('FORWARD_INTEGRITY_BLOCK')
            if companion_block:reasons.append('CHALLENGER_EVIDENCE_DEGRADED')
            if shadow_health.get('status')=='ERROR':reasons.append('SHADOW_JOURNAL_FAILURE')
            if pending:reasons.append('PENDING_RECOVERY')
            age=(now-parse(checkpoint['at'])).total_seconds() if checkpoint else float('inf')
            if not 0<=age<150:reasons.append('MONITOR_STALE_OR_CLOCK_INVALID')
            state='NOT_READY' if reasons else 'FORWARD_DEMO_READY' if replay.get('ok') else 'DATA_READY'
            return {'status':state,'reasons':sorted(set(reasons)),'health':selection['health'],**storage,
                    'replay':replay,'champion':'PHASE3B_CHAMPION','challenger':'ISOLATED_DEMO_ONLY_NO_AUTO_PROMOTION',
                    'promotion':'PROMOTION_INELIGIBLE','research':'INSUFFICIENT_EVIDENCE'}
        except Exception:return {'status':'NOT_READY','reasons':['STORAGE_OR_EVIDENCE_INTEGRITY_FAILURE']}

    def run(self):
        with self.lock:
            while not self.stop.is_set():
                try:self._once()
                except Exception:logging.getLogger('dardania.forward').error('FORWARD_CYCLE_FAILED')
                self.stop.wait(seconds_to_next_tick(self.clock()))
