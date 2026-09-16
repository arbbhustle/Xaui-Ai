"""Minute-aligned worker with a process lock and bounded shutdown."""
from datetime import datetime, timezone
from pathlib import Path
import logging
import os
import threading

from .domain import canonical, stamp
from .storage import Store

log = logging.getLogger("dardania.worker")


class WorkerLock:
    def __init__(self, path):
        self.path = Path(path)
        self.handle = None

    def __enter__(self):
        self.handle = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                if not self.handle.read(1):
                    self.handle.write(b"0")
                    self.handle.flush()
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            raise RuntimeError("Only one monitor process may use this database") from None
        return self

    def __exit__(self, *args):
        self.handle.close()


def seconds_to_next_tick(now):
    # Ten-second settlement allowance after each UTC minute boundary.
    delay = (10 - now.timestamp() % 60) % 60
    return delay if delay > 0 else 60


class Monitor:
    def __init__(self, engine, feed, clock=lambda: datetime.now(timezone.utc)):
        self.engine, self.feed, self.clock = engine, feed, clock
        self.stop_event = threading.Event()
        self.thread = None
        self.lock = WorkerLock(engine.store.path + ".worker.lock")

    def run_once(self):
        try:
            frames, errors = self.feed.fetch(self.clock())
            self.engine.tick(frames, self.clock(), errors)
        except Exception as exc:
            # Exceptions may contain API tokens. Emit only the class and event code.
            log.error(canonical({"event": "monitor_failed", "at": stamp(self.clock()),
                                 "error_type": type(exc).__name__}))
            try:
                with self.engine.store.transaction() as conn:
                    Store.set_state(conn, "health", {
                        "observed_at": stamp(self.clock()), "status": "ERROR",
                        "data_errors": ["MONITOR_FAILURE"], "risk_codes": ["MONITOR_FAILURE"],
                    })
            except Exception:
                log.error(canonical({"event": "health_write_failed"}))

    def _run(self):
        while not self.stop_event.is_set():
            self.run_once()
            self.stop_event.wait(seconds_to_next_tick(self.clock()))

    def start(self):
        self.lock.__enter__()
        self.thread = threading.Thread(target=self._run, name="demo-monitor", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=45)
            if self.thread.is_alive():
                # Keep the ownership lock until process exit if shutdown cannot finish.
                raise RuntimeError("Monitor did not stop within shutdown budget")
        self.lock.__exit__()
