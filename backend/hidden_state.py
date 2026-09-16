"""Causal gold-state heuristics. Scores are bounded indices, never probabilities."""
from dataclasses import asdict, dataclass
from datetime import timedelta
import math
from statistics import mean

from .domain import INTERVALS, inspect_frames, parse, stamp

STATES = ("ACCUMULATION", "COMPRESSION", "LIQUIDITY_SWEEP", "FRACTURE",
          "EXPANSION", "EXHAUSTION", "RANGE", "SHOCK")


@dataclass(frozen=True)
class HiddenPolicy:
    entropy_veto: float = 78
    tension_veto: float = 85
    shock_atr: float = 3
    event_resolution_minutes: int = 30
    unstable_transitions: int = 3

    def __post_init__(self):
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in asdict(self).values()):
            raise ValueError("Invalid hidden policy")
        if not (50 <= self.entropy_veto <= 100 and 50 <= self.tension_veto <= 100
                and 2 <= self.shock_atr <= 10 and type(self.event_resolution_minutes) is int
                and 5 <= self.event_resolution_minutes <= 120 and type(self.unstable_transitions) is int
                and 2 <= self.unstable_transitions <= 3):
            raise ValueError("Invalid hidden thresholds")


def clip(value, low=0, high=100):
    return round(max(low, min(high, value)), 6)


def sign(value):
    return 1 if value > 0 else -1 if value < 0 else 0


def true_ranges(bars):
    return [max(b.h-b.l, abs(b.h-a.c), abs(b.l-a.c)) for a, b in zip(bars, bars[1:])]


def entropy(closes):
    changes = [b-a for a, b in zip(closes, closes[1:])]
    distance = sum(abs(x) for x in changes)
    if not changes or distance <= 1e-12:
        return {"score": 100.0, "efficiency": 0.0, "sign_entropy": 0.0, "flip_rate": 0.0}
    directions = [sign(x) for x in changes if abs(x) > 1e-12]
    p = sum(x > 0 for x in directions) / len(directions)
    shannon = -sum(q * math.log2(q) for q in (p, 1-p) if q)
    flips = sum(a != b for a, b in zip(directions, directions[1:])) / max(1, len(directions)-1)
    efficiency = abs(closes[-1]-closes[0]) / distance
    return {"score": clip(100 * (1-efficiency) * (.6*shannon + .4*flips)),
            "efficiency": round(efficiency, 6), "sign_entropy": round(shannon, 6), "flip_rate": round(flips, 6)}


def liquidity(bars):
    """Trailing extrema exclude the possible breakout/sweep and its follow-through."""
    prior, previous, last = bars[-22:-2], bars[-2], bars[-1]
    high, low = max(b.h for b in prior), min(b.l for b in prior)
    scale = max(mean(true_ranges(bars[-43:-3])), 1e-9)
    epsilon = scale * 1e-6
    upper = last.c < high-epsilon and any(b.h > high+epsilon and b.c < high-epsilon for b in (previous,last))
    lower = last.c > low+epsilon and any(b.l < low-epsilon and b.c > low+epsilon for b in (previous,last))
    false_up = previous.c > high and last.c < high
    false_down = previous.c < low and last.c > low
    # Ambiguous two-sided sweeps stay neutral rather than guessing intrabar order.
    bullish, bearish = lower or false_down, upper or false_up
    reclaim = 1 if bullish and not bearish else -1 if bearish and not bullish else 0
    wick = (last.h-max(last.o,last.c) if reclaim < 0 else min(last.o,last.c)-last.l if reclaim > 0 else 0)
    rejection = clip(wick / max(last.h-last.l, 1e-9) * 100)
    displacement = clip(abs(last.c-last.o) / scale * 50)
    breakout = 1 if last.c > high else -1 if last.c < low else 0
    follow = (breakout != 0 and sign(previous.c-high if breakout > 0 else previous.c-low) == breakout
              and sign(last.c-previous.c) == breakout) or (reclaim != 0 and sign(last.c-previous.c) == reclaim)
    pressure = clip(reclaim * (50 + rejection/2) if reclaim else breakout * displacement, -100, 100)
    return {"swing_high": high, "swing_low": low, "upper_sweep": upper, "lower_sweep": lower,
            "false_breakout": bool(false_up or false_down), "reclaim_direction": reclaim,
            "rejection": rejection, "displacement": displacement, "breakout_direction": breakout,
            "follow_through": bool(follow), "liquidity_pressure": pressure, "baseline_atr": scale}


