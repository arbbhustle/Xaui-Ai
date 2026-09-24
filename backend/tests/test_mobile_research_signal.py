from datetime import timedelta

from backend.domain import stamp
from backend.mobile.research_signal import compose_research_signal
from test_phase1 import NOW


def xau_candidate(direction):
    return {
        "candidate_direction": direction,
        "direction": "NO_TRADE",
        "entry": 4300.0,
        "expires_at": stamp(NOW + timedelta(minutes=4)),
        "buy_score": 72.5,
        "sell_score": 27.5,
        "components": {"technical": {"buy": 80.0, "sell": 20.0, "weight": 0.3}},
        "hidden_state": {"state": "EXPANSION", "dgfe": {"directional_pressure": 61.0}},
        "analytics_context": {"hidden_state": "EXPANSION"},
        "veto_codes": [
            "MISSING_CRITICAL_INTELLIGENCE:usd",
            "MISSING_CRITICAL_INTELLIGENCE:yields",
            "MISSING_CRITICAL_INTELLIGENCE:news",
            "MISSING_CRITICAL_EVENT_DATA",
            "INSUFFICIENT_CALIBRATION",
        ],
    }


def test_xau_buy_creates_research_setup():
    result = compose_research_signal(xau_candidate("BUY"), NOW)
    assert result["direction"] == "BUY"
    assert result["action"] == "BUY RESEARCH SETUP"
    assert result["execution"] == "DEMO_ONLY"
    assert result["research_only"] is True
    assert result["predictive_edge_established"] is False
    assert result["veto_codes"] == []
    assert result["buy_score"] == 72.5
    assert result["components"]["technical"]["buy"] == 80.0
    assert result["hidden_state"]["state"] == "EXPANSION"
    assert result["analytics_context"]["hidden_state"] == "EXPANSION"


def test_xau_sell_creates_research_setup():
    result = compose_research_signal(xau_candidate("SELL"), NOW)
    assert result["direction"] == "SELL"
    assert result["action"] == "SELL RESEARCH SETUP"
    assert result["veto_codes"] == []


def test_real_xau_technical_veto_is_not_ignored():
    xau = xau_candidate("BUY")
    xau["veto_codes"].append("TIMEFRAME_DISAGREEMENT:15m")
    result = compose_research_signal(xau, NOW)
    assert result["direction"] == "NO_TRADE"
    assert "TIMEFRAME_DISAGREEMENT:15m" in result["veto_codes"]


def test_expired_xau_candidate_is_blocked():
    xau = xau_candidate("BUY")
    xau["expires_at"] = stamp(NOW - timedelta(seconds=1))
    result = compose_research_signal(xau, NOW)
    assert result["direction"] == "NO_TRADE"
    assert "XAU_SIGNAL_EXPIRED" in result["veto_codes"]


def test_opposing_liquidity_sweep_blocks_chasing_sell():
    xau = xau_candidate("SELL")
    xau["hidden_state"] = {
        "state": "LIQUIDITY_SWEEP",
        "dgfe": {"directional_pressure": -60.0},
        "liquidity": {"reclaim_direction": 1},
    }
    result = compose_research_signal(xau, NOW)
    assert result["direction"] == "NO_TRADE"
    assert "OPPOSING_LIQUIDITY_SWEEP" in result["veto_codes"]


def test_same_direction_liquidity_sweep_does_not_block_sell():
    xau = xau_candidate("SELL")
    xau["hidden_state"] = {
        "state": "LIQUIDITY_SWEEP",
        "dgfe": {"directional_pressure": -60.0},
        "liquidity": {"reclaim_direction": -1},
    }
    result = compose_research_signal(xau, NOW)
    assert result["direction"] == "SELL"
    assert "OPPOSING_LIQUIDITY_SWEEP" not in result["veto_codes"]


def test_opposing_exhaustion_pressure_blocks_chasing_buy():
    xau = xau_candidate("BUY")
    xau["hidden_state"] = {
        "state": "EXHAUSTION",
        "dgfe": {"directional_pressure": -20.0},
        "liquidity": {"reclaim_direction": 0},
    }
    result = compose_research_signal(xau, NOW)
    assert result["direction"] == "NO_TRADE"
    assert "OPPOSING_EXHAUSTION_PRESSURE" in result["veto_codes"]
