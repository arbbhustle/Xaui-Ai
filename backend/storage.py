"""Transactional SQLite persistence; one host with a durable local disk."""
from contextlib import contextmanager
from pathlib import Path
import json
import sqlite3

from .domain import canonical

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_version VALUES (1);
CREATE TABLE IF NOT EXISTS snapshots (
 id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decisions (
 id TEXT PRIMARY KEY, candle_close TEXT NOT NULL UNIQUE,
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS trades (
 id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE REFERENCES decisions(id),
 status TEXT NOT NULL CHECK(status IN ('PENDING','OPEN','CLOSED','CANCELLED')),
 payload TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_trade ON trades((1))
 WHERE status IN ('PENDING','OPEN');
CREATE TABLE IF NOT EXISTS trade_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, trade_id TEXT NOT NULL REFERENCES trades(id),
 event_key TEXT NOT NULL UNIQUE, occurred_at TEXT NOT NULL, kind TEXT NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            if conn.execute("SELECT name FROM sqlite_master WHERE name='schema_version'").fetchone():
                versions = conn.execute("SELECT version FROM schema_version ORDER BY version").fetchall()
                if [r[0] for r in versions] != [1]:
                    raise RuntimeError("Unsupported database schema")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            versions = conn.execute("SELECT version FROM schema_version").fetchall()
            if [r[0] for r in versions] != [1]:
                raise RuntimeError("Unsupported database schema")

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=FULL")
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self):
        with self.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @staticmethod
    def get_state(conn, key, default=None):
        row = conn.execute("SELECT payload FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def set_state(conn, key, value):
        conn.execute("INSERT INTO state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload",
                     (key, canonical(value)))

    @staticmethod
    def active(conn):
        row = conn.execute("SELECT payload FROM trades WHERE status IN ('PENDING','OPEN')").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def save_trade(conn, trade):
        conn.execute("UPDATE trades SET status=?,payload=? WHERE id=?",
                     (trade["status"], canonical(trade), trade["id"]))

    @staticmethod
    def event(conn, trade, kind, at, snapshot_id, details=None):
        key = f"{trade['id']}:{kind}:{at}"
        conn.execute("INSERT OR IGNORE INTO trade_events(trade_id,event_key,occurred_at,kind,snapshot_id,payload) VALUES(?,?,?,?,?,?)",
                     (trade["id"], key, at, kind, snapshot_id, canonical(details or trade)))

    @staticmethod
    def summary(conn):
        rows = conn.execute("SELECT payload FROM trades WHERE status='CLOSED' ORDER BY rowid").fetchall()
        trades = [json.loads(r[0]) for r in rows]
        wins = sum(t["r_multiple"] > 0 for t in trades)
        losses = sum(t["r_multiple"] < 0 for t in trades)
        total_r = sum(t["r_multiple"] for t in trades)
        streak = best = 0
        for trade in trades:
            streak = streak + 1 if trade["r_multiple"] > 0 else 0
            best = max(best, streak)
        active = Store.active(conn)
        return {
            "closed_trades": len(trades), "wins": wins, "losses": losses,
            "win_rate_pct": round(100 * wins / len(trades), 1) if trades else 0.0,
            "total_r": round(total_r, 4),
            "average_r": round(total_r / len(trades), 4) if trades else 0.0,
            "best_win_streak": best,
            "open_trade": active if active and active["status"] == "OPEN" else None,
            "pending_trade": active if active and active["status"] == "PENDING" else None,
            "note": "Persistent demo only; gross candle-based results, not broker fills or calibrated confidence.",
        }

    def backup(self, destination):
        """SQLite online backup includes WAL contents; safe while the worker runs."""
        with self.connect() as conn, sqlite3.connect(str(destination)) as target:
            conn.backup(target)
