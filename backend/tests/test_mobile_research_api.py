from datetime import timedelta

from backend.domain import stamp
from backend.mobile.research_api import (
    research_view,
    research_xau_freshness_errors,
    research_history,
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

    result = research_view(FakeRuntime(), None, NOW)

    assert result["direction"] == "BUY"
    assert result["action"] == "BUY RESEARCH SETUP"
    assert result["mode"] == "XAU_ONLY_RESEARCH"
    assert result["execution"] == "DEMO_ONLY"
    assert result["strict_signal_unchanged"] is True
    assert result["automatic_promotion"] is False
    assert result["predictive_edge_established"] is False


def test_research_api_sell_setup(monkeypatch):
    monkeypatch.setattr(
        "backend.mobile.research_api.latest_xau_candidate",
        lambda runtime, now: xau_candidate("SELL"),
    )

    result = research_view(FakeRuntime(), None, NOW)

    assert result["direction"] == "SELL"
    assert result["action"] == "SELL RESEARCH SETUP"


def test_research_api_missing_xau_blocks(monkeypatch):
    monkeypatch.setattr(
        "backend.mobile.research_api.latest_xau_candidate",
        lambda runtime, now: None,
    )

    result = research_view(FakeRuntime(), None, NOW)

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

class HistoryConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params):
        now = params[0]
        before = params[1] if len(params) > 1 else None
        eligible = [
            row for row in self.rows
            if row["at"] <= now and (before is None or row["seq"] < before)
        ]
        eligible.sort(key=lambda row: row["seq"], reverse=True)
        return HistoryRows(eligible[:500])


class HistoryRows(list):
    def fetchall(self):
        return list(self)


class HistoryStore:
    @staticmethod
    def get(conn, payload_id):
        seq = int(payload_id.split("-")[-1])
        if seq == 100:
            champion = xau_candidate("SELL")
            champion["signal_candle_close"] = stamp(NOW - timedelta(hours=20))
            champion["expires_at"] = stamp(NOW - timedelta(hours=19, minutes=55))
            champion["veto_codes"] = ["INSUFFICIENT_CALIBRATION"]
        else:
            champion = xau_candidate("NO_TRADE")
        return {"champion": champion}


class HistoryRuntime:
    def __init__(self):
        # 600 newer NO_TRADE evaluations put the actionable SELL beyond the
        # old 500-row scan window.
        self.rows = [
            {"seq": seq, "at": stamp(NOW - timedelta(minutes=(700 - seq) * 2)), "payload_id": f"event-{seq}"}
            for seq in range(100, 701)
        ]
        self.store = HistoryStore()

    class _Read:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self):
            return self.conn

        def __exit__(self, *args):
            return False

    def read(self):
        return self._Read(HistoryConnection(self.rows))


def test_research_history_pages_past_500_quiet_evaluations():
    history = research_history(HistoryRuntime(), NOW, limit=30)

    assert len(history["items"]) == 1
    assert history["items"][0]["direction"] == "SELL"
    assert history["items"][0]["cursor"] == 100


class EarlyReversalHistoryStore:
    @staticmethod
    def get(conn, payload_id):
        champion = xau_candidate("NO_TRADE")
        champion["entry"] = 4288.15
        champion["veto_codes"] = ["INSUFFICIENT_SCORE", "INSUFFICIENT_CALIBRATION"]
        champion["timeframes"] = {
            "1min": {"bias": "SELL", "bias_score": -0.32},
            "5min": {
                "bias": "SELL",
                "bias_score": -0.27,
                "momentum": -0.35,
                "market_structure": -0.25,
            },
        }
        champion["hidden_state"] = {
            "state": "EXPANSION",
            "dgfe": {"directional_pressure": -40.0},
            "liquidity": {"reclaim_direction": 0, "displacement": 20.0},
        }
        return {"champion": champion}


class EarlyReversalHistoryRuntime(HistoryRuntime):
    def __init__(self):
        self.rows = [{
            "seq": 1,
            "at": stamp(NOW - timedelta(minutes=1)),
            "payload_id": "event-1",
        }]
        self.store = EarlyReversalHistoryStore()


def test_research_history_archives_early_reversal_setup():
    history = research_history(EarlyReversalHistoryRuntime(), NOW, limit=30)

    assert len(history["items"]) == 1
    assert history["items"][0]["direction"] == "SELL"
    assert history["items"][0]["entry"] == 4288.15
    assert history["items"][0]["early_reversal"] is True
