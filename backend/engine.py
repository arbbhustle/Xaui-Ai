"""Single-transaction monitor, decision journal, risk gate, and demo simulator."""
from dataclasses import asdict
from datetime import timedelta
from pathlib import Path
import json
import logging

from .domain import (Policy, apply_veto, canonical, closed_frame, digest, evaluate,
                     inspect_frames, parse, stamp, valid_plan, VERSION, allowed_session_gap,
                     safe_input, INTERVALS)
from .storage import Store

log = logging.getLogger("dardania.engine")


def execution_gates(result, context, policy, now):
    episode = dict(context["episode"])
    direction = result["candidate_direction"]
    if result["data_status"] in ("LIVE_DATA", "FIXTURE_DATA", "DELAYED_DATA") and direction != episode["direction"]:
        episode = {"direction": direction, "used": False}
    gates = []
    if context["active_trade_id"]:
        gates.append("POSITION_ALREADY_ACTIVE")
    if context["daily_loss_r"] <= -policy.daily_loss_limit_r:
        gates.append("DAILY_DEMO_LOSS_LIMIT")
    if direction in ("BUY", "SELL") and episode["used"]:
        gates.append("DUPLICATE_SIGNAL_EPISODE")
    if result["expires_at"] and now >= parse(result["expires_at"]):
        gates.append("SIGNAL_EXPIRED")
    return gates, episode


def implementation_hash(root=None):
    root = Path(root) if root is not None else Path(__file__).parent
    # Universal newline decoding makes checkout line endings irrelevant.
    return digest({name: (root / name).read_text(encoding="utf-8") for name in
                   ("scoring.py", "domain.py", "engine.py", "storage.py")})


