"""Databento GC order-flow diagnostics isolated from the XAU decision engine.

Historical/delayed data only. This module is deliberately on-demand and read-only:
it never changes Champion/Research scoring, vetoes, entries, or execution state.
"""
from copy import deepcopy
from datetime import timedelta
from threading import Lock
import os

import databento as db

DATASET = "GLBX.MDP3"
SYMBOL = "GC.v.0"
ROLE = "SHADOW_ONLY_NO_SIGNAL_INFLUENCE"
CACHE_SECONDS = 300
_CACHE = {"stored_at": None, "key": None, "value": None}
_CACHE_LOCK = Lock()


def _configured():
    return bool(os.environ.get("DATABENTO_API_KEY", "").strip())


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch(schema, start, end, limit, client=None):
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DATABENTO_NOT_CONFIGURED")
    historical = client or db.Historical(key)
    store = historical.timeseries.get_range(
        dataset=DATASET,
        schema=schema,
        symbols=SYMBOL,
        stype_in="continuous",
        start=start,
        end=end,
        limit=int(limit),
    )
    frame = store.to_df()
    if frame is None or frame.empty:
        return []
    return frame.reset_index().to_dict(orient="records")


def analyze(trades, books):
    buy_volume = sell_volume = neutral_volume = 0.0
    buy_trades = sell_trades = neutral_trades = 0

    for row in trades:
        size = _number(row.get("size"))
        if size is None or size < 0:
            continue
        side = str(row.get("side", "N")).upper()
        if side == "B":
            buy_trades += 1
            buy_volume += size
        elif side == "A":
            sell_trades += 1
            sell_volume += size
        else:
            neutral_trades += 1
            neutral_volume += size

    sided = buy_volume + sell_volume
    delta = buy_volume - sell_volume
    delta_ratio = delta / sided if sided > 0 else 0.0

    imbalances = []
    latest_bid = latest_ask = 0.0
    for row in books[-200:]:
        bid = ask = 0.0
        for level in range(10):
            suffix = f"{level:02d}"
            b = _number(row.get(f"bid_sz_{suffix}"))
            a = _number(row.get(f"ask_sz_{suffix}"))
            if b is not None and b >= 0:
                bid += b
            if a is not None and a >= 0:
                ask += a
        total = bid + ask
        if total > 0:
            imbalances.append((bid - ask) / total)
            latest_bid, latest_ask = bid, ask

    book_imbalance = sum(imbalances) / len(imbalances) if imbalances else 0.0

    # Keep the original weighting when both feeds are present. If MBP-10 is
    # temporarily unavailable, expose a trades-only pressure rather than
    # failing the entire diagnostic endpoint.
    if trades and books:
        pressure_score = 0.65 * delta_ratio + 0.35 * book_imbalance
        pressure_basis = "TRADES_PLUS_MBP10"
    elif trades:
        pressure_score = delta_ratio
        pressure_basis = "TRADES_ONLY_PARTIAL"
    elif books:
        pressure_score = book_imbalance
        pressure_basis = "MBP10_ONLY_PARTIAL"
    else:
        pressure_score = 0.0
        pressure_basis = "NO_FEED_DATA"

    pressure = "BULLISH" if pressure_score >= 0.12 else "BEARISH" if pressure_score <= -0.12 else "NEUTRAL"

    return {
        "trade_flow": {
            "records": len(trades),
            "buy_aggressor_trades": buy_trades,
            "sell_aggressor_trades": sell_trades,
            "neutral_trades": neutral_trades,
            "buy_aggressor_volume": round(buy_volume, 6),
            "sell_aggressor_volume": round(sell_volume, 6),
            "neutral_volume": round(neutral_volume, 6),
            "delta": round(delta, 6),
            "delta_ratio": round(delta_ratio, 6),
        },
        "book_flow": {
            "records": len(books),
            "sampled_book_records": len(imbalances),
            "average_top10_imbalance": round(book_imbalance, 6),
            "latest_top10_bid_size": round(latest_bid, 6),
            "latest_top10_ask_size": round(latest_ask, 6),
        },
        "pressure": {
            "label": pressure,
            "score": round(pressure_score, 6),
            "basis": pressure_basis,
            "validated_signal": False,
        },
    }


