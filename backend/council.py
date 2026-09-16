"""Pure Phase 2 multi-timeframe council. No I/O, broker, or external context."""
from dataclasses import asdict, dataclass
from datetime import timedelta
import math

from .calibration import calibrate
from .domain import (INTERVALS, Policy, apply_veto, inspect_frames, market_closed,
                     parse, stamp, valid_plan)
from .scoring import (CONFIG, atr, breakout_bias, candle_pressure, efficiency_ratio,
                      ema, rsi, session_name, structure_bias)

VERSION = "phase2-1"
MODEL_VERSION = "technical-council-v1"
WEIGHTS = {"technical": .30, "momentum": .20, "market_structure": .20,
           "volatility": .10, "multi_timeframe_alignment": .20}
ALIGNMENT_WEIGHTS = {"1min": .10, "15min": .35, "1h": .30, "4h": .25}
ROLES = {"1min": "TIMING_MONITOR", "5min": "ENTRY", "15min": "CONFIRMATION",
         "1h": "DIRECTIONAL_BIAS", "4h": "MACRO_TREND_BIAS"}


@dataclass(frozen=True)
class CouncilPolicy:
    min_score: float = 68
    min_edge: float = 14
    bias_threshold: float = .15
    min_confidence: float = .55
    calibration_min_samples: int = 30
    calibration_window: int = 500
    allow_demo_warmup: bool = True
    warmup_min_score: float = 80
    extreme_atr_ratio: float = 1.8
    extreme_bar_atr: float = 3.0
    extreme_atr_fraction: float = .015

    def __post_init__(self):
        for name, value in asdict(self).items():
            if not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"Invalid council setting: {name}")
        if not (0 < self.min_score <= self.warmup_min_score <= 100
                and 0 < self.min_edge <= 100 and 0 < self.bias_threshold < 1
                and 0 < self.min_confidence < 1
                and 30 <= self.calibration_min_samples <= self.calibration_window <= 5000
                and self.extreme_atr_ratio > 1 and self.extreme_bar_atr > 1
                and 0 < self.extreme_atr_fraction < 1):
            raise ValueError("Invalid council thresholds")
        if (type(self.calibration_min_samples) is not int or type(self.calibration_window) is not int
                or type(self.allow_demo_warmup) is not bool):
            raise ValueError("Invalid council setting type")


def clamp(value, low=-1.0, high=1.0):
    return max(low, min(high, value))


def features(candles, settings):
    closes = [c.c for c in candles]
    fast, slow, trend = (ema(closes, period) for period in (8, 21, 55))
    series = atr(candles)
    current_atr = series[-1]
    scale = max(current_atr, 1e-9)
    technical = (.45 * clamp((fast[-1] - slow[-1]) / (2 * scale))
                 + .30 * clamp((closes[-1] - trend[-1]) / (3 * scale))
                 + .25 * clamp((fast[-1] - fast[-5]) / scale))
    # Flat prices have neutral RSI for scoring (the legacy RSI helper returns 100).
    rsi_value = rsi(closes)[-1] if max(closes[-15:]) != min(closes[-15:]) else 50.0
    momentum = .5 * clamp((rsi_value - 50) / 25) + .5 * clamp(candle_pressure(candles) * 2)
    structure = .65 * structure_bias(candles) + .35 * breakout_bias(candles)
    baseline = sum(series[-41:-1]) / 40
    ratio = current_atr / max(baseline, 1e-9)
    last = candles[-1]
    true_range = max(last.h - last.l, abs(last.h - candles[-2].c), abs(last.l - candles[-2].c))
    bar_ratio = true_range / max(series[-2], 1e-9)
    extreme = (ratio >= settings.extreme_atr_ratio or bar_ratio >= settings.extreme_bar_atr
               or current_atr / closes[-1] >= settings.extreme_atr_fraction)
    quality = 0 if extreme or current_atr <= 0 else clamp(100 - 50 * abs(math.log2(max(ratio, 1e-9))), 0, 100)
    bias_score = .5 * technical + .25 * momentum + .25 * structure
    bias = "BUY" if bias_score >= settings.bias_threshold else "SELL" if bias_score <= -settings.bias_threshold else "NEUTRAL"
    return {
        "technical": technical, "momentum": momentum, "market_structure": structure,
        "volatility_quality": quality, "atr": current_atr, "atr_ratio": ratio,
        "bar_atr_ratio": bar_ratio, "extreme_volatility": extreme,
        "rsi": rsi_value, "efficiency": efficiency_ratio(closes), "bias": bias,
        "bias_score": bias_score,
    }


