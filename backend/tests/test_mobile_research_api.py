from datetime import timedelta

from backend.domain import stamp
from backend.mobile.research_api import (
    research_view,
    research_xau_freshness_errors,
)
from test_phase1 import NOW


class FakeCollector:
    def status(self, now):
        return {
            "status": "HEALTHY",
            "freshness": "FRESH",
            "age_seconds": 60,
            "data_mode": "LIVE_DATA",
        }


class FakeRuntime:
    def __init__(self):
        self.collector = FakeCollector()


class FakeResearchRuntime:
    def __init__(self, change_bps=-2.0):
        self.change_bps = change_bps

    def snapshot(self, now=None, limit=120):
        latest = now - timedelta(minutes=1)
        anchor = latest - timedelta(hours=1)

        anchor_value = 4.75
        latest_value = anchor_value + self.change_bps / 100

        return [
            {
                "observed_at": stamp(anchor),
                "retrieved_at": stamp(anchor + timedelta(seconds=5)),
                "value": anchor_value,
                "provider": "twelve-data-us2y",
            },
            {
                "observed_at": stamp(latest),
                "retrieved_at": stamp(latest + timedelta(seconds=5)),
                "value": latest_value,
                "provider": "twelve-data-us2y",
            },
        ]

    def status(self, now=None):
        rows = self.snapshot(now, 120)

        return {
            "status": "HEALTHY",
            "freshness": "FRESH",
            "age_seconds": 60,
            "value": rows[-1]["value"],
            "mode": "RESEARCH_ONLY",
            "channel": "us2y",
            "symbol": "US2Y",
        }


def xau_candidate(direction="BUY"):
    return {
        "candidate_direction": direction,
        "direction": "NO_TRADE",
        "entry": 4300.0,
        "expires_at": stamp(NOW + timedelta(minutes=4)),
        "signal_candle_close": stamp(NOW - timedelta(minutes=1)),
        "buy_score": 82.0 if direction == "BUY" else 18.0,
        "sell_score": 18.0 if direction == "BUY" else 82.0,
        "source_candle_closes": {
            "1min": stamp(NOW - timedelta(seconds=60)),
            "5min": stamp(NOW - timedelta(minutes=5)),
            "15min": stamp(NOW - timedelta(minutes=15)),
            "1h": stamp(NOW - timedelta(hours=1)),
            "4h": stamp(NOW - timedelta(hours=4)),
        },
        "veto_codes": [
            "MISSING_CRITICAL_INTELLIGENCE:usd",
            "MISSING_CRITICAL_INTELLIGENCE:yields",
            "MISSING_CRITICAL_INTELLIGENCE:news",
            "MISSING_CRITICAL_EVENT_DATA",
            "INSUFFICIENT_CALIBRATION",
        ],
    }


def test_research_api_buy_setup(monkeypatch):
    monkeypatch.setattr(
        "backend.mobile.research_api.latest_xau_candidate",
        lambda runtime, now: xau_candidate("BUY"),
    )

    result = research_view(
        FakeRuntime(),
        FakeResearchRuntime(change_bps=-2.0),
        NOW,
    )

    assert result["direction"] == "BUY"
    assert result["action"] == "BUY RESEARCH SETUP"
    assert result["mode"] == "XAU_US2Y_RESEARCH_ONLY"
    assert result["execution"] == "DEMO_ONLY"
    assert result["strict_signal_unchanged"] is True
    assert result["automatic_promotion"] is False
    assert result["predictive_edge_established"] is False


def test_research_api_sell_setup(monkeypatch):
    monkeypatch.setattr(
        "backend.mobile.research_api.latest_xau_candidate",
        lambda runtime, now: xau_candidate("SELL"),
    )

    result = research_view(
        FakeRuntime(),
        FakeResearchRuntime(change_bps=2.0),
        NOW,
    )

    assert result["direction"] == "SELL"
    assert result["action"] == "SELL RESEARCH SETUP"


def test_research_api_conflict_blocks(monkeypatch):
    monkeypatch.setattr(
        "backend.mobile.research_api.latest_xau_candidate",
        lambda runtime, now: xau_candidate("BUY"),
    )

    result = research_view(
        FakeRuntime(),
        FakeResearchRuntime(change_bps=2.0),
        NOW,
    )

    assert result["direction"] == "NO_TRADE"
    assert "US2Y_CONFLICT" in result["veto_codes"]


def test_research_api_missing_xau_blocks(monkeypatch):
    monkeypatch.setattr(
        "backend.mobile.research_api.latest_xau_candidate",
        lambda runtime, now: None,
    )

    result = research_view(
        FakeRuntime(),
        FakeResearchRuntime(change_bps=-2.0),
        NOW,
    )

    assert result["direction"] == "NO_TRADE"
    assert "NO_XAU_DECISION" in result["veto_codes"]


def test_mobile_xau_240_second_research_window():
    closes = {
        "1min": stamp(NOW - timedelta(seconds=190)),
        "5min": stamp(NOW),
        "15min": stamp(NOW),
        "1h": stamp(NOW),
        "4h": stamp(NOW),
    }

    assert "STALE_DATA:1min" not in research_xau_freshness_errors(
        closes,
        NOW,
    )

    closes["1min"] = stamp(
        NOW - timedelta(seconds=241)
    )

    assert "STALE_DATA:1min" in research_xau_freshness_errors(
        closes,
        NOW,
    )