def tension(timeframes, previous=None):
    biases = {k: timeframes[k]["bias_score"] for k in INTERVALS if k in timeframes}
    if len(biases) != 5:
        return {"score": None, "convergence": None, "biases": biases, "alignment": "UNAVAILABLE"}
    strength = sum(abs(v) for v in biases.values())
    score = clip(100 * (1-abs(sum(biases.values()))/strength)) if strength else 0.0
    return {"score": score, "convergence": round(previous-score, 6) if previous is not None else None,
            "biases": biases, "alignment": "ALIGNED" if score < 25 else "MIXED" if score < 70 else "OPPOSED"}


def macro_pressure(intelligence):
    scores = intelligence["scores"]
    data_vetoes = [v for v in intelligence["vetoes"] if v not in ("MAJOR_EVENT_IMMINENT","MAJOR_EVENT_BLACKOUT")]
    if data_vetoes or any(scores[k] is None for k in ("usd_score", "yields_score", "news_sentiment_score")):
        return None
    weights = {"usd_score": .4, "yields_score": .3, "macro_score": .15, "news_sentiment_score": .15}
    values = [(scores[k], w) for k, w in weights.items() if scores[k] is not None]
    return round(sum(v*w for v, w in values)/sum(w for _, w in values), 6)


def resilience(bars, intelligence, momentum_seconds=3600):
    expected = macro_pressure(intelligence)
    required = momentum_seconds//60+1
    if expected is None or momentum_seconds % 60 or required < 2 or len(bars) < required:
        return {"gold_resilience_score": None, "expected_pressure": expected, "components": {}, "reason": "Unavailable aligned gold/macro history"}
    window = bars[-required:]
    if any(parse(b.t)-parse(a.t) != timedelta(minutes=1) for a,b in zip(window,window[1:])):
        return {"gold_resilience_score": None, "expected_pressure": expected, "components": {}, "reason": "Gold reaction window crosses a data/session gap"}
    scale = max(mean(true_ranges(window)), 1e-9)
    actual = clip((window[-1].c-window[0].c)/(scale*4), -1, 1)
    score = clip((actual-expected/100)*50, -100, 100)
    components = {key: {"expected_pressure": value,
                       "residual_score": clip((actual-value/100)*50,-100,100) if value is not None else None,
                       "resisted_expected_move": bool(abs(value) >= 20 and (actual*sign(value) <= .15)) if value is not None else None}
                  for key,value in intelligence["scores"].items() if key in
                  ("usd_score","yields_score","macro_score","news_sentiment_score")}
    return {"gold_resilience_score": score, "expected_pressure": expected,
            "components": components,
            "gold_move_atr": round((window[-1].c-window[0].c)/scale, 6),
            "window_start": stamp(parse(window[0].t)+timedelta(minutes=1)),
            "window_end": stamp(parse(window[-1].t)+timedelta(minutes=1)),
            "reason": "Gold return minus contemporaneous signed macro pressure; heuristic residual, not causality"}


