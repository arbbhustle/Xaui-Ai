"""Phase 2 strategy adapter over the Phase 1 transaction and demo simulator."""
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import json

from .council import CouncilPolicy, MODEL_VERSION, VERSION, evaluate_council
from .calibration import bucket
from .domain import Policy, digest, parse, stamp
from .engine import Engine

SOURCE_FILES = ("scoring.py", "domain.py", "engine.py", "storage.py",
                "council.py", "calibration.py", "phase2.py")


def implementation_hash(root=None):
    root = Path(root) if root is not None else Path(__file__).parent
    return digest({name: (root / name).read_text(encoding="utf-8") for name in SOURCE_FILES})


class Phase2Engine(Engine):
    strategy_version = VERSION
    replay_fields = Engine.replay_fields + (
        "components", "calibration", "confidence_kind", "calibrated_confidence",
        "raw_score", "model_version", "model_identity", "warmup_trade", "reasons", "mode",
        "edge", "atr", "rsi_state", "action", "strategy_version", "engine", "symbol",
        "timeframe", "market_source", "data_status", "risk_veto", "signal_candle_close",
        "expires_at", "decision_type", "source_candle_closes", "session", "timestamp_utc",
        "monitor_price", "monitor_candle_close")

    def __init__(self, store, policy=Policy(), council_policy=CouncilPolicy()):
        super().__init__(store, policy)
        self.council_policy = council_policy
        self.code_hash = implementation_hash()
        self.model_identity = digest({"code_hash": self.code_hash, "model": MODEL_VERSION,
                                      "policy": asdict(policy), "council_policy": asdict(council_policy)})
        with store.connect() as conn:
            mixed = conn.execute("""SELECT 1 FROM decisions
                WHERE coalesce(json_extract(payload, '$.strategy_version'), '') != ? LIMIT 1""",
                                 (VERSION,)).fetchone()
        if mixed:
            raise RuntimeError("Phase 2 requires a separate demo database; Phase 1 history is preserved")

    def snapshot_context(self, conn, snapshot, checked):
        context = {"council_policy": asdict(self.council_policy), "model_identity": self.model_identity,
                   "calibration_samples": []}
        if not checked["5min"]:
            return context
        cutoff = parse(checked["5min"][-1].t) + timedelta(minutes=5)
        # Candidate scores do not depend on calibration. Select the bounded
        # history within its bin so other directions cannot evict its evidence.
        candidate = self.evaluate_snapshot(dict(snapshot, **context), self.policy)
        direction = candidate["candidate_direction"]
        if direction == "NO_TRADE":
            return context
        lower = bucket(candidate["raw_score"]) * 20
        upper = 101 if lower == 80 else lower + 20
        # Require an actual journalled closure, not merely a mutable trade row.
        # Both economic close and first recorded knowledge must precede cutoff.
        rows = conn.execute("""
            SELECT t.id, e.payload AS trade, d.payload AS decision, s.observed_at AS known_at
            FROM trades t JOIN decisions d ON d.id=t.decision_id
            JOIN trade_events e ON e.trade_id=t.id AND e.kind='CLOSED'
            JOIN snapshots s ON s.id=e.snapshot_id
            WHERE t.status='CLOSED'
              AND json_extract(d.payload, '$.model_identity')=?
              AND json_extract(d.payload, '$.candidate_direction')=?
              AND json_extract(d.payload, '$.raw_score') >= ?
              AND json_extract(d.payload, '$.raw_score') < ?
              AND s.observed_at <= ?
              AND json_extract(e.payload, '$.closed_at') <= ?
            ORDER BY json_extract(e.payload, '$.closed_at') DESC, t.id DESC LIMIT ?
            """, (self.model_identity, direction, lower, upper, stamp(cutoff), stamp(cutoff),
                  self.council_policy.calibration_window)).fetchall()
        context["calibration_samples"] = [
            {"trade_id": row["id"], "direction": decision["candidate_direction"],
             "raw_score": decision["raw_score"], "closed_at": trade["closed_at"],
             "known_at": row["known_at"], "win": trade["r_multiple"] > 0}
            for row in reversed(rows)
            for trade, decision in [(json.loads(row["trade"]), json.loads(row["decision"]))]]
        return context

    def evaluate_snapshot(self, snapshot, policy):
        return evaluate_council(snapshot, policy)

    def trade_metadata(self, result):
        return {key: result[key] for key in (
            "strategy_version", "model_version", "model_identity", "components", "raw_score",
            "confidence_kind", "calibrated_confidence", "calibration", "warmup_trade")}
