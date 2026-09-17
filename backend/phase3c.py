"""Isolated adaptive companion to the immutable Phase 3B champion."""
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import json
import logging

from .domain import INTERVALS,canonical,digest,parse,stamp,freshness_errors
from .storage import Store
from .phase3b import Phase3BEngine
from .meta_council import BASELINE,MetaPolicy,DemoCosts,evaluate_meta,regime_key
from .meta_evaluation import performance,promotion_report
from . import meta_journal as journal

BASE_COMMIT="6ec0099057eac4134abe3248900499b8f3db9a0a"
BASE_HASH="b6859153bbabb313754dfd6bb1ec9b7528b264cf67f696025ca8b3db9b462dc9"
SOURCE_FILES=("meta_council.py","meta_evaluation.py","meta_journal.py","phase3c.py")


def implementation_hash(root=None):
    root=Path(root) if root else Path(__file__).parent
    return digest({k:(root/k).read_text(encoding="utf-8") for k in SOURCE_FILES})


def feature_start(snapshot):
    now=parse(snapshot["observed_at"])
    starts=[]
    for interval,seconds in INTERVALS.items():
        opened=[]
        for row in snapshot["frames"].get(interval,[]):
            try:
                at=parse(row["t"])
                if at+timedelta(seconds=seconds)<=now:
                    opened.append(at)
            except (KeyError,TypeError,ValueError):
                continue
        starts.extend(sorted(opened)[-120:])
    return min(starts) if starts else now


