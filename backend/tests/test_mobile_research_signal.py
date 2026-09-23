from datetime import timedelta

from backend.domain import stamp
from backend.mobile.research_signal import (
    compose_research_signal,
    us2y_momentum,
)
from test_phase1 import NOW


def us2y_rows(change_bps):
    latest_at = NOW - timedelta(minutes=1)
    anchor_at = latest_at - timedelta(hours=1)

    anchor_value = 4.75
    latest_value = anchor_value + change_bps / 100

    return [
        {
            "observed_at": stamp(anchor_at),
            "retrieved_at": stamp(anchor_at + timedelta(seconds=5)),
            "value": anchor_value,
            "provider": "twelve-data-us2y",
        },
        {
            "observed_at": stamp(latest_at),
            "retrieved_at": stamp(latest_at + timedelta(seconds=5)),
            "value": latest_value,
            "provider": "twelve-data-us2y",
        },
    ]


def xau_candidate(direction):
    return {
        "candidate_direction": direction,
        "direction": "NO_TRADE",
        "entry": 4300.0,
        "expires_at": stamp(NOW + timedelta(minutes=4)),
        "veto_codes": [
            "MISSING_CRITICAL_INTELLIGENCE:usd",
            "MISSING_CRITICAL_INTELLIGENCE:yields",
            "MISSING_CRITICAL_INTELLIGENCE:news",
            "MISSING_CRITICAL_EVENT_DATA",
            "INSUFFICIENT_CALIBRATION",
        ],
    }


def test_us2y_falling_yield_confirms_gold_buy():
    result = us2y_momentum(us2y_rows(-2.0), NOW)

    assert result["status"] == "READY"
    assert result["bias"] == "BUY"
    assert result["change_bps"] == -2.0


def test_us2y_rising_yield_confirms_gold_sell():
    result = us2y_momentum(us2y_rows(2.0), NOW)

    assert result["status"] == "READY"
    assert result["bias"] == "SELL"
    assert result["change_bps"] == 2.0


def test_us2y_small_move_is_neutral():
    result = us2y_momentum(us2y_rows(0.5), NOW)

    assert result["status"] == "READY"
    assert result["bias"] == "NEUTRAL"


def test_xau_buy_and_us2y_buy_create_research_setup():
    result = compose_research_signal(
        xau_candidate("BUY"),
        us2y_rows(-2.0),
        NOW,
    )

    assert result["direction"] == "BUY"
    assert result["action"] == "BUY RESEARCH SETUP"
    assert result["execution"] == "DEMO_ONLY"
    assert result["research_only"] is True
    assert result["predictive_edge_established"] is False
    assert result["veto_codes"] == []


def test_xau_sell_and_us2y_sell_create_research_setup():
    result = compose_research_signal(
        xau_candidate("SELL"),
        us2y_rows(2.0),
        NOW,
    )

    assert result["direction"] == "SELL"
    assert result["action"] == "SELL RESEARCH SETUP"
    assert result["veto_codes"] == []


def test_us2y_conflict_is_diagnostic_only_for_xau_research():
    result = compose_research_signal(
        xau_candidate("BUY"),
        us2y_rows(2.0),
        NOW,
    )

    assert result["direction"] == "BUY"
    assert result["us2y"]["bias"] == "SELL"
    assert "US2Y_CONFLICT" not in result["veto_codes"]


def test_us2y_neutral_is_diagnostic_only_for_xau_research():
    result = compose_research_signal(
        xau_candidate("BUY"),
        us2y_rows(0.5),
        NOW,
    )

    assert result["direction"] == "BUY"
    assert result["us2y"]["bias"] == "NEUTRAL"
    assert "US2Y_NEUTRAL" not in result["veto_codes"]


def test_real_xau_technical_veto_is_not_ignored():
    xau = xau_candidate("BUY")
    xau["veto_codes"].append("TIMEFRAME_DISAGREEMENT:15m")

    result = compose_research_signal(
        xau,
        us2y_rows(-2.0),
        NOW,
    )

    assert result["direction"] == "NO_TRADE"
    assert "TIMEFRAME_DISAGREEMENT:15m" in result["veto_codes"]


def test_expired_xau_candidate_is_blocked():
    xau = xau_candidate("BUY")
    xau["expires_at"] = stamp(NOW - timedelta(seconds=1))

    result = compose_research_signal(
        xau,
        us2y_rows(-2.0),
        NOW,
    )

    assert result["direction"] == "NO_TRADE"
    assert "XAU_SIGNAL_EXPIRED" in result["veto_codes"]


def test_missing_us2y_history_is_diagnostic_only():
    latest = NOW - timedelta(minutes=1)

    rows = [
        {
            "observed_at": stamp(latest),
            "retrieved_at": stamp(latest + timedelta(seconds=5)),
            "value": 4.75,
            "provider": "twelve-data-us2y",
        }
    ]

    result = compose_research_signal(
        xau_candidate("BUY"),
        rows,
        NOW,
    )

    assert result["direction"] == "BUY"
    assert result["us2y"]["status"] == "INSUFFICIENT_HISTORY"
    assert "US2Y_NOT_READY" not in result["veto_codes"]