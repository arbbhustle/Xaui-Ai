from datetime import datetime, timezone

from backend.mobile.orderflow_shadow import analyze, snapshot


NOW = datetime(2026, 10, 28, 12, 0, tzinfo=timezone.utc)


def test_orderflow_analysis_is_shadow_only_math():
    trades = [
        {"side": "B", "size": 5},
        {"side": "B", "size": 3},
        {"side": "A", "size": 2},
        {"side": "N", "size": 7},
    ]
    book = {
        "bid_sz_00": 10,
        "bid_sz_01": 10,
        "ask_sz_00": 5,
        "ask_sz_01": 5,
    }
    value = analyze(trades, [book])
    assert value["trade_flow"]["delta"] == 6
    assert value["trade_flow"]["delta_ratio"] == 0.6
    assert value["book_flow"]["average_top10_imbalance"] > 0
    assert value["pressure"]["label"] == "BULLISH"
    assert value["pressure"]["validated_signal"] is False


def test_orderflow_snapshot_is_inert_without_key(monkeypatch):
    monkeypatch.delenv("DATABENTO_API_KEY", raising=False)
    value = snapshot(NOW)
    assert value["status"] == "NOT_CONFIGURED"
    assert value["credential_configured"] is False
    assert value["brain_influence"] == "NONE"
    assert value["execution_influence"] == "NONE"
    assert "window" not in value