def event_absorption(bars, events, prior_macro, now, policy):
    reports = []
    unique = {}
    for e in events:
        if (e["kind"] in ("CPI", "NFP", "PCE", "FOMC") or e["importance"] == "HIGH") and parse(e["scheduled_at"]) <= now:
            unique[e["event_key"]] = e
    for key, event in sorted(unique.items()):
        start = parse(event["scheduled_at"])
        if start < now-timedelta(hours=6):
            continue
        pre = [b for b in bars if parse(b.t)+timedelta(minutes=1) <= start]
        post = [b for b in bars if parse(b.t) >= start and parse(b.t)+timedelta(minutes=1) <= now]
        report = {"event_key": key, "scheduled_at": event["scheduled_at"], "source": event["source"],
                  "published_at": event["published_at"], "status": "INSUFFICIENT_HISTORY", "unresolved": True,
                  "initial_impulse_atr": None, "recovery_seconds": None, "rejection": None,
                  "follow_through_atr": None, "failed_expected_reaction": None}
        reports.append(report)
        # A sub-minute event bar mixes pre/post prices: decline to infer an impulse.
        if start.second or start.microsecond or len(pre) < 15 or len(post) < 3:
            continue
        if parse(pre[-1].t)+timedelta(minutes=1) != start or any(parse(b.t)-parse(a.t) != timedelta(minutes=1) for a,b in zip(pre[-15:],pre[-14:])):
            continue
        if parse(post[0].t) != start or any(parse(b.t)-parse(a.t) != timedelta(minutes=1) for a,b in zip(post,post[1:])):
            continue
        scale = max(mean(true_ranges(pre[-15:])), 1e-9)
        origin = pre[-1].c
        initial = post[:3]
        extreme = max(initial, key=lambda b: max(abs(b.h-origin), abs(b.l-origin)))
        peak = extreme.h if abs(extreme.h-origin) >= abs(extreme.l-origin) else extreme.l
        impulse = (peak-origin)/scale
        direction = sign(impulse)
        latest = post[-1].c
        remaining = (latest-origin)/scale
        recovery = next((stamp(parse(b.t)+timedelta(minutes=1)) for b in post[3:]
                         if (b.c-origin)*direction <= abs(peak-origin)*.25), None)
        rejection = clip((1-remaining/impulse)*100) if impulse else 0.0
        old = [r for r in prior_macro if start-timedelta(hours=1) <= parse(r["at"]) < start and r["pressure"] is not None]
        expectation = max(old,key=lambda r:r["at"]) if old else None
        expected = expectation["pressure"] if expectation else None
        elapsed = (now-parse(event["end_at"])).total_seconds()/60
        resolved = elapsed >= policy.event_resolution_minutes and (rejection >= 75 or
                   (len(post) >= 5 and max(true_ranges(post[-5:])) <= 2*scale))
        report.update(status="ABSORBED" if recovery else "FOLLOW_THROUGH" if resolved else "SHOCK_PENDING",
            unresolved=not resolved, initial_impulse_atr=round(impulse,6),
            recovery_seconds=(parse(recovery)-start).total_seconds() if recovery else None,
            rejection=rejection, follow_through_atr=round(remaining,6), expected_pressure_before_event=expected,
            expectation_observed_at=expectation["at"] if expectation else None,
            expectation_snapshot_id=expectation.get("snapshot_id") if expectation else None,
            failed_expected_reaction=(sign(remaining) != sign(expected) and abs(expected) >= 20) if expected is not None else None)
    return reports


def classify_state(shock, exhaustion, expansion, fracture, liquid, compression, accumulation):
    if shock:
        return "SHOCK"
    if exhaustion >= 70:
        return "EXHAUSTION"
    if expansion >= 65:
        return "EXPANSION"
    if fracture >= 55:
        return "FRACTURE"
    if liquid:
        return "LIQUIDITY_SWEEP"
    if compression >= 65:
        return "COMPRESSION"
    if accumulation >= 55:
        return "ACCUMULATION"
    return "RANGE"


