"""Prequential evaluation: score only predictions actually recorded before entry."""
from dataclasses import dataclass
from collections import defaultdict
from statistics import mean

from .domain import parse
from .meta_council import independent


def performance(records):
    rows=sorted(records,key=lambda r:(parse(r["closed_at"]),r["id"]))
    equity=peak=drawdown=0.0
    for r in rows:
        equity+=r["net_r"]
        peak=max(peak,equity)
        drawdown=max(drawdown,peak-equity)
    scored=[]
    for r in rows:
        p=r.get("prediction")
        if p is not None and 0 <= p <= 1 and r.get("prediction_at") and parse(r["prediction_at"]) < parse(r["opened_at"]):
            outcome=r["gross_r"] > 0 if r.get("prediction_target") == "POSITIVE_GROSS_R" else r["net_r"] > 0
            scored.append((p,int(outcome)))
    bins=[]
    for i in range(10):
        group=[(p,y) for p,y in scored if min(9,int(p*10))==i]
        bins.append({"lower":i/10,"upper":(i+1)/10,"sample_count":len(group),
                     "mean_prediction":mean(p for p,_ in group) if group else None,
                     "hit_rate":mean(y for _,y in group) if group else None})
    n=len(rows)
    return {"sample_count":n,"effective_sample_size":len(independent(rows)),
            "hit_rate":mean(r["net_r"]>0 for r in rows) if n else None,
            "gross_expectancy_r":mean(r["gross_r"] for r in rows) if n else None,
            "net_expectancy_r":mean(r["net_r"] for r in rows) if n else None,
            "gross_total_r":sum(r["gross_r"] for r in rows),"net_total_r":sum(r["net_r"] for r in rows),
            "max_net_drawdown_r":drawdown,"calibration_sample_count":len(scored),
            "brier_score":mean((p-y)**2 for p,y in scored) if scored else None,
            "calibration_error":sum(b["sample_count"]*abs(b["mean_prediction"]-b["hit_rate"]) for b in bins if b["sample_count"])/len(scored) if scored else None,
            "reliability_bins":bins,"calibration_targets":sorted({r.get("prediction_target","UNAVAILABLE") for r in rows if r.get("prediction") is not None})}


def promotion_report(records,integrity_ok=True,active_guards=()):
    rows=independent([r for r in records if r["source"]=="ADAPTIVE_CHALLENGER"])
    metrics=performance(rows)
    reasons=list(active_guards)
    if len(rows)<100:
        reasons.append("MINIMUM_100_INDEPENDENT_OUTCOMES")
    if metrics["net_expectancy_r"] is None or metrics["net_expectancy_r"]<=0:
        reasons.append("NONPOSITIVE_NET_EXPECTANCY")
    if metrics["max_net_drawdown_r"]>8:
        reasons.append("DRAWDOWN_LIMIT")
    if (metrics["calibration_sample_count"]<60 or metrics["brier_score"] is None or
            metrics["brier_score"]>.25 or metrics["calibration_error"]>.10):
        reasons.append("UNSTABLE_OR_INSUFFICIENT_CALIBRATION")
    states={r["regime"]["hidden_state"] for r in rows}
    sessions={r["regime"]["session"] for r in rows}
    if len(states)<3 or len(sessions)<2:
        reasons.append("INSUFFICIENT_REGIME_BREADTH")
    weeks=defaultdict(list)
    regimes=defaultdict(list)
    for r in rows:
        weeks[parse(r["opened_at"]).strftime("%G-W%V")].append(r)
        regimes[r["regime"]["hidden_state"]].append(r)
    if len(weeks)<4 or (rows and max(map(len,weeks.values()))/len(rows)>.5):
        reasons.append("NARROW_EVALUATION_PERIOD")
    if len([v for v in regimes.values() if len(v)>=20 and mean(r["net_r"] for r in v)>0])<3:
        reasons.append("EXPECTANCY_NOT_SUPPORTED_ACROSS_REGIMES")
    if not integrity_ok or any(not r.get("leakage_audit_passed") for r in rows):
        reasons.append("LEAKAGE_OR_INTEGRITY_NOT_CLEARED")
    # A good aggregate must not conceal a deteriorating final chronological block.
    if len(rows)>=100 and mean(r["net_r"] for r in rows[-25:])<=0:
        reasons.append("RECENT_EXPECTANCY_DEGRADATION")
    return {"status":"PROMOTION_INELIGIBLE" if reasons else "PROMOTION_ELIGIBLE_FOR_HUMAN_REVIEW",
            "reasons":reasons,"metrics":metrics,"automatic_promotion":False,
            "active_strategy":"PHASE3B_CHAMPION"}
