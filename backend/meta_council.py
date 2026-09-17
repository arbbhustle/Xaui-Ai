"""Causal Phase 3C learning. Pure functions; all scores except calibration are indices."""
from dataclasses import asdict, dataclass
from datetime import timedelta
import math
from statistics import mean

from .domain import digest, parse, stamp
from .hidden_state import STATES

VERSION = "phase3c-1"
BASELINE = {"technical":.22,"momentum":.12,"market_structure":.12,"volatility":.06,
            "multi_timeframe_alignment":.14,"macro":.08,"news":.04,"gold_resilience":.04,
            "dgfe":.04,"liquidity":.03,"entropy":.03,"event_absorption":.02,
            "TREND_CONTINUATION":.02,"SWEEP_RECLAIM":.02,"RESILIENCE_DIVERGENCE":.02}
SHADOWS = ("TREND_CONTINUATION","SWEEP_RECLAIM","RESILIENCE_DIVERGENCE")
DIMENSIONS = ("hidden_state","session","volatility_regime","macro_regime","timeframe_alignment","direction")


@dataclass(frozen=True)
class MetaPolicy:
    min_samples: int = 30
    calibration_samples: int = 60
    shrinkage: int = 20
    embargo_seconds: int = 3600
    max_weight: float = .35
    max_weight_step: float = .005
    max_influence: float = .25
    max_uncertainty: float = 70
    min_confidence: float = .55
    drift_recent: int = 20
    drift_baseline: int = 60
    drift_drop_r: float = .5
    history_limit: int = 2000

    def __post_init__(self):
        for k,v in asdict(self).items():
            if type(v) not in (int,float) or not math.isfinite(v):
                raise ValueError("Invalid meta policy")
        for k in ("min_samples","calibration_samples","shrinkage","embargo_seconds","drift_recent","drift_baseline","history_limit"):
            if type(getattr(self,k)) is not int:
                raise ValueError("Invalid meta policy integer")
        if not (20 <= self.min_samples <= 500 and self.min_samples <= self.calibration_samples <= 1000 and
                10 <= self.shrinkage <= 1000 and 0 <= self.embargo_seconds <= 86400 and
                .22 <= self.max_weight <= .4 and 0 < self.max_weight_step <= .01 and
                0 < self.max_influence <= .25 and 40 <= self.max_uncertainty <= 80 and .5 <= self.min_confidence <= .9 and
                10 <= self.drift_recent <= 100 and self.drift_baseline >= 3*self.drift_recent and
                .1 <= self.drift_drop_r <= 3 and 200 <= self.history_limit <= 10000):
            raise ValueError("Unsafe meta policy bounds")


@dataclass(frozen=True)
class DemoCosts:
    spread_points: float = .30
    slippage_points_per_side: float = .10
    latency_penalty_points: float = 0.0

    def __post_init__(self):
        if any(type(v) not in (int,float) or not math.isfinite(v) or not 0 <= v <= 100 for v in asdict(self).values()):
            raise ValueError("Invalid simulated cost assumption")

    def net(self,gross_r,risk):
        if not math.isfinite(gross_r) or not math.isfinite(risk) or risk <= 0:
            raise ValueError("Invalid R accounting")
        return round(gross_r-(self.spread_points+2*self.slippage_points_per_side+self.latency_penalty_points)/risk,8)


def regime_key(regime):
    return digest({k:regime[k] for k in DIMENSIONS})