class Engine:
    strategy_version = VERSION
    replay_fields = ("direction", "candidate_direction", "buy_score", "sell_score", "confidence",
                     "entry", "sl", "tp1", "tp2", "timeframes", "veto_codes")

    def __init__(self, store: Store, policy: Policy = Policy()):
        self.store, self.policy = store, policy
        self.code_hash = implementation_hash()

    def snapshot_context(self, conn, snapshot, checked):
        """Capture any strategy-specific inputs inside the tick transaction."""
        return {}

    def evaluate_snapshot(self, snapshot, policy):
        return evaluate(snapshot, policy)

    def trade_metadata(self, result):
        return {}

    def api_health(self, conn, health, now):
        return health

    def api_decision(self, conn, result, now):
        return result

    def monitor_gates(self, trade, snapshot):
        return []

    def after_tick(self, conn, snapshot, result, now, snapshot_id):
        """Optional isolated journals within the same transaction."""
        return None

    def tick(self, frames: dict, now, source_errors=None):
        snapshot = {
            "observed_at": stamp(now), "frames": safe_input(frames),
            "source_errors": source_errors or {}, "policy": asdict(self.policy),
            "strategy_version": self.strategy_version, "code_hash": self.code_hash,
        }
        checked, problems = inspect_frames(snapshot, self.policy)
        # A transaction serializes overlapping ticks and ensures restart-safe deduplication.
        with self.store.transaction() as conn:
            previous = Store.get_state(conn, "health", {})
            if previous.get("observed_at") and parse(previous["observed_at"]) >= now:
                return None
            snapshot.update(self.snapshot_context(conn, snapshot, checked))
            snapshot_id = digest(snapshot)
            result = self.evaluate_snapshot(snapshot, self.policy)
            if source_errors:
                result = apply_veto(result, ["PROVIDER_ERROR:" + k for k in sorted(source_errors)])
                result["data_status"] = "ERROR"
            conn.execute("INSERT OR IGNORE INTO snapshots VALUES (?,?,?)",
                         (snapshot_id, stamp(now), canonical(snapshot)))
            monitor_problems = []
            trade = Store.active(conn)
            monitor_bars = []
            if trade:
                checkpoint = (parse(trade["last_bar_open"]) + timedelta(minutes=1)
                              if trade["last_bar_open"] else parse(trade["eligible_from"]))
                try:
                    rows = [r for r in snapshot["frames"].get("1min", [])
                            if parse(r["t"]) >= checkpoint]
                    monitor_bars, monitor_problems = closed_frame(
                        rows, "1min", now, minimum=0, continuity=False)
                except (TypeError, KeyError, ValueError, AttributeError):
                    monitor_problems = ["INVALID_DATA:1min"]
            if "1min" in (source_errors or {}):
                monitor_problems.append("PROVIDER_ERROR:1min")
            if trade:
                monitor_problems.extend(self.monitor_gates(trade, snapshot))
            pre_entry_veto = (trade and trade["status"] == "PENDING"
                              and now < parse(trade["eligible_from"])
                              and result["veto_codes"])
            if not monitor_problems or pre_entry_veto:
                monitor_problems += self._monitor(conn, monitor_bars, now, snapshot_id,
                                                   result["veto_codes"])
            if monitor_problems:
                result = apply_veto(result, monitor_problems)
            result["monitor_veto_codes"] = monitor_problems
            close = result["signal_candle_close"]
            existing = conn.execute("SELECT id FROM decisions WHERE candle_close=?", (close,)).fetchone() if close else None
            latest = conn.execute("SELECT candle_close FROM decisions ORDER BY candle_close DESC LIMIT 1").fetchone()
            if close and not existing and (not latest or close > latest[0]):
                result = self._decision(conn, result, now, snapshot_id)
            self.after_tick(conn, snapshot, result, now, snapshot_id)
            health = {
                "observed_at": stamp(now), "snapshot_id": snapshot_id,
                "data_errors": sorted(set(problems + monitor_problems +
                                          ["PROVIDER_ERROR:" + k for k in (source_errors or {})])),
                "risk_codes": result["veto_codes"],
                "monitor_candle_close": result.get("monitor_candle_close"),
                "source_candle_closes": {
                    k: stamp(parse(v[-1].t) + timedelta(seconds=INTERVALS[k]))
                    for k, v in checked.items() if v},
                "status": "DEGRADED" if problems or monitor_problems or source_errors else "OK",
            }
            Store.set_state(conn, "health", health)
        log.info(canonical({"event": "monitor_tick", "snapshot_id": snapshot_id,
                            "at": stamp(now), "status": health["status"],
                            "decision_id": result.get("decision_id"), "veto_codes": result["veto_codes"]}))
        return result

    def seal_decision(self, result):
        return result

    def _decision(self, conn, result, now, snapshot_id):
        result = dict(result)
        context = {
            "active_trade_id": (Store.active(conn) or {}).get("id"),
            "episode": Store.get_state(conn, "episode", {"direction": "NO_TRADE", "used": False}),
            "daily_loss_r": 0.0,
        }
        for row in conn.execute("SELECT payload FROM trades WHERE status='CLOSED'"):
            trade = json.loads(row[0])
            if parse(trade["closed_at"]).date() == now.date():
                context["daily_loss_r"] += min(0.0, trade["r_multiple"])
        # Data outages must not reset a previously used signal episode.
        gates, episode = execution_gates(result, context, self.policy, now)
        result = apply_veto(result, gates)
        result["decision_id"] = digest({"symbol": "XAU/USD", "close": result["signal_candle_close"]})
        result["snapshot_id"] = snapshot_id
        result["execution_context"] = context
        result["execution_veto_codes"] = gates
        result = self.seal_decision(result)
        conn.execute("INSERT INTO decisions VALUES (?,?,?,?)",
                     (result["decision_id"], result["signal_candle_close"], snapshot_id, canonical(result)))
        if result["direction"] in ("BUY", "SELL"):
            eligible = now.replace(second=0, microsecond=0)
            if eligible < now:
                eligible += timedelta(minutes=1)
            # Order created now; only a FUTURE full minute can supply its fill.
            trade = {
                "id": result["decision_id"], "decision_id": result["decision_id"],
                "direction": result["direction"], "status": "PENDING",
                "entry": result["entry"], "sl": result["sl"], "tp1": result["tp1"], "tp2": result["tp2"],
                "atr": result["atr"], "created_at": stamp(now), "eligible_from": stamp(eligible),
                "expires_at": result["expires_at"], "opened_at": None, "closed_at": None,
                "last_bar_open": None, "tp1_hit": False, "tp2_hit": False,
                "exit_price": None, "result": None, "r_multiple": 0.0,
                "confidence": result["confidence"], "buy_score": result["buy_score"],
                "sell_score": result["sell_score"], "mode": result["mode"],
                "engine": result["engine"], "reasons": result["reasons"],
                "fill_model": "NEXT_FULL_1M_OPEN_GROSS_NO_COSTS", "ambiguity": False,
                **self.trade_metadata(result),
            }
            conn.execute("INSERT INTO trades VALUES (?,?,?,?)",
                         (trade["id"], result["decision_id"], "PENDING", canonical(trade)))
            Store.event(conn, trade, "CREATED", stamp(now), snapshot_id)
            episode["used"] = True
        Store.set_state(conn, "episode", episode)
        return result

    def _monitor(self, conn, candles, now, snapshot_id, risk_codes):
        trade = Store.active(conn)
        if not trade:
            return []
        # A veto observed after the eligible open cannot undo that earlier fill,
        # even if its candle is still forming or temporarily missing.
        if (trade["status"] == "PENDING" and risk_codes
                and now < parse(trade["eligible_from"])):
            trade.update(status="CANCELLED", result="RISK_VETO", closed_at=stamp(now))
            Store.save_trade(conn, trade)
            Store.event(conn, trade, "CANCELLED", stamp(now), snapshot_id, {"codes": risk_codes})
            return []
        eligible = parse(trade["eligible_from"])
        expected = parse(trade["last_bar_open"]) + timedelta(minutes=1) if trade["last_bar_open"] else eligible
        bars = [c for c in candles if parse(c.t) >= expected]
        if bars and parse(bars[0].t) > expected and allowed_session_gap(expected, parse(bars[0].t), 60):
            expected = parse(bars[0].t)
        if bars and parse(bars[0].t) > expected:
            # Do not invent a result when downtime exceeds the available candle history.
            return ["MONITOR_CATCHUP_GAP"]
        for bar in bars:
            at = parse(bar.t)
            if at > expected and allowed_session_gap(expected, at, 60):
                expected = at
            if at != expected:
                return ["MONITOR_CATCHUP_GAP"]
            if trade["status"] == "PENDING":
                if at >= parse(trade["expires_at"]):
                    trade.update(status="CANCELLED", result="EXPIRED", closed_at=stamp(at))
                elif abs(bar.o - trade["entry"]) > self.policy.max_fill_gap_atr * trade["atr"]:
                    trade.update(status="CANCELLED", result="FILL_GAP", closed_at=stamp(at))
                else:
                    original = trade["entry"]
                    delta = bar.o - original
                    trade.update(entry=bar.o, planned_entry=original,
                                 sl=round(trade["sl"] + delta, 2),
                                 tp1=round(trade["tp1"] + delta, 2),
                                 tp2=round(trade["tp2"] + delta, 2),
                                 status="OPEN", opened_at=stamp(at))
                    if not valid_plan(trade):
                        trade.update(status="CANCELLED", result="INVALID_FILL_PLAN", closed_at=stamp(at))
                    else:
                        Store.event(conn, trade, "FILLED", stamp(at), snapshot_id)
                if trade["status"] == "CANCELLED":
                    Store.save_trade(conn, trade)
                    Store.event(conn, trade, "CANCELLED", stamp(at), snapshot_id)
                    return []
            self._apply_bar(trade, bar)
            trade["last_bar_open"] = stamp(at)
            Store.save_trade(conn, trade)
            Store.event(conn, trade, "BAR", stamp(at), snapshot_id,
                        {"bar": asdict(bar), "state": dict(trade)})
            if trade["status"] == "CLOSED":
                Store.event(conn, trade, "CLOSED", trade["closed_at"], snapshot_id)
                return []
            expected = at + timedelta(minutes=1)
        if expected + timedelta(minutes=1) <= now:
            return ["MONITOR_CATCHUP_GAP"]
        return []

    @staticmethod
    def _apply_bar(trade, bar):
        buy = trade["direction"] == "BUY"
        stop = bar.l <= trade["sl"] if buy else bar.h >= trade["sl"]
        tp1 = bar.h >= trade["tp1"] if buy else bar.l <= trade["tp1"]
        tp2 = bar.h >= trade["tp2"] if buy else bar.l <= trade["tp2"]
        if stop:
            # If the market gaps beyond SL, use the worse opening price.
            exit_price = min(bar.o, trade["sl"]) if buy else max(bar.o, trade["sl"])
            trade.update(status="CLOSED", result="SL", exit_price=exit_price,
                         ambiguity=bool(tp1 or tp2))
        elif tp2:
            trade.update(status="CLOSED", result="TP2", exit_price=trade["tp2"],
                         tp1_hit=True, tp2_hit=True)
        elif tp1:
            trade["tp1_hit"] = True
        if trade["status"] == "CLOSED":
            risk = abs(trade["entry"] - trade["sl"])
            pnl = (trade["exit_price"] - trade["entry"]) * (1 if buy else -1)
            trade["r_multiple"] = round(pnl / risk, 6)
            trade["closed_at"] = stamp(parse(bar.t) + timedelta(minutes=1))

    def replay(self, decision_id):
        with self.store.connect() as conn:
            row = conn.execute("SELECT d.payload,s.payload FROM decisions d JOIN snapshots s ON s.id=d.snapshot_id WHERE d.id=?", (decision_id,)).fetchone()
        if not row:
            raise KeyError(decision_id)
        stored, snapshot = map(json.loads, row)
        if snapshot["code_hash"] != self.code_hash:
            raise ValueError("Replay requires original code version")
        if digest(snapshot) != stored["snapshot_id"]:
            raise ValueError("Snapshot checksum mismatch")
        policy = Policy(**snapshot["policy"])
        replayed = self.evaluate_snapshot(snapshot, policy)
        replayed = apply_veto(replayed, ["PROVIDER_ERROR:" + k for k in sorted(snapshot["source_errors"])])
        if snapshot["source_errors"]:
            replayed["data_status"] = "ERROR"
        replayed = apply_veto(replayed, stored.get("monitor_veto_codes", []))
        gates, _ = execution_gates(replayed, stored["execution_context"], policy, parse(snapshot["observed_at"]))
        replayed = apply_veto(replayed, gates)
        keys = self.replay_fields
        return {"decision_id": decision_id, "matches": all(stored[k] == replayed[k] for k in keys),
                "replayed": {k: replayed[k] for k in keys}}
