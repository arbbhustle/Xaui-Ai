"""Persistent US2Y research collector.

Isolated from the production provider configuration.
RESEARCH / DEMO ONLY. No broker or trade execution.
"""

from datetime import datetime, timezone
from pathlib import Path
import os
import sqlite3
import threading

from ..domain import market_closed, parse, stamp
from .research_us2y import fetch_us2y


DEFAULT_RESEARCH_DB = "backend/data/mobile-v2-us2y-research.sqlite3"
US2Y_POLL_SECONDS = 300
US2Y_MAX_AGE_SECONDS = 900

def research_enabled():
    value = os.environ.get(
        "MOBILE_RESEARCH_US2Y_ENABLED",
        "false",
    )

    if value not in ("true", "false"):
        raise RuntimeError(
            "INVALID_RESEARCH_US2Y_FLAG"
        )

    return value == "true"


def configured_research_path(main_path=None):
    configured = os.environ.get("MOBILE_RESEARCH_US2Y_DB_PATH", "").strip()

    if configured:
        path = Path(configured).resolve()
    elif main_path is not None:
        main = Path(main_path).resolve()
        path = main.with_name(main.stem + "-us2y-research.sqlite3")
    else:
        path = Path(DEFAULT_RESEARCH_DB).resolve()

    if os.environ.get("RENDER"):
        disk = Path(os.environ.get("MOBILE_DISK_PATH", "/var/data")).resolve()
        if not path.is_relative_to(disk):
            raise RuntimeError("RESEARCH_DB_MUST_USE_PERSISTENT_DISK")

    return path


