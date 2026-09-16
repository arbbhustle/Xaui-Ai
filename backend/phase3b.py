"""Phase 3B gold intelligence: risk-only council integration and isolated shadows."""
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import json
import logging

from .council import CouncilPolicy
from .domain import Policy, apply_veto, canonical, digest, parse, stamp
from .intelligence import IntelligencePolicy
from .phase3a import Phase3AEngine, SOURCE_FILES as BASE_SOURCES, evaluate_phase3a
from .hidden_state import HiddenPolicy, analyze_hidden, macro_pressure
from .slow_context import SLOW_KEY, SlowPolicy, normalize_slow, assess_slow
from .gold_analytics import context, summarize
from . import gold_shadows
from .storage import Store

VERSION = "phase3b-1"
SOURCE_FILES = (*BASE_SOURCES, "slow_context.py", "hidden_state.py", "gold_shadows.py", "gold_analytics.py", "phase3b.py")


def implementation_hash(root=None):
    root = Path(root) if root is not None else Path(__file__).parent
    return digest({name:(root/name).read_text(encoding="utf-8") for name in SOURCE_FILES})


def evaluate_phase3b(snapshot, policy):
    result = evaluate_phase3a(snapshot,policy)
    hidden = analyze_hidden(snapshot,result,policy)
    slow = assess_slow(snapshot["slow_regime"],parse(snapshot["observed_at"]),SlowPolicy(**snapshot["slow_policy"]))
    vetoes = hidden["vetoes"] + slow["vetoes"]
    if slow["data_mode"] == "TEST_DATA" and snapshot["market_provenance"]["data_mode"] != "FIXTURE":
        vetoes.append("SLOW_TEST_DATA_IN_REAL_MARKET")
    result.update(strategy_version=VERSION,model_version="gold-hidden-council-v1",engine="DardaniaXAUTRADE AI Phase 3B",
                  hidden_state=hidden,slow_regime=slow,hidden_integration="RISK_FILTER_NO_SCORE_PROMOTION")
    result["analytics_context"] = context(result)
    result["reasons"] += hidden["reasons"]
    result["reasons"].append("Slow regime is background context only; it cannot promote a trade")
    if slow["vetoes"]:
        result["data_status"] = "SLOW_REGIME_BLOCKED"
    return apply_veto(result,vetoes)


