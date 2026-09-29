from datetime import datetime, timezone

from backend.mobile.push import XauPushNotifier


NOW = datetime(2026, 9, 29, tzinfo=timezone.utc)


def notifier(signal):
    return XauPushNotifier(lambda now: signal, lambda: NOW, interval=1)


def test_push_key_matches_android_dedupe_contract():
    n = notifier({})
    signal = {
        "direction": "SELL",
        "signal_candle_close": "2026-09-29T20:30:00Z",
        "expires_at": "2026-09-29T20:45:00Z",
    }
    assert n.key(signal) == "SELL|2026-09-29T20:30:00Z|2026-09-29T20:45:00Z"
    assert n.key({"direction": "NO_TRADE"}) is None


def test_push_dedupes_only_after_success(monkeypatch):
    signal = {
        "direction": "BUY",
        "entry": 4158.67,
        "signal_candle_close": "2026-09-29T13:10:00Z",
        "expires_at": "2026-09-29T13:25:00Z",
    }
    n = notifier(signal)
    monkeypatch.setattr(n, "configured", lambda: True)
    sent = []
    monkeypatch.setattr(n, "_send", lambda value: sent.append(value))
    assert n.check_once() == "SENT"
    assert n.check_once() == "DUPLICATE"
    assert len(sent) == 1


def test_push_retries_same_signal_after_send_failure(monkeypatch):
    signal = {
        "direction": "SELL",
        "signal_candle_close": "2026-09-29T15:20:00Z",
        "expires_at": "2026-09-29T15:35:00Z",
    }
    n = notifier(signal)
    monkeypatch.setattr(n, "configured", lambda: True)
    attempts = []
    def send(value):
        attempts.append(value)
        if len(attempts) == 1:
            raise RuntimeError("network")
    monkeypatch.setattr(n, "_send", send)
    assert n.check_once() == "FCM_SEND_FAILED"
    assert n.check_once() == "SENT"
    assert len(attempts) == 2
