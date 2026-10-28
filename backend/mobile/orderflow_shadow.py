"""Databento GC order-flow diagnostics isolated from the XAU decision engine.

Historical/delayed data only. This module is deliberately on-demand and read-only:
it never changes Champion/Research scoring, vetoes, entries, or execution state.
"""
from datetime import timedelta
import json
import os

import httpx

DATASET = "GLBX.MDP3"
SYMBOL = "GC.v.0"
ROLE = "SHADOW_ONLY_NO_SIGNAL_INFLUENCE"
URL = "https://hist.databento.com/v0/timeseries.get_range"


def _configured():
    return bool(os.environ.get("DATABENTO_API_KEY", "").strip())


def _records(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        value = json.loads(line)
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _fetch(schema, start, end, limit, client=None):
    key = os.environ.get("DATABENTO_API_KEY", "").strip()
    if not key:
        raise RuntimeError("DATABENTO_NOT_CONFIGURED")
    owned = client is None
    client = client or httpx.Client(timeout=20.0)
    try:
        response = client.get(
            URL,
            auth=(key, ""),
            params={
                "dataset": DATASET,
                "schema": schema,
                "symbols": SYMBOL,
                "stype_in": "continuous",
                "start": start.isoformat(),
                "end": end.isoformat(),
                "encoding": "json",
                "compression": "none",
                "pretty_px": "true",
                "pretty_ts": "true",
                "map_symbols": "true",
                "limit": int(limit),
            },
        )
        response.raise_for_status()
        return _records(response.text)
    finally:
        if owned:
            client.close()


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
    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        base["status"] = "SHADOW_UNAVAILABLE"
        base["provider_error"] = (
            "AUTH_OR_PERMISSION" if code in (401, 403)
            else "PAYMENT_OR_ENTITLEMENT" if code == 402
            else "REQUEST_REJECTED" if 400 <= code < 500
            else "PROVIDER_ERROR"
        )
    except (httpx.TimeoutException, httpx.TransportError):
        base["status"] = "SHADOW_UNAVAILABLE"
        base["provider_error"] = "NETWORK_OR_TIMEOUT"
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        base["status"] = "SHADOW_UNAVAILABLE"
        base["provider_error"] = "INVALID_PROVIDER_RESPONSE"

    return base