def votes(champion):
    side = 1 if champion.get("candidate_direction") == "BUY" else -1 if champion.get("candidate_direction") == "SELL" else 0
    result = {k:None for k in BASELINE}
    for k,c in champion.get("components",{}).items():
        if k in BASELINE:
            result[k] = side*c["buy"] if k == "volatility" else c["buy"]-c["sell"]
    h = champion.get("hidden_state",{})
    result.update(macro=h.get("resilience",{}).get("expected_pressure"),
                  news=champion.get("intelligence_components",{}).get("news_sentiment_score"),
                  gold_resilience=h.get("resilience",{}).get("gold_resilience_score"),
                  liquidity=h.get("dgfe",{}).get("liquidity_pressure"))
    if h.get("dgfe"):
        result["dgfe"] = h["dgfe"]["directional_pressure"]*h["dgfe"]["fracture_score"]/100
        result["TREND_CONTINUATION"] = h["dgfe"]["directional_pressure"]
        result["SWEEP_RECLAIM"] = h["dgfe"]["liquidity_pressure"]
        result["RESILIENCE_DIVERGENCE"] = result["gold_resilience"]
    if h.get("entropy"):
        result["entropy"] = side*(100-h["entropy"]["score"])
    events = [e for e in h.get("event_absorption",[]) if not e["unresolved"] and e.get("follow_through_atr") is not None]
    if events:
        result["event_absorption"] = max(-100,min(100,mean(e["follow_through_atr"] for e in events)*25))
    return {k:round(v,8) if v is not None else None for k,v in result.items()}


def independent(samples):
    """Greedy, deterministic non-overlapping closed intervals; no duplicated exposure."""
    selected=[]
    end=None
    for row in sorted(samples,key=lambda r:(parse(r["closed_at"]),parse(r["opened_at"]),r["id"])):
        if end is None or parse(row["opened_at"]) > end:
            selected.append(row)
            end=parse(row["closed_at"])
    return selected


def select_history(samples,at,feature_start,policy,namespace,decision_id):
    cutoff = feature_start-timedelta(seconds=policy.embargo_seconds)
    guard_cutoff = at-timedelta(seconds=policy.embargo_seconds)
    learning,guard,errors,seen=[],[],[],set()
    for row in samples:
        try:
            # Do not even inspect a future outcome's value or regime.
            if row.get("status") != "CLOSED" or parse(row["known_at"]) >= at:
                continue
            if row["namespace"] != namespace or row["decision_id"] == decision_id:
                continue
            opened,closed,known = (parse(row[k]) for k in ("opened_at","closed_at","known_at"))
            if closed >= at:
                continue
            if not opened < closed <= known or row["id"] in seen:
                raise ValueError("Invalid historical chronology")
            seen.add(row["id"])
            if row["source"] not in ("CHAMPION","ADAPTIVE_CHALLENGER",*SHADOWS):
                raise ValueError("Unknown outcome source")
            if any(type(row[k]) not in (int,float) or not math.isfinite(row[k]) for k in ("gross_r","net_r","raw_score")):
                raise ValueError("Invalid outcome")
            if not 0 <= row["raw_score"] <= 100 or row["regime"]["direction"] not in ("BUY","SELL"):
                raise ValueError("Invalid historical regime")
            if any(not isinstance(row["regime"][k],str) or not 0<len(row["regime"][k])<=200 for k in DIMENSIONS):
                raise ValueError("Invalid regime label")
            for k,v in row.get("votes",{}).items():
                if k not in BASELINE or (v is not None and (type(v) not in (int,float) or not math.isfinite(v) or not -100<=v<=100)):
                    raise ValueError("Invalid component evidence")
            if row.get("prediction") is not None and (type(row["prediction"]) not in (int,float) or not math.isfinite(row["prediction"]) or not 0<=row["prediction"]<=1):
                raise ValueError("Invalid historical probability")
            if "cost_assumptions" in row and abs(DemoCosts(**row["cost_assumptions"]).net(row["gross_r"],row["risk_points"])-row["net_r"])>1e-7:
                raise ValueError("Inconsistent net R")
            regime_key(row["regime"])
            if closed < guard_cutoff and known < guard_cutoff:
                guard.append(row)
            if closed < cutoff and known < cutoff:
                learning.append(row)
        except (ValueError,KeyError,TypeError,AttributeError,OverflowError):
            errors.append("CORRUPT_HISTORY")
    order=lambda r:(parse(r["known_at"]),r["id"])
    return sorted(learning,key=order)[-policy.history_limit:],sorted(guard,key=order)[-policy.history_limit:],sorted(set(errors))