class ResearchUS2YRuntime:
    """Owns a separate append-only US2Y research database."""

    def __init__(self, main_path=None, path=None, clock=lambda: datetime.now(timezone.utc)):
        self.clock = clock
        self.enabled = research_enabled()

        self.stop = threading.Event()
        self.thread = None
        self.failure = None
        self._writer = threading.Lock()

        if not self.enabled:
            self.path = None
            return

        self.path = Path(path).resolve() if path else configured_research_path(main_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

        self._initialize()

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize(self):
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_metadata(
                    id INTEGER PRIMARY KEY CHECK(id=1),
                    schema_version INTEGER NOT NULL,
                    mode TEXT NOT NULL
                )
                """
            )

            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS research_us2y_observations(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    value REAL NOT NULL CHECK(value >= -5 AND value <= 30),
                    retrieved_at TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    data_mode TEXT NOT NULL CHECK(data_mode='RESEARCH_ONLY'),
                    UNIQUE(observed_at, value)
                )
                """
            )

            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS
                idx_research_us2y_observed_at
                ON research_us2y_observations(observed_at)
                """
            )

            metadata = conn.execute(
                "SELECT schema_version, mode FROM research_metadata WHERE id=1"
            ).fetchone()

            if metadata:
                if metadata["schema_version"] != 1 or metadata["mode"] != "XAU_US2Y_RESEARCH_ONLY":
                    raise RuntimeError("INCOMPATIBLE_RESEARCH_DATABASE")
            else:
                conn.execute(
                    """
                    INSERT INTO research_metadata(id, schema_version, mode)
                    VALUES(1, 1, 'XAU_US2Y_RESEARCH_ONLY')
                    """
                )

    def once(self):
        if not self.enabled:
            return {"status": "COLLECTION_DISABLED"}
        
        now = self.clock()

        if now.tzinfo is None or now.utcoffset() is None:
            return {"status": "INVALID_CLOCK"}

        if market_closed(now):
            return {"status": "MARKET_CLOSED"}

        try:
            result = fetch_us2y(now=now)

            inserted = 0

            with self._writer:
                with self._connect() as conn:
                    before = conn.total_changes

                    for row in result["records"]:
                        conn.execute(
                            """
                            INSERT OR IGNORE INTO research_us2y_observations(
                                observed_at,
                                value,
                                retrieved_at,
                                provider,
                                data_mode
                            )
                            VALUES(?,?,?,?,?)
                            """,
                            (
                                row["observed_at"],
                                row["value"],
                                result["retrieved_at"],
                                result["provider"],
                                result["data_mode"],
                            ),
                        )

                    inserted = conn.total_changes - before

            self.failure = None

            return {
                "status": "COMPLETE",
                "inserted": inserted,
                "as_of": result["as_of"],
            }

        except Exception:
            # Never expose provider responses, credentials or exception text.
            self.failure = "US2Y_COLLECTION_FAILED"
            return {"status": self.failure}

    def snapshot(self, now=None, limit=120):
        if not self.enabled:
            return []

        now = now or self.clock()

        if not 1 <= limit <= 1000:
            raise ValueError("INVALID_RESEARCH_LIMIT")

        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT r.observed_at, r.value, r.retrieved_at, r.provider
                FROM research_us2y_observations r
                JOIN (
                    SELECT observed_at, MAX(id) AS id
                    FROM research_us2y_observations
                    WHERE observed_at <= ? AND retrieved_at <= ?
                    GROUP BY observed_at
                ) latest ON latest.id = r.id
                ORDER BY r.observed_at DESC
                LIMIT ?
                """,
                (stamp(now), stamp(now), limit),
            ).fetchall()

        return [
            {
                "observed_at": row["observed_at"],
                "value": row["value"],
                "retrieved_at": row["retrieved_at"],
                "provider": row["provider"],
            }
            for row in reversed(rows)
        ]

    def status(self, now=None):
        if not self.enabled:
            return {
                "status": "DISABLED",
                "freshness": "UNAVAILABLE",
                "age_seconds": None,
                "last_observed_at": None,
                "credential_configured": bool(
                    os.environ.get("TWELVE_DATA_API_KEY", "").strip()
                ),
                "mode": "RESEARCH_ONLY",
                "channel": "us2y",
                "symbol": "US2Y",
                "last_attempt_status": "COLLECTION_DISABLED",
            }

        now = now or self.clock()
        rows = self.snapshot(now, 1)

        if not rows:
            return {
                "status": "UNAVAILABLE",
                "freshness": "UNAVAILABLE",
                "age_seconds": None,
                "last_observed_at": None,
                "credential_configured": bool(
                    os.environ.get("TWELVE_DATA_API_KEY", "").strip()
                ),
                "mode": "RESEARCH_ONLY",
                "channel": "us2y",
                "symbol": "US2Y",
            }

        latest = rows[-1]
        age = (now - parse(latest["observed_at"])).total_seconds()
        fresh = 0 <= age < US2Y_MAX_AGE_SECONDS and not self.failure

        return {
            "status": "HEALTHY" if fresh else "UNAVAILABLE",
            "freshness": "FRESH" if fresh else "STALE",
            "age_seconds": age,
            "last_observed_at": latest["observed_at"],
            "value": latest["value"],
            "credential_configured": bool(
                os.environ.get("TWELVE_DATA_API_KEY", "").strip()
            ),
            "mode": "RESEARCH_ONLY",
            "channel": "us2y",
            "symbol": "US2Y",
            "last_attempt_status": self.failure or "COMPLETE",
        }

    def start(self):
        if not self.enabled:
            return

        if self.thread is not None:
            return

        def loop():
            while not self.stop.is_set():
                now = self.clock()

                if not market_closed(now):
                    self.once()

                now = self.clock()
                epoch = now.timestamp()

                # One request each 5 minutes, shortly after candle close.
                next_tick = ((int(epoch) // US2Y_POLL_SECONDS) + 1) * US2Y_POLL_SECONDS + 8
                self.stop.wait(max(1, next_tick - epoch))

        self.thread = threading.Thread(
            target=loop,
            name="research-us2y",
            daemon=True,
        )
        self.thread.start()

    def close(self):
        self.stop.set()

        if self.thread:
            self.thread.join()
