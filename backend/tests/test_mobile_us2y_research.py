from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest

from backend.domain import stamp
from backend.mobile.research_us2y import parse_us2y
from test_phase1 import NOW


def us2y_payload(rows):
    return {
        "meta": {
            "symbol": "US2Y",
            "interval": "1min",
            "exchange_timezone": "America/New_York",
            "type": "Bond",
        },
        "values": rows,
        "status": "ok",
    }


def provider_datetime(utc_time):
    return (
        utc_time.astimezone(ZoneInfo("America/New_York"))
        .replace(tzinfo=None)
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def test_us2y_research_parses_closed_candle():
    opened = NOW.replace(second=0, microsecond=0) - timedelta(minutes=2)

    result = parse_us2y(
        us2y_payload(
            [
                {
                    "datetime": provider_datetime(opened),
                    "open": "4.75",
                    "high": "4.76",
                    "low": "4.74",
                    "close": "4.7583",
                    "volume": "0",
                }
            ]
        ),
        NOW,
    )

    assert result["status"] == "OK"
    assert result["data_mode"] == "RESEARCH_ONLY"
    assert result["channel"] == "us2y"
    assert result["symbol"] == "US2Y"
    assert result["records"][0]["instrument"] == "US2Y"
    assert result["records"][0]["value"] == 4.7583
    assert result["records"][0]["observed_at"] == stamp(opened + timedelta(minutes=1))


def test_us2y_research_drops_forming_candle():
    closed = NOW.replace(second=0, microsecond=0) - timedelta(minutes=2)
    forming = NOW.replace(second=0, microsecond=0)

    result = parse_us2y(
        us2y_payload(
            [
                {
                    "datetime": provider_datetime(forming),
                    "open": "4.80",
                    "high": "4.81",
                    "low": "4.79",
                    "close": "4.80",
                    "volume": "0",
                },
                {
                    "datetime": provider_datetime(closed),
                    "open": "4.75",
                    "high": "4.76",
                    "low": "4.74",
                    "close": "4.7583",
                    "volume": "0",
                },
            ]
        ),
        NOW,
    )

    assert len(result["records"]) == 1
    assert result["records"][0]["value"] == 4.7583


@pytest.mark.parametrize(
    "field,value",
    [
        ("symbol", "US10Y"),
        ("interval", "5min"),
        ("type", "Common Stock"),
    ],
)
def test_us2y_research_rejects_wrong_instrument(field, value):
    payload = us2y_payload(
        [
            {
                "datetime": provider_datetime(
                    NOW.replace(second=0, microsecond=0) - timedelta(minutes=2)
                ),
                "open": "4.75",
                "high": "4.76",
                "low": "4.74",
                "close": "4.7583",
                "volume": "0",
            }
        ]
    )

    payload["meta"][field] = value

    with pytest.raises(ValueError):
        parse_us2y(payload, NOW)