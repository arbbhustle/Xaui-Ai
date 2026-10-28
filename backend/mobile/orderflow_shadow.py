"""Databento GC order-flow diagnostics isolated from the XAU decision engine.

Historical/delayed data only. This module is deliberately on-demand and read-only:
it never changes Champion/Research scoring, vetoes, entries, or execution state.
"""
from datetime import timedelta
import os

import databento as db

DATASET = "GLBX.MDP3"
SYMBOL = "GC.v.0"
ROLE = "SHADOW_ONLY_NO_SIGNAL_INFLUENCE"


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
    # Databento timeseries.get_range returns a DBNStore. Convert only this
    # bounded sample after download; raw provider payloads are never persisted.
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
    pressure_score = 0.65 * delta_ratio + 0.35 * book_imbalance
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
            "validated_signal": False,
        },
    }


def _provider_error(exc):
    # Never expose provider exception bodies: they may contain request/account
    # details. Return only a coarse diagnostic category.
    value = (type(exc).__name__ + " " + str(exc)).upper()
    if any(marker in value for marker in ("401", "403", "AUTH", "PERMISSION", "UNAUTHORIZED", "FORBIDDEN")):
        return "AUTH_OR_PERMISSION"
    if any(marker in value for marker in ("402", "PAYMENT", "ENTITLE", "CREDIT", "BILLING")):
        return "PAYMENT_OR_ENTITLEMENT"
    if any(marker in value for marker in ("TIMEOUT", "CONNECT", "NETWORK", "DNS")):
        return "NETWORK_OR_TIMEOUT"
    if any(marker in value for marker in ("400", "404", "422", "SYMBOL", "SCHEMA", "REQUEST")):
        return "REQUEST_REJECTED"
    return "PROVIDER_ERROR"


def snapshot(now, minutes=2, delay_minutes=30, client=None):
    """Return a bounded delayed GC order-flow sample without mutating bot state."""
    minutes = max(1, min(int(minutes), 5))
    delay_minutes = max(15, min(int(delay_minutes), 240))
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

    end = now - timedelta(minutes=delay_minutes)
    start = end - timedelta(minutes=minutes)
    base["window"] = {"start": start.isoformat(), "end": end.isoformat()}

    try:
        trades = _fetch("trades", start, end, 10000, client)
        books = _fetch("mbp-10", start, end, 10000, client)
        base.update(analyze(trades, books))
        base["status"] = "SHADOW_READY" if trades or books else "SHADOW_NO_DATA"
    except Exception as exc:
        base["status"] = "SHADOW_UNAVAILABLE"
        base["provider_error"] = _provider_error(exc)

    return base
