"""Isolated Twelve Data US2Y research feed.

RESEARCH / DEMO ONLY.
No broker connection and no trade execution.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import math
import os

import httpx

from ..domain import stamp


SYMBOL = "US2Y"
INTERVAL = "1min"
OUTPUTSIZE = 120
URL = "https://api.twelvedata.com/time_series"


def _number(value):
    if isinstance(value, bool):
        raise ValueError("INVALID_US2Y_VALUE")
    number = float(value)
    if not math.isfinite(number) or not -5 <= number <= 30:
        raise ValueError("INVALID_US2Y_VALUE")
    return number


def _provider_time(value, timezone_name):
    if not isinstance(value, str):
        raise ValueError("INVALID_US2Y_TIME")

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(timezone_name))

    return parsed.astimezone(timezone.utc)


def parse_us2y(payload, now):
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        raise ValueError("INVALID_US2Y_RESPONSE")

    meta = payload.get("meta", {})

    if meta.get("symbol") != SYMBOL:
        raise ValueError("WRONG_US2Y_SYMBOL")

    if meta.get("interval") != INTERVAL:
        raise ValueError("WRONG_US2Y_INTERVAL")

    if meta.get("type") != "Bond":
        raise ValueError("WRONG_US2Y_INSTRUMENT")

    timezone_name = meta.get("exchange_timezone")
    if not isinstance(timezone_name, str) or not timezone_name:
        raise ValueError("MISSING_US2Y_TIMEZONE")

    records = []

    for row in payload.get("values", []):
        opened = _provider_time(row["datetime"], timezone_name)
        observed = opened + timedelta(minutes=1)

        # Never use a forming/future candle.
        if observed > now:
            continue

        value = _number(row["close"])

        records.append(
            {
                "id": f"{SYMBOL}:{stamp(observed)}",
                "source": "Twelve Data",
                "url": "https://twelvedata.com",
                "published_at": stamp(observed),
                "observed_at": stamp(observed),
                "instrument": SYMBOL,
                "value": value,
            }
        )

    records.sort(key=lambda row: row["observed_at"])

    if not records:
        raise ValueError("NO_CLOSED_US2Y_DATA")

    if len({row["observed_at"] for row in records}) != len(records):
        raise ValueError("DUPLICATE_US2Y_DATA")

    return {
        "provider": "twelve-data-us2y",
        "channel": "us2y",
        "symbol": SYMBOL,
        "status": "OK",
        "data_mode": "RESEARCH_ONLY",
        "retrieved_at": stamp(now),
        "as_of": records[-1]["observed_at"],
        "records": records,
    }


def fetch_us2y(now=None, environ=None):
    now = now or datetime.now(timezone.utc)
    environ = os.environ if environ is None else environ

    token = environ.get("TWELVE_DATA_API_KEY", "").strip()
    if not token:
        raise RuntimeError("TWELVE_DATA_API_KEY_NOT_CONFIGURED")

    if any(char in token for char in ("\r", "\n", "\x00")):
        raise RuntimeError("INVALID_TWELVE_DATA_API_KEY")

    headers = {"Authorization": "apikey " + token}
    params = {
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": OUTPUTSIZE,
    }

    with httpx.Client(
        timeout=8,
        trust_env=False,
        follow_redirects=False,
    ) as client:
        response = client.get(URL, params=params, headers=headers)

    if response.status_code != 200:
        raise RuntimeError("US2Y_PROVIDER_UNAVAILABLE")

    payload = response.json()

    return parse_us2y(payload, now)