"""Pure timestamp, input validation, and deterministic decision functions."""
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from zoneinfo import ZoneInfo

from .scoring import Candle, CONFIG, score_engine

UTC = timezone.utc
INTERVALS = {"1min": 60, "5min": 300, "15min": 900, "1h": 3600, "4h": 14400}
VERSION = "phase1-2"


def stamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def parse(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timestamp must include UTC offset")
    return result.astimezone(UTC)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return sha256(canonical(value).encode()).hexdigest()


def market_closed(at: datetime) -> bool:
    """Conservative spot-gold session policy; holidays also fail freshness checks."""
    local = at.astimezone(ZoneInfo("America/New_York"))
    day, hour = local.weekday(), local.hour
    return (day == 5 or (day == 4 and hour >= 17) or
            (day == 6 and hour < 18) or (day < 4 and hour == 17))


def allowed_session_gap(start: datetime, end: datetime, seconds: int) -> bool:
    # Allow missing intervals only when they overlap a scheduled session closure.
    cursor = start
    while cursor < end:
        segment_end = min(cursor + timedelta(seconds=seconds), end)
        probe = cursor
        overlaps_closed = False
        while probe < segment_end:
            overlaps_closed |= market_closed(probe)
            probe += timedelta(minutes=1)
        if not overlaps_closed:
            return False
        cursor = segment_end
    return True


@dataclass(frozen=True)
class Policy:
    freshness_grace_seconds: int = 90
    heartbeat_limit_seconds: int = 150
    confirmation_edge: int = 8
    bias_veto_edge: int = 14
    max_entry_drift_atr: float = 0.5
    max_fill_gap_atr: float = 0.5
    daily_loss_limit_r: float = 3.0
    kill_switch: bool = False

    def __post_init__(self):
        if not (0 < self.freshness_grace_seconds <= 120):
            raise ValueError("freshness_grace_seconds must be within 1..120")
        if self.daily_loss_limit_r <= 0 or self.max_fill_gap_atr <= 0:
            raise ValueError("Risk limits must be positive")


def safe_input(value):
    """Keep rejected inputs replayable without non-JSON values or NaN in hashes."""
    if isinstance(value, dict):
        return {k: safe_input(v) for k, v in value.items() if isinstance(k, str)}
    if isinstance(value, (list, tuple)):
        return [safe_input(v) for v in value]
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


def freshness_errors(closes, now, policy):
    errors = []
    for interval, seconds in INTERVALS.items():
        try:
            age = (now - parse(closes[interval])).total_seconds()
            if age < 0 or age >= seconds + policy.freshness_grace_seconds:
                errors.append(f"STALE_DATA:{interval}")
        except (KeyError, ValueError, TypeError, AttributeError):
            errors.append(f"MISSING_TIMESTAMP:{interval}")
    return errors


def closed_frame(rows: list[dict], interval: str, now: datetime,
                 minimum=60, continuity=True) -> tuple[list[Candle], list[str]]:
    seconds = INTERVALS[interval]
    errors = []
    candles = []
    seen = set()
    try:
        if not isinstance(rows, list):
            raise ValueError("Invalid frame")
        for row in rows:
            candle = Candle(**row)
            opened = parse(candle.t)
            if opened.second or opened.microsecond:
                raise ValueError("Unaligned candle")
            if opened > now:
                raise ValueError("Future candle")
            if opened in seen:
                raise ValueError("Duplicate candle")
            seen.add(opened)
            values = [candle.o, candle.h, candle.l, candle.c]
            if not all(math.isfinite(x) and x > 0 for x in values):
                raise ValueError("Invalid price")
            if not (candle.l <= min(candle.o, candle.c) <= max(candle.o, candle.c) <= candle.h):
                raise ValueError("Invalid OHLC")
            if opened + timedelta(seconds=seconds) <= now:
                candles.append(candle)
        candles.sort(key=lambda c: parse(c.t))
        if len(candles) < minimum:
            errors.append(f"INSUFFICIENT_DATA:{interval}")
        for a, b in zip(candles, candles[1:]):
            expected = parse(a.t) + timedelta(seconds=seconds)
            if parse(b.t) < expected:
                raise ValueError("Overlapping candles")
            if continuity and parse(b.t) > expected and not allowed_session_gap(expected, parse(b.t), seconds):
                errors.append(f"CANDLE_GAP:{interval}")
                break
    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
        return [], [f"INVALID_DATA:{interval}"]
    return candles, errors


def inspect_frames(snapshot: dict, policy: Policy):
    now = parse(snapshot["observed_at"])
    frames, errors = {}, []
    for interval, seconds in INTERVALS.items():
        rows = snapshot["frames"].get(interval, [])
        frames[interval], problems = closed_frame(rows, interval, now)
        errors.extend(problems)
        if frames[interval]:
            latest_close = parse(frames[interval][-1].t) + timedelta(seconds=seconds)
            if (now - latest_close).total_seconds() >= seconds + policy.freshness_grace_seconds:
                errors.append(f"STALE_DATA:{interval}")
    return frames, sorted(set(errors))


def evaluate(snapshot: dict, policy: Policy) -> dict:
    """No database/network/wall clock: identical input and version => identical output."""
    now = parse(snapshot["observed_at"])
    frames, vetoes = inspect_frames(snapshot, policy)
    if market_closed(now):
        vetoes.append("MARKET_CLOSED")
    if policy.kill_switch:
        vetoes.append("KILL_SWITCH")
    base = {
        "direction": "NO_TRADE", "candidate_direction": "NO_TRADE",
        "confidence": 0.0, "confidence_kind": "UNCALIBRATED_SCORE",
        "buy_score": 0, "sell_score": 0, "entry": None,
        "sl": None, "tp1": None, "tp2": None, "action": "WAIT",
        "mode": "DATA_WAIT", "reasons": [], "timestamp_utc": stamp(now),
        "engine": "DardaniaXAUTRADE AI Phase 1", "strategy_version": VERSION,
        "symbol": "XAU/USD", "timeframe": "5min", "market_source": "Twelve Data",
        "data_status": "ERROR" if vetoes else "LIVE_DATA", "timeframes": {},
        "risk_veto": False, "veto_codes": [], "signal_candle_close": None,
        "expires_at": None, "decision_type": "DEMO_ONLY",
        "source_candle_closes": {
            k: stamp(parse(v[-1].t) + timedelta(seconds=INTERVALS[k]))
            for k, v in frames.items() if v},
    }
    if frames["5min"]:
        close = parse(frames["5min"][-1].t) + timedelta(minutes=5)
        base["signal_candle_close"] = stamp(close)
        base["expires_at"] = stamp(close + timedelta(minutes=5))
    if not vetoes:
        five = frames["5min"][-120:]
        cutoff = parse(base["signal_candle_close"])
        base.update(score_engine(five, cutoff))
        base["timestamp_utc"] = stamp(now)
        base["engine"] = "DardaniaXAUTRADE AI Phase 1"
        base["candidate_direction"] = base["direction"]
        direction = base["direction"]
        for interval in ("15min", "1h", "4h"):
            # No higher-timeframe information after the originating 5m close.
            eligible = [c for c in frames[interval]
                        if parse(c.t) + timedelta(seconds=INTERVALS[interval]) <= cutoff]
            if len(eligible) < 60:
                vetoes.append(f"INSUFFICIENT_CONFIRMATION:{interval}")
                continue
            score = score_engine(eligible[-120:], cutoff)
            edge = score["buy_score"] - score["sell_score"]
            bias = "BUY" if edge >= policy.confirmation_edge else "SELL" if edge <= -policy.confirmation_edge else "NEUTRAL"
            base["timeframes"][interval] = {
                "bias": bias, "edge": edge,
                "candle_close": stamp(parse(eligible[-1].t) + timedelta(seconds=INTERVALS[interval])),
            }
            if direction in ("BUY", "SELL"):
                if interval == "15min" and bias != direction:
                    vetoes.append("CONFIRMATION_15M")
                elif interval != "15min" and abs(edge) >= policy.bias_veto_edge and bias != direction:
                    vetoes.append(f"BIAS_CONFLICT:{interval}")
        base["monitor_price"] = frames["1min"][-1].c
        base["monitor_candle_close"] = stamp(parse(frames["1min"][-1].t) + timedelta(minutes=1))
        if base["volatility"] == "HIGH":
            vetoes.append("HIGH_VOLATILITY")
        if base["atr"] <= 0:
            vetoes.append("INVALID_ATR")
        if direction in ("BUY", "SELL"):
            if abs(base["monitor_price"] - base["entry"]) > policy.max_entry_drift_atr * base["atr"]:
                vetoes.append("ENTRY_DRIFT")
            if not valid_plan(base):
                vetoes.append("INVALID_TRADE_PLAN")
    return apply_veto(base, vetoes)


def valid_plan(plan: dict) -> bool:
    try:
        entry, sl, tp1, tp2 = (float(plan[k]) for k in ("entry", "sl", "tp1", "tp2"))
        if not all(math.isfinite(v) and v > 0 for v in (entry, sl, tp1, tp2)):
            return False
        return sl < entry < tp1 < tp2 if plan["direction"] == "BUY" else tp2 < tp1 < entry < sl
    except (TypeError, ValueError, KeyError):
        return False


def apply_veto(result: dict, codes: list[str]) -> dict:
    result = dict(result)
    result["veto_codes"] = sorted(set(result.get("veto_codes", []) + codes))
    result["risk_veto"] = bool(result["veto_codes"])
    if result["risk_veto"]:
        result.update(direction="NO_TRADE", action="WAIT", sl=None, tp1=None, tp2=None)
        result["reasons"] = list(result.get("reasons", [])) + ["Risk veto: " + c for c in codes]
    return result