class Phase3BEngine(Phase3AEngine):
    strategy_version = VERSION
    replay_fields = Phase3AEngine.replay_fields + ("hidden_state","slow_regime","hidden_integration","analytics_context")

    def __init__(self,store,policy=Policy(),council_policy=CouncilPolicy(),intelligence_policy=IntelligencePolicy(),
                 hidden_policy=HiddenPolicy(),slow_policy=SlowPolicy(),shadow_enabled=True):
        super().__init__(store,policy,council_policy,intelligence_policy)
        if type(shadow_enabled) is not bool:
            raise ValueError("Invalid shadow flag")
        self.hidden_policy,self.slow_policy,self.shadow_enabled = hidden_policy,slow_policy,shadow_enabled
        self.code_hash = implementation_hash()
        self.model_identity = digest({"code":self.code_hash,"policy":asdict(policy),"council":asdict(council_policy),
            "intelligence":asdict(intelligence_policy),"hidden":asdict(hidden_policy),"slow":asdict(slow_policy)})
        with store.connect() as conn:
            conn.executescript(gold_shadows.SCHEMA)

    def snapshot_context(self,conn,snapshot,checked):
        now = parse(snapshot["observed_at"])
        slow = normalize_slow(snapshot["frames"].pop(SLOW_KEY,{}),now)
        snapshot.update(slow_regime=slow,slow_policy=asdict(self.slow_policy),hidden_policy=asdict(self.hidden_policy),
                        model_namespace_extra={k:v["data_mode"] for k,v in slow.items()})
        previous = conn.execute("SELECT payload FROM decisions ORDER BY candle_close DESC LIMIT 3").fetchall()
        history = []
        for row in reversed(previous):
            decision = json.loads(row[0])
            hidden = decision["hidden_state"]
            if hidden["state"] and now-timedelta(hours=1) <= parse(decision["timestamp_utc"]) < now:
                history.append({"at":decision["timestamp_utc"],"candle_close":decision["signal_candle_close"],
                                "state":hidden["state"],"tension":hidden["tension"]["score"]})
        snapshot["hidden_history"] = history
        rows = conn.execute("SELECT payload,snapshot_id FROM phase3b_context WHERE at>=? AND at<? ORDER BY at DESC LIMIT 73",
                            (stamp(now-timedelta(hours=6)),stamp(now))).fetchall()
        snapshot["prior_macro"] = list(reversed([dict(json.loads(row[0]),snapshot_id=row[1]) for row in rows]))
        return super().snapshot_context(conn,snapshot,checked)

    def evaluate_snapshot(self,snapshot,policy):
        return evaluate_phase3b(snapshot,policy)

    def trade_metadata(self,result):
        return {**super().trade_metadata(result),"analytics_context":result["analytics_context"],
                "hidden_state_at_entry":result["hidden_state"]["state"]}

    def after_tick(self,conn,snapshot,result,now,snapshot_id):
        if self.shadow_enabled:
            conn.execute("SAVEPOINT phase3b_shadow_work")
            try:
                gold_shadows.monitor(conn,snapshot,snapshot_id,now)
                gold_shadows.create(conn,result,snapshot,snapshot_id,now)
            except Exception:
                conn.execute("ROLLBACK TO phase3b_shadow_work")
                Store.set_state(conn,"phase3b_shadow_health",{"status":"ERROR","at":stamp(now)})
                logging.getLogger("dardania.shadows").error("SHADOW_JOURNAL_FAILED")
            else:
                Store.set_state(conn,"phase3b_shadow_health",{"status":"OK","at":stamp(now)})
            finally:
                conn.execute("RELEASE phase3b_shadow_work")
        if result.get("decision_id"):
            payload = {"at":stamp(now),"pressure":macro_pressure(result["intelligence"])}
            conn.execute("INSERT OR IGNORE INTO phase3b_context VALUES (?,?,?)",(stamp(now),snapshot_id,canonical(payload)))

    def _slow_response(self,conn,snapshot_id,now):
        row = conn.execute("SELECT payload FROM snapshots WHERE id=?",(snapshot_id,)).fetchone()
        if not row:
            return {"vetoes":["MISSING_SLOW_SNAPSHOT"]}
        snapshot = json.loads(row[0])
        policy = dict(snapshot["slow_policy"])
        policy["allow_test_data"] &= self.slow_policy.allow_test_data
        view = assess_slow(snapshot["slow_regime"],now,SlowPolicy(**policy))
        if view["data_mode"] == "TEST_DATA" and snapshot["market_provenance"]["data_mode"] != "FIXTURE":
            view["vetoes"].append("SLOW_TEST_DATA_IN_REAL_MARKET")
        return view

    def api_health(self,conn,health,now):
        health = super().api_health(conn,health,now)
        view = self._slow_response(conn,health.get("snapshot_id"),now)
        health["slow_regime"] = view
        if view["vetoes"]:
            health["status"] = "DEGRADED"
            health["data_errors"] = sorted(set(health["data_errors"]+view["vetoes"]))
        return health

    def api_decision(self,conn,result,now):
        result = super().api_decision(conn,result,now)
        result["slow_regime_at_response"] = self._slow_response(conn,result.get("snapshot_id"),now)
        if result["slow_regime_at_response"]["vetoes"]:
            result["data_status"] = "SLOW_REGIME_BLOCKED"
        return apply_veto(result,result["slow_regime_at_response"]["vetoes"])

    def analytics(self):
        with self.store.connect() as conn:
            conn.execute("BEGIN")  # Both cohorts must describe the same committed instant.
            production = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM trades WHERE status='CLOSED'")]
            shadows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM phase3b_shadows WHERE status='CLOSED'")]
        return {"production_demo":summarize(production),"shadow_demo":{variant:summarize([r for r in shadows if r["variant"]==variant])
                for variant in gold_shadows.VARIANTS},"separation":"SHADOWS_EXCLUDED_FROM_PRODUCTION_CALIBRATION"}