def trust(samples,regime,policy):
    exact=[s for s in samples if regime_key(s["regime"]) == regime_key(regime)]
    result={}
    for component in BASELINE:
        if component in SHADOWS:
            selected=independent([s for s in exact if s["source"] == component])
        else:
            # Do not invent the return of a counterfactual opposite trade.
            selected=independent([s for s in exact if s["source"] in ("CHAMPION","ADAPTIVE_CHALLENGER") and
                s.get("votes",{}).get(component) is not None and
                s["votes"][component]*(1 if s["regime"]["direction"] == "BUY" else -1) >= 20])
        n=len(selected)
        wins=sum(s["net_r"] > 0 for s in selected)
        smoothed=(wins+policy.shrinkage*.5)/(n+policy.shrinkage)
        strength=(2*smoothed-1) if n >= policy.min_samples else 0.0
        expectancy=mean(s['net_r'] for s in selected) if n else None
        # Frequent small wins cannot earn extra trust while losing money overall.
        if n >= policy.min_samples and expectancy <= 0:
            strength=min(strength,0.0)
        result[component]={"sample_count":n,"effective_sample_size":n,"trust_index":round(100*smoothed,6),
                           "strength":round(strength,8),"eligible":n >= policy.min_samples,
                           "net_expectancy":expectancy,
                           "sample_ids":[s["id"] for s in selected]}
    return result,exact


def bounded_weights(target,previous,policy):
    if set(previous) != set(BASELINE) or any(type(v) not in (int,float) or not math.isfinite(v) or v < 0 or v > policy.max_weight for v in previous.values()) or abs(sum(previous.values())-1) > 1e-7:
        raise ValueError("Corrupt prior weights")
    low={k:max(0,previous[k]-policy.max_weight_step) for k in BASELINE}
    high={k:min(policy.max_weight,previous[k]+policy.max_weight_step) for k in BASELINE}
    left,right=-2.0,2.0
    for _ in range(80):
        offset=(left+right)/2
        total=sum(max(low[k],min(high[k],target[k]+offset)) for k in BASELINE)
        if total > 1:
            right=offset
        else:
            left=offset
    return {k:round(max(low[k],min(high[k],target[k]+(left+right)/2)),12) for k in BASELINE}


