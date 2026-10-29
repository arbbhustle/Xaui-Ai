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


class _FakeStore:
    def __init__(self, rows):
        self._rows = rows
    def to_df(self):
        import pandas as pd
        return pd.DataFrame(self._rows)


class _FakeTimeseries:
    def __init__(self, fail_schema=None):
        self.fail_schema = fail_schema
    def get_range(self, **kwargs):
        schema = kwargs["schema"]
        if schema == self.fail_schema:
            raise RuntimeError("429 rate limited")
        if schema == "trades":
            return _FakeStore([{"side":"B","size":4},{"side":"A","size":1}])
        return _FakeStore([{"bid_sz_00":5,"ask_sz_00":3}])


class _FakeClient:
    def __init__(self, fail_schema=None):
        self.timeseries = _FakeTimeseries(fail_schema)


def test_orderflow_partial_when_book_feed_fails(monkeypatch):
    monkeypatch.setenv("DATABENTO_API_KEY", "test-key")
    value = snapshot(NOW, client=_FakeClient("mbp-10"))
    assert value["status"] == "SHADOW_PARTIAL"
    assert value["feeds"]["trades"]["status"] == "READY"
    assert value["feeds"]["mbp10"]["status"] == "UNAVAILABLE"
    assert value["feeds"]["mbp10"]["error"] == "RATE_LIMITED"
    assert value["pressure"]["basis"] == "TRADES_ONLY_PARTIAL"
    assert value["brain_influence"] == "NONE"
