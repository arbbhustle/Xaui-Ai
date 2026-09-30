"""Optional Firebase push delivery for actionable XAU research signals.

Push delivery is operational only: it never changes the immutable strategy,
research projection, or evidence database.
"""
from pathlib import Path
import logging
import os
import threading

TOPIC = "xau_signals"
DEFAULT_CREDENTIAL = "/etc/secrets/firebase-service-account.json"
log = logging.getLogger("xau.fcm")


class XauPushNotifier:
    def __init__(self, view, clock, interval=15):
        self.view = view
        self.clock = clock
        self.interval = interval
        self.stop = threading.Event()
        self.thread = None
        self.last_key = None
        self.failure = None

    def _credential_path(self):
        return Path(os.environ.get("FIREBASE_SERVICE_ACCOUNT_PATH", DEFAULT_CREDENTIAL))

    def configured(self):
        return self._credential_path().is_file()

    def _send(self, signal):
        import firebase_admin
        from firebase_admin import credentials, messaging

        if not firebase_admin._apps:
            firebase_admin.initialize_app(credentials.Certificate(str(self._credential_path())))
            log.warning("FCM_INITIALIZED topic=%s", TOPIC)

        direction = signal["direction"]
        entry = signal.get("entry")
        body = "XAU/USD " + direction
        if entry is not None:
            body += " · Entry " + format(float(entry), ".2f")
        message = messaging.Message(
            topic=TOPIC,
            data={
                "direction": direction,
                "entry": "" if entry is None else str(entry),
                "body": body,
                "signal_candle_close": str(signal.get("signal_candle_close") or ""),
                "expires_at": str(signal.get("expires_at") or ""),
            },
            android=messaging.AndroidConfig(priority="high"),
        )
        message_id = messaging.send(message)
        log.warning("FCM_SENT id=%s direction=%s entry=%s", message_id, direction, entry)
        return message_id

    @staticmethod
    def key(signal):
        direction = signal.get("direction")
        if direction not in ("BUY", "SELL"):
            return None
        return "|".join((
            direction,
            str(signal.get("signal_candle_close") or ""),
            str(signal.get("expires_at") or ""),
        ))

    def check_once(self):
        if not self.configured():
            self.failure = "FCM_NOT_CONFIGURED"
            log.error("FCM_NOT_CONFIGURED credential_path=%s", self._credential_path())
            return "NOT_CONFIGURED"
        signal = self.view(self.clock())
        key = self.key(signal)
        if not key:
            return "NO_SIGNAL"
        if key == self.last_key:
            return "DUPLICATE"
        log.warning("FCM_SIGNAL direction=%s entry=%s key=%s", signal.get("direction"), signal.get("entry"), key)
        try:
            self._send(signal)
        except Exception as exc:
            self.failure = "FCM_SEND_FAILED"
            log.exception("FCM_SEND_FAILED type=%s error=%s", type(exc).__name__, exc)
            return self.failure
        self.last_key = key
        self.failure = None
        return "SENT"

    def start(self):
        if self.thread is not None:
            return
        if not self.configured():
            self.failure = "FCM_NOT_CONFIGURED"
            log.error("FCM_DISABLED credential_missing path=%s", self._credential_path())
            return
        log.warning("FCM_NOTIFIER_STARTED topic=%s interval_seconds=%s", TOPIC, self.interval)
        def loop():
            while not self.stop.is_set():
                try:
                    self.check_once()
                except Exception as exc:
                    self.failure = "FCM_LOOP_FAILED"
                    log.exception("FCM_LOOP_FAILED type=%s error=%s", type(exc).__name__, exc)
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=loop, name="xau-fcm", daemon=True)
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
        log.warning("FCM_NOTIFIER_STOPPED")