def calibration(samples,regime,raw_score,policy):
    def candidates(source):
        return independent([s for s in samples if s["source"] == source and s["regime"]["direction"] == regime["direction"] and
                            min(4,int(s["raw_score"]//20)) == min(4,int(raw_score//20))])
    chosen=candidates("ADAPTIVE_CHALLENGER")
    source="CHALLENGER"
    if len(chosen) < policy.calibration_samples:
        chosen=candidates("CHAMPION")
        source="CHAMPION_PROXY"
    exact=[s for s in chosen if regime_key(s["regime"]) == regime_key(regime)]
    if len(exact) >= policy.calibration_samples:
        chosen=exact
        source+="_EXACT_REGIME"
    else:
        source+="_POOLED_DIRECTION_SCORE_BIN"
    n=len(chosen)
    wins=sum(s["net_r"] > 0 for s in chosen)
    return {"status":"READY" if n >= policy.calibration_samples else "INSUFFICIENT_HISTORY",
            "probability":round((wins+2)/(n+4),8) if n >= policy.calibration_samples else None,
            "source":source,"sample_count":n,"wins":wins,"prior_alpha":2,"prior_beta":2,
            "sample_ids":[s["id"] for s in chosen],"target":"POSITIVE_NET_SIMULATED_R",
            "note":"Historical estimate; champion proxy is not validated challenger confidence"}


def drift(samples,policy):
    report={}
    cohorts={source:[r for r in samples if r["source"]==source] for source in ("CHAMPION","ADAPTIVE_CHALLENGER",*SHADOWS)}
    for source,rows in list(cohorts.items()):
        for key in sorted({regime_key(r["regime"]) for r in rows}):
            cohorts[source+":"+key]=[r for r in rows if regime_key(r["regime"])==key]
    for key,rows in cohorts.items():
        rows=independent(rows)
        enough=len(rows) >= policy.drift_recent+policy.drift_baseline
        recent=rows[-policy.drift_recent:] if enough else []
        baseline=rows[-policy.drift_recent-policy.drift_baseline:-policy.drift_recent] if enough else []
        r=mean(x["net_r"] for x in recent) if recent else None
        b=mean(x["net_r"] for x in baseline) if baseline else None
        report[key]={"status":"DRIFT" if enough and r < 0 and b-r >= policy.drift_drop_r else "STABLE" if enough else "INSUFFICIENT_HISTORY",
                     "recent_net_r":r,"baseline_net_r":b,"sample_ids":[x["id"] for x in baseline+recent]}
    return report


def transition(memory,current):
    counts=dict(memory.get("counts",{}))
    previous=memory.get("previous_state")
    if previous is not None and previous not in STATES or any(k not in {a+">"+b for a in STATES for b in STATES} or type(v) is not int or v < 0 for k,v in counts.items()):
        raise ValueError("Corrupt transition memory")
    outgoing=sum(v for k,v in counts.items() if k.startswith(str(previous)+">"))
    key=str(previous)+">"+str(current)
    n=counts.get(key,0)
    report={"previous_state":previous,"current_state":current,"transition_count":n,
            "transition_reliability":outgoing/(outgoing+20),"prior_outgoing_count":outgoing,
            "observed_frequency":n/outgoing if outgoing else None,"meaning":"PRIOR_OBSERVED_FREQUENCY_NOT_PREDICTION"}
    if previous in STATES and current in STATES:
        counts[key]=n+1
    return report,{"previous_state":current if current in STATES else None,"counts":counts}


def evaluate_meta(inputs):
    policy=MetaPolicy(**inputs["policy"])
    at=parse(inputs["at"])
    champion=inputs["champion"]
    regime=champion["analytics_context"]
    learning,guard,errors=select_history(inputs["history"],at,parse(inputs["feature_start"]),policy,inputs["namespace"],champion["decision_id"])
    errors+=inputs.get("integrity_errors",[])
    if parse(inputs["feature_start"])>at:
        errors.append("INVALID_FEATURE_WINDOW")
    trust_report,exact=trust(learning,regime,policy)
    drift_report=drift(guard,policy)
    drifted=any(r["status"]=="DRIFT" for r in drift_report.values())
    previous=inputs["weights_previous"]
    target={k:v*(1+.5*trust_report[k]["strength"]) for k,v in BASELINE.items()}
    if drifted or errors:
        target=dict(BASELINE)
    try:
        weights=bounded_weights(target,previous,policy)
    except (ValueError,KeyError,TypeError):
        weights=dict(BASELINE)
        errors.append("CORRUPT_WEIGHT_HISTORY")
    try:
        transitions,memory_after=transition(inputs["transition_memory"],regime["hidden_state"])
    except (ValueError,TypeError,AttributeError):
        transitions,memory_after=transition({},regime["hidden_state"])
        errors.append("CORRUPT_TRANSITION_HISTORY")
    signals=votes(champion)
    for name in SHADOWS:
        if not trust_report[name]["eligible"]:
            signals[name]=None
    coverage=sum(weights[k] for k,v in signals.items() if v is not None)
    signed=sum(weights[k]*v for k,v in signals.items() if v is not None)
    magnitude=sum(weights[k]*abs(v) for k,v in signals.items() if v is not None)
    disagreement=100*(1-abs(signed)/magnitude) if magnitude > 1e-9 else 100.0
    ess=len(independent(exact))
    uncertainty=max(100*(1-min(ess/policy.min_samples,1)),disagreement,100*(1-coverage))
    influence=policy.max_influence*(1-uncertainty/100)*(.5+.5*transitions["transition_reliability"])
    if drifted or errors:
        influence=0.0
    champion_signed=champion["buy_score"]-champion["sell_score"]
    combined=(1-influence)*champion_signed+influence*signed
    raw_score=round(50+abs(combined)/2,8)
    proposed="BUY" if combined>0 else "SELL" if combined<0 else "NO_TRADE"
    estimate=calibration(learning,dict(regime,direction=proposed),raw_score,policy)
    reasons=list(errors)
    if champion.get("veto_codes") or champion["direction"] not in ("BUY","SELL"):
        reasons.append("CHAMPION_RISK_OR_DATA_VETO")
    if ess < policy.min_samples:
        reasons.append("INSUFFICIENT_HISTORY")
    if uncertainty >= policy.max_uncertainty:
        reasons.append("EXTREME_UNCERTAINTY")
    if coverage < .7:
        reasons.append("INSUFFICIENT_COMPONENT_COVERAGE")
    if estimate["probability"] is None:
        reasons.append("CALIBRATION_FAILURE")
    elif estimate["probability"] < policy.min_confidence:
        reasons.append("INSUFFICIENT_CALIBRATED_CONFIDENCE")
    if proposed != champion["direction"] or raw_score < 68:
        reasons.append("ADAPTIVE_QUALITY_VETO")
    if drifted:
        reasons.append("DRIFT_FALLBACK")
    proposed_influence=influence
    if reasons:
        influence=0.0
    metrics={axis:{value:{"sample_count":len(group),"net_expectancy":mean(r["net_r"] for r in group)}
                  for value in sorted({r["regime"][axis] for r in learning})
                  for group in [[r for r in learning if r["regime"][axis]==value]]} for axis in DIMENSIONS}
    return {"version":VERSION,"namespace":inputs["namespace"],"at":inputs["at"],"mode":"DEMO_ONLY",
            "champion":champion,"regime":regime,"weights_previous":previous,"weights_before_decision":weights,
            "component_votes":signals,"trust":trust_report,"performance_by_dimension":metrics,
            "evidence_sample_count":len(exact),"effective_sample_size":ess,
            "uncertainty_level":round(uncertainty,8),"disagreement_score":round(disagreement,8),
            "adaptive_quality_score":round(max(0,min(100,.4*raw_score+.3*(100-disagreement)+.3*(100-uncertainty)))*coverage,8),
            "score_kind":"UNCALIBRATED_INDEX","adaptive_influence":round(influence,8),
            "proposed_adaptive_influence":round(proposed_influence,8),
            "calibration":estimate,"transitions":transitions,"transition_memory_after":memory_after,"drift":drift_report,
            "historical_sample_ids":[r["id"] for r in learning],"guard_sample_ids":[r["id"] for r in guard],
            "challenger":{"role":"ADAPTIVE_CHALLENGER","direction":"NO_TRADE" if reasons else proposed,
                          "proposed_direction":proposed,"raw_score":raw_score,"calibrated_confidence":estimate["probability"],
                          "calibration_source":estimate["source"],"vetoes":sorted(set(reasons))},
            "routing":{"active":"PHASE3B_CHAMPION","fallback":bool(reasons),"automatic_promotion":False}}


def walk_forward(decisions,history,policy=MetaPolicy(),namespace="offline"):
    """Strict expanding chronology with the same purge/embargo and frozen policy."""
    previous_at=None
    weights={}
    memory={}
    result=[]
    for item in decisions:
        at=parse(item["at"])
        if previous_at is not None and at <= previous_at:
            raise ValueError("Walk-forward inputs must be strictly chronological")
        key=regime_key(item["champion"]["analytics_context"])
        inputs=dict(item,history=history,namespace=namespace,policy=asdict(policy),
                    weights_previous=weights.get(key,dict(BASELINE)),transition_memory=memory)
        output=evaluate_meta(inputs)
        weights[key]=output["weights_before_decision"]
        memory=output["transition_memory_after"]
        result.append({"inputs":inputs,"result":output})
        previous_at=at
    return result
