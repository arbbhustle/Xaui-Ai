"""Entry-context stratified, gross-R demo analytics; shadows are separate cohorts."""
from collections import defaultdict

DIMENSIONS = ("hidden_state", "session", "volatility_regime", "direction", "timeframe_alignment", "macro_regime")


def context(result):
    hidden = result["hidden_state"]
    pressure = hidden.get("resilience", {}).get("expected_pressure")
    return {"hidden_state": hidden["state"] or "UNAVAILABLE", "session": result["session"],
            "volatility_regime": result["mode"], "direction": result["candidate_direction"],
            "timeframe_alignment": hidden.get("tension", {}).get("alignment", "UNAVAILABLE"),
            "macro_regime": "UNAVAILABLE" if pressure is None else "GOLD_SUPPORTIVE" if pressure >= 20 else
                            "GOLD_ADVERSE" if pressure <= -20 else "MIXED"}


def metrics(rows):
    count = wins = 0
    total = gross_profit = gross_loss = peak = drawdown = 0.0
    for row in rows:
        value = row["r_multiple"]
        count += 1
        wins += value > 0
        total += value
        gross_profit += max(0, value)
        gross_loss += max(0, -value)
        peak = max(peak, total)
        drawdown = max(drawdown, peak-total)
    return {"sample_count": count, "win_rate": wins/count if count else None,
            "expectancy": total/count if count else None, "average_r": total/count if count else None,
            "profit_factor": gross_profit/gross_loss if gross_loss else None,
            "profit_factor_status": "DEFINED" if gross_loss else "NO_LOSSES" if count else "NO_SAMPLES",
            "max_drawdown_r": drawdown, "total_r": total}


def summarize(rows):
    # Economic closure order is deterministic; entry context is never recomputed.
    rows = sorted((r for r in rows if r["status"] == "CLOSED"), key=lambda r:(r["closed_at"], r["id"]))
    groups = {dimension: defaultdict(list) for dimension in DIMENSIONS}
    for row in rows:
        for dimension in DIMENSIONS:
            value = row.get("analytics_context", {}).get(dimension, "UNAVAILABLE")
            groups[dimension][value].append(row)
    return {"overall": metrics(rows), "by": {dim: {key:metrics(value) for key,value in sorted(buckets.items())}
            for dim,buckets in groups.items()}, "units": "GROSS_R_NO_COSTS", "mode": "DEMO_ONLY"}