def _provider_error(exc):
    value = (type(exc).__name__ + " " + str(exc)).upper()
    if any(marker in value for marker in ("401", "403", "AUTH", "PERMISSION", "UNAUTHORIZED", "FORBIDDEN")):
        return "AUTH_OR_PERMISSION"
    if any(marker in value for marker in ("402", "PAYMENT", "ENTITLE", "CREDIT", "BILLING")):
        return "PAYMENT_OR_ENTITLEMENT"
    if any(marker in value for marker in ("429", "RATE", "TOO MANY")):
        return "RATE_LIMITED"
    if any(marker in value for marker in ("TIMEOUT", "CONNECT", "NETWORK", "DNS")):
        return "NETWORK_OR_TIMEOUT"
    if any(marker in value for marker in ("400", "404", "422", "SYMBOL", "SCHEMA", "REQUEST")):
        return "REQUEST_REJECTED"
    return "PROVIDER_ERROR"


def _cached(now, cache_key):
    with _CACHE_LOCK:
        stored_at = _CACHE["stored_at"]
        if _CACHE["key"] != cache_key or stored_at is None or _CACHE["value"] is None:
            return None
        age = (now - stored_at).total_seconds()
        if age < 0 or age >= CACHE_SECONDS:
            return None
        value = deepcopy(_CACHE["value"])
    value["cache"] = {"hit": True, "age_seconds": round(age, 3), "ttl_seconds": CACHE_SECONDS}
    return value


def _store_cache(now, cache_key, value):
    with _CACHE_LOCK:
        _CACHE["stored_at"] = now
        _CACHE["key"] = cache_key
        _CACHE["value"] = deepcopy(value)


def snapshot(now, minutes=2, delay_minutes=1560, client=None):
    """Return a bounded delayed GC order-flow sample without mutating bot state."""
    minutes = max(1, min(int(minutes), 5))
    # Historical API is for data older than 24h; keep a two-hour safety margin.
    delay_minutes = max(1560, min(int(delay_minutes), 10080))
    base = {
        "status": "NOT_CONFIGURED" if not _configured() else "SHADOW_PENDING",
        "role": ROLE,
        "brain_influence": "NONE",
        "execution_influence": "NONE",
        "dataset": DATASET,
        "symbol": SYMBOL,
        "symbology": "continuous_volume_front_month",
        "data_mode": "HISTORICAL_DELAYED",
        "credential_configured": _configured(),
        "requested_minutes": minutes,
        "delay_minutes": delay_minutes,
    }
    if not _configured():
        return base

    cache_key = (minutes, delay_minutes)
    if client is None:
        cached = _cached(now, cache_key)
        if cached is not None:
            return cached

    end = now - timedelta(minutes=delay_minutes)
    start = end - timedelta(minutes=minutes)
    base["window"] = {"start": start.isoformat(), "end": end.isoformat()}

    feeds = {}
    try:
        trades = _fetch("trades", start, end, 10000, client)
        feeds["trades"] = {"status": "READY", "records": len(trades)}
    except Exception as exc:
        trades = []
        feeds["trades"] = {"status": "UNAVAILABLE", "error": _provider_error(exc)}

    try:
        books = _fetch("mbp-10", start, end, 10000, client)
        feeds["mbp10"] = {"status": "READY", "records": len(books)}
    except Exception as exc:
        books = []
        feeds["mbp10"] = {"status": "UNAVAILABLE", "error": _provider_error(exc)}

    base["feeds"] = feeds
    base.update(analyze(trades, books))
    if trades and books:
        base["status"] = "SHADOW_READY"
    elif trades or books:
        base["status"] = "SHADOW_PARTIAL"
    else:
        base["status"] = "SHADOW_UNAVAILABLE"

    base["cache"] = {"hit": False, "age_seconds": 0.0, "ttl_seconds": CACHE_SECONDS}
    if client is None and base["status"] in ("SHADOW_READY", "SHADOW_PARTIAL"):
        _store_cache(now, cache_key, base)
    return base