class Phase3CEngine(Phase3BEngine):
    # Do NOT override champion strategy_version, code_hash, model_identity or evaluation.
    def __init__(self,store,*args,meta_policy=MetaPolicy(),costs=DemoCosts(),meta_enabled=True,**kwargs):
        with store.connect() as conn:
            existing=conn.execute("SELECT 1 FROM decisions LIMIT 1").fetchone()
            companion=conn.execute("SELECT 1 FROM sqlite_master WHERE name='meta_decisions'").fetchone()
            if existing and not companion:
                raise RuntimeError("Phase 3C requires a separate database; immutable base history preserved")
        super().__init__(store,*args,**kwargs)
        if self.code_hash!=BASE_HASH:
            raise RuntimeError("Immutable Phase 3B source identity mismatch")
        if type(meta_enabled) is not bool:
            raise ValueError("Invalid meta flag")
        self.meta_policy,self.costs,self.meta_enabled=meta_policy,costs,meta_enabled
        self.meta_hash=implementation_hash()
        self.meta_identity=digest({"version":"phase3c-1","code":self.meta_hash,"base":BASE_HASH,
                                   "policy":asdict(meta_policy),"costs":asdict(costs)})
        with store.connect() as conn:
            conn.executescript(journal.SCHEMA)

    def namespace(self,champion):
        return digest([self.meta_identity,champion["model_identity"]])

    def _verified_decision(self,row):
        inputs,result=json.loads(row[0]),json.loads(row[1])
        if inputs["code_hash"]!=self.meta_hash or digest({"inputs":inputs,"result":result})!=row[2]:
            raise ValueError("Meta decision integrity mismatch")
        if evaluate_meta(inputs)!=result:
            raise ValueError("Meta replay mismatch")
        return inputs,result

    def _inputs(self,conn,snapshot,champion,now,errors):
        namespace=self.namespace(champion)
        key=regime_key(champion["analytics_context"])
        history={}
        # Select after temporal eligibility, so recent purged trades cannot evict
        # the older training window from a bounded query.
        for cutoff in (feature_start(snapshot),now):
            cutoff-=timedelta(seconds=self.meta_policy.embargo_seconds)
            for row in conn.execute("""SELECT payload,checksum FROM meta_outcomes
                    WHERE namespace=? AND known_at<?
                    ORDER BY known_at DESC,id DESC LIMIT ?""",
                    (namespace,stamp(cutoff),self.meta_policy.history_limit)):
                try:
                    sample=journal.checked_payload(*row)
                    history[sample["id"]]=sample
                except (ValueError,TypeError,KeyError):
                    errors.append("CORRUPT_HISTORY")
        previous=dict(BASELINE)
        memory={}
        for kind,extra,params in (("global","",()),("regime"," AND regime_key=?",(key,))):
            row=conn.execute("SELECT inputs,result,checksum FROM meta_decisions WHERE namespace=? AND at<?"+extra+" ORDER BY at DESC LIMIT 1",
                             (namespace,stamp(now),*params)).fetchone()
            if row:
                try:
                    _,prior=self._verified_decision(row)
                    if kind=="global":
                        memory=prior["transition_memory_after"]
                    else:
                        previous=prior["weights_before_decision"]
                except (ValueError,TypeError,KeyError):
                    errors.append("REPLAY_MISMATCH")
        if conn.execute("SELECT 1 FROM meta_control WHERE key='replay_block'").fetchone():
            errors.append("REPLAY_MISMATCH")
        return {"at":stamp(now),"feature_start":stamp(feature_start(snapshot)),"namespace":namespace,
                "code_hash":self.meta_hash,"base_commit":BASE_COMMIT,"base_hash":BASE_HASH,
                "champion":champion,"history":sorted(history.values(),key=lambda r:(r["known_at"],r["id"])),"weights_previous":previous,
                "transition_memory":memory,"policy":asdict(self.meta_policy),"costs":asdict(self.costs),
                "integrity_errors":sorted(set(errors))}

    def after_tick(self,conn,snapshot,result,now,snapshot_id):
        super().after_tick(conn,snapshot,result,now,snapshot_id)
        if not self.meta_enabled:
            return
        errors=[]
        # Failure in learning or hypothetical execution cannot change champion writes.
        conn.execute("SAVEPOINT meta_collection")
        try:
            journal.monitor_positions(conn,snapshot,now,snapshot_id,self.costs)
            if result.get("decision_id"):
                journal.ingest_base_outcomes(conn,self.namespace(result),result["model_identity"],self.costs,now)
        except Exception:
            conn.execute("ROLLBACK TO meta_collection")
            errors.append("CORRUPT_HISTORY")
            logging.getLogger("dardania.meta").error("META_HISTORY_OR_EXECUTION_FAILED")
        finally:
            conn.execute("RELEASE meta_collection")
        if not result.get("decision_id"):
            if errors:
                conn.execute("INSERT OR REPLACE INTO meta_control VALUES ('health',?)",(canonical({"fallback":True,"reasons":errors,"at":stamp(now)}),))
            return
        conn.execute("SAVEPOINT meta_decision_work")
        try:
            inputs=self._inputs(conn,snapshot,result,now,errors)
            meta=evaluate_meta(inputs)
            conn.execute("INSERT INTO meta_decisions VALUES (?,?,?,?,?,?,?)",
                         (result["decision_id"],stamp(now),inputs["namespace"],regime_key(result["analytics_context"]),
                          canonical(inputs),canonical(meta),digest({"inputs":inputs,"result":meta})))
            journal.create_position(conn,meta,snapshot,now,snapshot_id)
            conn.execute("INSERT OR REPLACE INTO meta_control VALUES ('health',?)",
                         (canonical({"fallback":meta["routing"]["fallback"],"reasons":meta["challenger"]["vetoes"],"at":stamp(now)}),))
        except Exception:
            conn.execute("ROLLBACK TO meta_decision_work")
            conn.execute("INSERT OR REPLACE INTO meta_control VALUES ('health',?)",
                         (canonical({"fallback":True,"reasons":["META_JOURNAL_FAILURE"],"at":stamp(now)}),))
            logging.getLogger("dardania.meta").error("META_DECISION_FAILED")
        finally:
            conn.execute("RELEASE meta_decision_work")

    def replay_meta(self,decision_id):
        with self.store.connect() as conn:
            row=conn.execute("SELECT inputs,result,checksum FROM meta_decisions WHERE id=?",(decision_id,)).fetchone()
        if not row:
            raise KeyError(decision_id)
        try:
            _,result=self._verified_decision(row)
            return {"decision_id":decision_id,"matches":True,"result":result}
        except (ValueError,TypeError,KeyError):
            with self.store.transaction() as conn:
                conn.execute("INSERT OR REPLACE INTO meta_control VALUES ('replay_block',?)",(canonical({"reason":"REPLAY_MISMATCH","decision_id":decision_id}),))
            return {"decision_id":decision_id,"matches":False,"fallback":"PHASE3B_CHAMPION","reason":"REPLAY_MISMATCH"}

    def latest_meta(self,now=None):
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            row=conn.execute("SELECT inputs,result,checksum FROM meta_decisions ORDER BY at DESC LIMIT 1").fetchone()
            health=conn.execute("SELECT payload FROM meta_control WHERE key='health'").fetchone()
            blocked=bool(conn.execute("SELECT 1 FROM meta_control WHERE key='replay_block'").fetchone())
            champion_health=Store.get_state(conn,"health",{})
            response_guards=[]
            if now is not None and row:
                try:
                    health_view=self.api_health(conn,dict(champion_health,data_errors=list(champion_health.get('data_errors',[]))),now)
                    response_guards.extend(health_view.get('data_errors',[]))
                    decision_view=self.api_decision(conn,json.loads(row[1])['champion'],now)
                    response_guards.extend(decision_view.get('veto_codes',[]))
                    observed=champion_health.get('observed_at')
                    if not observed or not 0<=(now-parse(observed)).total_seconds()<=self.policy.heartbeat_limit_seconds:
                        response_guards.append('MONITOR_STALE')
                except (ValueError,TypeError,KeyError):
                    response_guards.append('RESPONSE_DATA_UNAVAILABLE')
        if not row:
            return {"routing":{"active":"PHASE3B_CHAMPION","fallback":True},"health":json.loads(health[0]) if health else {"reasons":["WAITING_FOR_META_DECISION"]}}
        try:
            _,result=self._verified_decision(row)
            view=dict(result,health=json.loads(health[0]) if health else None,decision_is_historical=True)
            guards=list(response_guards)
            if blocked:
                guards.append("REPLAY_MISMATCH")
            if view["health"] and view["health"]["fallback"] and view["health"]["at"]>=result["at"]:
                guards.extend(view["health"]["reasons"])
            if now is not None:
                guards.extend(freshness_errors(champion_health.get("source_candle_closes",{}),now,self.policy))
                guards.extend(champion_health.get("data_errors",[]))
                guards.extend(champion_health.get("risk_codes",[]))
                if result["champion"].get("expires_at") and now>=parse(result["champion"]["expires_at"]):
                    guards.append("SIGNAL_EXPIRED")
            if guards:
                view["routing"]=dict(result["routing"],fallback=True)
                view["challenger"]=dict(result["challenger"],direction="NO_TRADE",vetoes=sorted(set(result["challenger"]["vetoes"]+guards)))
                view["adaptive_influence"]=0.0
            return view
        except (ValueError,TypeError,KeyError):
            return {"routing":{"active":"PHASE3B_CHAMPION","fallback":True},"vetoes":["REPLAY_MISMATCH"]}

    def meta_analytics(self,now):
        with self.store.connect() as conn:
            conn.execute("BEGIN")
            latest=conn.execute("SELECT namespace,inputs,result,checksum FROM meta_decisions ORDER BY at DESC LIMIT 1").fetchone()
            if not latest:
                return {"cohorts":{},"promotion":promotion_report([])}
            rows=[]
            integrity_ok=not conn.execute("SELECT 1 FROM meta_control WHERE key='replay_block'").fetchone()
            guards=[]
            try:
                _,decision=self._verified_decision(latest[1:])
                if any(r['status']=='DRIFT' for r in decision['drift'].values()):
                    guards.append('ACTIVE_DRIFT')
            except (ValueError,TypeError,KeyError):
                integrity_ok=False
            health=conn.execute("SELECT payload FROM meta_control WHERE key='health'").fetchone()
            if health and json.loads(health[0]).get('fallback'):
                guards.append('ACTIVE_META_FALLBACK')
            for row in conn.execute("SELECT payload,checksum FROM meta_outcomes WHERE namespace=? AND known_at<=?",(latest[0],stamp(now))):
                try:
                    rows.append(journal.checked_payload(*row))
                except (ValueError,TypeError):
                    integrity_ok=False
        return {"cohorts":{source:performance([r for r in rows if r["source"]==source]) for source in
                           ("CHAMPION","ADAPTIVE_CHALLENGER","TREND_CONTINUATION","SWEEP_RECLAIM","RESILIENCE_DIVERGENCE")},
                "promotion":promotion_report(rows,integrity_ok,guards),"cost_assumptions":asdict(self.costs),
                "cost_kind":"CONFIGURED_DEMO_ASSUMPTIONS_NOT_LIVE_BROKER_COSTS"}