def analyze_hidden(snapshot, base, risk_policy):
    now = parse(snapshot["observed_at"])
    policy = HiddenPolicy(**snapshot["hidden_policy"])
    frames, errors = inspect_frames(snapshot, risk_policy)
    result = {"state": None, "method": "CAUSAL_GOLD_HEURISTICS_V1", "score_kind": "UNCALIBRATED_INDEX",
              "as_of": stamp(now), "vetoes": list(errors), "reasons": [], "dgfe": {},
              "entropy": {}, "tension": {}, "resilience": {}, "event_absorption": [], "liquidity": {}}
    if errors or not base["timeframes"]:
        result["vetoes"] = sorted(set(errors + ["HIDDEN_DATA_UNAVAILABLE"]))
        return result
    cutoff = parse(base["signal_candle_close"])
    bars = [b for b in frames["5min"] if parse(b.t)+timedelta(minutes=5) <= cutoff]
    minute = frames["1min"]
    ranges = true_ranges(bars)
    scale = max(mean(ranges[-27:-7]), 1e-9)
    compression = clip(100*(1-mean(ranges[-6:])/scale))
    prior_compression = clip(100*(1-mean(ranges[-7:-1])/scale))
    liq = liquidity(bars)
    ent = entropy([b.c for b in bars[-25:]])
    history = [h for h in snapshot["hidden_history"] if parse(h["at"]) < now and h["candle_close"] < base["signal_candle_close"]]
    previous = history[-1]["tension"] if history else None
    mtf = tension(base["timeframes"], previous)
    resilient = resilience(minute, base["intelligence"],snapshot["intelligence_policy"]["momentum_seconds"])
    events = event_absorption(minute, base["intelligence"].get("details", {}).get("calendar", []),
                              snapshot["prior_macro"], now, policy)
    displacement = liq["displacement"]
    pressure = clip(.5 * base["timeframes"]["5min"]["bias_score"] * 100 +
                    .3 * liq["liquidity_pressure"] + .2 * (resilient["gold_resilience_score"] or 0), -100, 100)
    fracture = clip(.4*prior_compression + .4*displacement + .2*abs(liq["liquidity_pressure"]))
    expansion = clip(.4*prior_compression + .4*displacement + 20*liq["follow_through"])
    # Sustained directional expansion need not start with a fresh explosive bar.
    if compression < 40 and ranges[-1]/scale >= .7:
        expansion = max(expansion,clip(100*ent["efficiency"]*abs(base["timeframes"]["5min"]["bias_score"])))
    last = bars[-1]
    trend = last.c-bars[-13].c
    upper = last.h-max(last.o,last.c)
    lower = min(last.o,last.c)-last.l
    wick = upper if trend > 0 else lower
    rejection = wick/max(last.h-last.l, 1e-9)
    exhaustion = clip(50*min(1,abs(trend)/(4*scale)) + 50*rejection) if rejection >= .4 else 0.0
    accumulation = clip(50*(1-ent["efficiency"]) + 50*max(0, liq["liquidity_pressure"])/100)
    minute_ranges = true_ranges(minute)
    minute_scale = max(mean(minute_ranges[-21:-1]),1e-9)
    shock_timeframes = [tf for tf,ratio in (("5min",ranges[-1]/scale),("1min",minute_ranges[-1]/minute_scale))
                        if ratio >= policy.shock_atr]
    shock = bool(shock_timeframes) or any(e["unresolved"] for e in events)
    state = classify_state(shock, exhaustion, expansion, fracture,
                           liq["upper_sweep"] or liq["lower_sweep"] or liq["false_breakout"], compression, accumulation)
    states = [h["state"] for h in history[-3:]]+[state]
    unstable = sum(a != b for a,b in zip(states,states[1:])) >= policy.unstable_transitions
    vetoes = []
    if ent["score"] >= policy.entropy_veto:
        vetoes.append("HIGH_MARKET_ENTROPY")
    if shock:
        vetoes.append("UNRESOLVED_EVENT_SHOCK" if any(e["unresolved"] for e in events) else "MARKET_SHOCK")
    if mtf["score"] is not None and mtf["score"] >= policy.tension_veto:
        vetoes.append("EXTREME_TIMEFRAME_TENSION")
    if unstable:
        vetoes.append("UNSTABLE_HIDDEN_STATE")
    candidate = 1 if base["candidate_direction"] == "BUY" else -1 if base["candidate_direction"] == "SELL" else 0
    if candidate and pressure*candidate <= -50:
        vetoes.append("HIDDEN_COUNCIL_CONFLICT")
    dgfe = {"fracture_score": fracture, "directional_pressure": pressure, "compression_score": compression,
            "prior_compression_score": prior_compression, "liquidity_pressure": liq["liquidity_pressure"],
            "expansion_candidate_score": expansion, "exhaustion_score": exhaustion}
    result.update(state=state, dgfe=dgfe, entropy=ent, tension=mtf, resilience=resilient,
                  event_absorption=events, liquidity=liq, vetoes=vetoes, unstable=unstable,
                  shock_timeframes=shock_timeframes,
                  feature_candle_close=base["signal_candle_close"],
                  reasons=[f"Hidden state {state}; heuristic indices, not probabilities",
                           f"Entropy {ent['score']:.1f}; tension {mtf['score']}; pressure {pressure:.1f}"])
    return result