def evaluate_council(snapshot, policy: Policy):
    settings = CouncilPolicy(**snapshot["council_policy"])
    now = parse(snapshot["observed_at"])
    frames, errors = inspect_frames(snapshot, policy)
    closes = {k: stamp(parse(v[-1].t) + timedelta(seconds=INTERVALS[k]))
              for k, v in frames.items() if v}
    result = {
        "direction": "NO_TRADE", "candidate_direction": "NO_TRADE",
        "confidence": 0.0, "calibrated_confidence": None,
        "confidence_kind": "UNAVAILABLE", "calibration": None, "raw_score": 0.0,
        "buy_score": 0.0, "sell_score": 0.0, "edge": 0.0, "entry": None,
        "sl": None, "tp1": None, "tp2": None, "atr": None, "rsi_state": None,
        "action": "WAIT", "mode": "DATA_WAIT", "reasons": [],
        "timestamp_utc": stamp(now), "engine": "DardaniaXAUTRADE AI Phase 2",
        "strategy_version": VERSION, "model_version": MODEL_VERSION,
        "model_identity": snapshot["model_identity"],
        "symbol": "XAU/USD", "timeframe": "5min", "market_source": "Twelve Data",
        "data_status": "ERROR" if errors else "LIVE_DATA", "timeframes": {},
        "components": {}, "risk_veto": False, "veto_codes": [],
        "signal_candle_close": closes.get("5min"), "expires_at": None,
        "decision_type": "DEMO_ONLY", "source_candle_closes": closes,
        "session": session_name(now), "warmup_trade": False,
        "monitor_price": None, "monitor_candle_close": None,
    }
    if closes.get("5min"):
        result["expires_at"] = stamp(parse(closes["5min"]) + timedelta(minutes=5))
    if market_closed(now):
        errors.append("MARKET_CLOSED")
    if policy.kill_switch:
        errors.append("KILL_SWITCH")
    if errors:
        return apply_veto(result, errors)

    cutoff = parse(closes["5min"])
    selected = {}
    for interval in INTERVALS:
        available_at = now if interval == "1min" else cutoff
        selected[interval] = [c for c in frames[interval]
                              if parse(c.t) + timedelta(seconds=INTERVALS[interval]) <= available_at][-120:]
        if len(selected[interval]) < 60:
            errors.append(f"INSUFFICIENT_CONFIRMATION:{interval}")
    if errors:
        return apply_veto(result, errors)
    analysis = {k: features(v, settings) for k, v in selected.items()}
    for interval, f in analysis.items():
        result["timeframes"][interval] = {
            **{k: round(v, 6) if type(v) is float else v for k, v in f.items()},
            "role": ROLES[interval],
            "candle_close": stamp(parse(selected[interval][-1].t) + timedelta(seconds=INTERVALS[interval])),
        }
        if f["extreme_volatility"]:
            errors.append(f"EXTREME_VOLATILITY:{interval}")
        if f["atr"] <= 0:
            errors.append(f"INVALID_ATR:{interval}")

    five = analysis["5min"]
    signed = {k: five[k] for k in ("technical", "momentum", "market_structure")}
    signed["multi_timeframe_alignment"] = sum(
        weight * ({"BUY": 1, "SELL": -1, "NEUTRAL": 0}[analysis[k]["bias"]])
        for k, weight in ALIGNMENT_WEIGHTS.items())
    for name, weight in WEIGHTS.items():
        buy = five["volatility_quality"] if name == "volatility" else 50 * (1 + signed[name])
        sell = buy if name == "volatility" else 100 - buy
        result["components"][name] = {"buy": round(buy, 4), "sell": round(sell, 4), "weight": weight}
    for side in ("buy", "sell"):
        result[f"{side}_score"] = round(sum(c[side] * c["weight"] for c in result["components"].values()), 4)
    buy, sell = result["buy_score"], result["sell_score"]
    result["edge"] = round(abs(buy - sell), 4)
    result["raw_score"] = max(buy, sell)
    direction = ("BUY" if buy > sell else "SELL") if max(buy, sell) >= settings.min_score and abs(buy - sell) >= settings.min_edge else "NO_TRADE"
    result.update(direction=direction, candidate_direction=direction,
                  entry=round(selected["5min"][-1].c, 2), atr=round(five["atr"], 4),
                  rsi_state=round(five["rsi"], 2), monitor_price=selected["1min"][-1].c,
                  monitor_candle_close=closes["1min"],
                  mode="HIGH_VOLATILITY" if any(f["extreme_volatility"] for f in analysis.values())
                  else "TREND" if five["efficiency"] >= .48 else "RANGE" if five["efficiency"] <= .20 else "TRANSITION")
    result["reasons"] = [f"{name}: BUY {c['buy']:.1f}, SELL {c['sell']:.1f}, weight {c['weight']:.0%}"
                         for name, c in result["components"].items()]
    if direction == "NO_TRADE":
        errors.append("INSUFFICIENT_SCORE")
    else:
        for interval in ("15min", "1h", "4h"):
            if analysis[interval]["bias"] != direction:
                errors.append(f"TIMEFRAME_DISAGREEMENT:{interval}")
        if analysis["1min"]["bias"] not in (direction, "NEUTRAL"):
            errors.append("TIMING_DISAGREEMENT:1min")
        if abs(result["monitor_price"] - result["entry"]) > policy.max_entry_drift_atr * five["atr"]:
            errors.append("ENTRY_DRIFT")
        calibration = calibrate(direction, result["raw_score"], snapshot["calibration_samples"],
                                cutoff, settings.calibration_min_samples)
        result["calibration"] = calibration
        if calibration["status"] == "READY":
            result.update(confidence=calibration["probability"], calibrated_confidence=calibration["probability"],
                          confidence_kind="EMPIRICAL_DEMO_BETA_BIN")
            result["reasons"].append(f"Historical demo calibration: {calibration['wins']}/{calibration['sample_count']} positive outcomes")
            if calibration["probability"] < settings.min_confidence:
                errors.append("INSUFFICIENT_CONFIDENCE")
        else:
            # Do not pass a heuristic score off as a historical probability.
            result["confidence_kind"] = "UNCALIBRATED_WARMUP"
            result["reasons"].append(f"Calibration warm-up: {calibration['sample_count']}/{settings.calibration_min_samples} comparable closed demos")
            if not settings.allow_demo_warmup or result["raw_score"] < settings.warmup_min_score:
                errors.append("INSUFFICIENT_CALIBRATION")
            else:
                result["warmup_trade"] = True
        multiplier = 1.10 if result["mode"] == "TREND" else .90 if result["mode"] == "RANGE" else 1.0
        distance = max(five["atr"] * CONFIG.risk_atr * multiplier, result["entry"] * .0008)
        sign = 1 if direction == "BUY" else -1
        result.update(sl=round(result["entry"] - sign * distance, 2),
                      tp1=round(result["entry"] + sign * distance * CONFIG.tp1_r, 2),
                      tp2=round(result["entry"] + sign * distance * CONFIG.tp2_r, 2), action=f"{direction} SETUP")
        if not valid_plan(result):
            errors.append("INVALID_TRADE_PLAN")
    return apply_veto(result, errors)
