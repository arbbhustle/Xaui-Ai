"""Isolated hypothetical next-bar experiments. Never read by production calibration."""
from datetime import timedelta
import json

from .domain import allowed_session_gap, canonical, closed_frame, digest, parse, stamp

VARIANTS = ("TREND_CONTINUATION", "SWEEP_RECLAIM", "RESILIENCE_DIVERGENCE")
SCHEMA = """
CREATE TABLE IF NOT EXISTS phase3b_shadows (
 id TEXT PRIMARY KEY, variant TEXT NOT NULL, status TEXT NOT NULL,
 decision_id TEXT NOT NULL REFERENCES decisions(id), payload TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS phase3b_shadow_active ON phase3b_shadows(variant)
 WHERE status IN ('PENDING','OPEN');
CREATE TABLE IF NOT EXISTS phase3b_shadow_events (
 id TEXT PRIMARY KEY, shadow_id TEXT NOT NULL REFERENCES phase3b_shadows(id),
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id), at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phase3b_context (
 at TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id), payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS phase3b_shadow_decisions (
 decision_id TEXT NOT NULL REFERENCES decisions(id), variant TEXT NOT NULL,
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id), payload TEXT NOT NULL,
 PRIMARY KEY(decision_id,variant));
"""


def save(conn, trade, snapshot_id, at, event):
    conn.execute("UPDATE phase3b_shadows SET status=?,payload=? WHERE id=?", (trade["status"],canonical(trade),trade["id"]))
    payload = {"event":event, "trade":trade}
    identity = digest([trade["id"], snapshot_id, at, event])
    conn.execute("INSERT OR IGNORE INTO phase3b_shadow_events VALUES (?,?,?,?,?)",
                 (identity,trade["id"],snapshot_id,at,canonical(payload)))


def monitor(conn, snapshot, snapshot_id, now):
    for row in conn.execute("SELECT payload FROM phase3b_shadows WHERE status IN ('PENDING','OPEN')").fetchall():
        trade = json.loads(row[0])
        # Preserve the checkpoint on mismatched provenance, missing bars or gaps.
        mode = snapshot["market_provenance"]["data_mode"]
        if mode == "UNAVAILABLE" or mode != trade["market_mode"] or "1min" in snapshot["source_errors"]:
            continue
        expected = parse(trade["checkpoint"]) if trade["checkpoint"] else parse(trade["eligible_from"])
        try:
            rows = [r for r in snapshot["frames"].get("1min",[]) if parse(r["t"]) >= expected]
            bars, errors = closed_frame(rows,"1min",now,minimum=0,continuity=False)
        except (TypeError,KeyError,ValueError,AttributeError):
            continue
        if errors:
            continue
        for bar in bars:
            at = parse(bar.t)
            if at > expected and allowed_session_gap(expected,at,60):
                expected = at
            if at != expected:
                break
            if trade["status"] == "PENDING":
                if at >= parse(trade["expires_at"]):
                    trade.update(status="CANCELLED",closed_at=stamp(at),result="EXPIRED")
                    save(conn,trade,snapshot_id,stamp(at),"CANCELLED")
                    break
                if abs(bar.o-trade["planned_entry"]) > trade["risk"]*.5:
                    trade.update(status="CANCELLED",closed_at=stamp(at),result="FILL_GAP")
                    save(conn,trade,snapshot_id,stamp(at),"CANCELLED")
                    break
                trade.update(status="OPEN",entry=bar.o,opened_at=stamp(at),
                             sl=bar.o-trade["sign"]*trade["risk"],tp=bar.o+trade["sign"]*trade["risk"]*2)
                save(conn,trade,snapshot_id,stamp(at),"FILLED")
            buy = trade["sign"] == 1
            stop = bar.l <= trade["sl"] if buy else bar.h >= trade["sl"]
            target = bar.h >= trade["tp"] if buy else bar.l <= trade["tp"]
            timeout = at+timedelta(minutes=1) >= parse(trade["opened_at"])+timedelta(minutes=60)
            if stop or target or timeout:
                price = (min(bar.o,trade["sl"]) if buy else max(bar.o,trade["sl"])) if stop else trade["tp"] if target else bar.c
                trade.update(status="CLOSED",closed_at=stamp(at+timedelta(minutes=1)),
                             result="SL" if stop else "TP" if target else "TIME_EXIT",
                             r_multiple=round(trade["sign"]*(price-trade["entry"])/trade["risk"],6),
                             ambiguity=bool(stop and target),exit_price=price)
            trade["checkpoint"] = stamp(at+timedelta(minutes=1))
            save(conn,trade,snapshot_id,trade["checkpoint"],"CLOSED" if trade["status"] == "CLOSED" else "BAR")
            if trade["status"] == "CLOSED":
                break
            expected = at+timedelta(minutes=1)


def create(conn, result, snapshot, snapshot_id, now):
    hidden = result["hidden_state"]
    if not result.get("decision_id"):
        return
    blockers = sorted(set(result["intelligence"]["vetoes"]+result["slow_regime"]["vetoes"]+hidden["vetoes"]+
                          ["PROVIDER_ERROR:"+k for k in snapshot["source_errors"]]))
    if not hidden["dgfe"] or result["atr"] is None or result["atr"] <= 0:
        blockers.append("SHADOW_INPUT_UNAVAILABLE")
    if result["slow_regime"]["data_mode"] == "TEST_DATA" and snapshot["market_provenance"]["data_mode"] != "FIXTURE":
        blockers.append("SLOW_TEST_DATA_IN_REAL_MARKET")
    scores = {"TREND_CONTINUATION": hidden["dgfe"].get("directional_pressure"),
              "SWEEP_RECLAIM": hidden["dgfe"].get("liquidity_pressure"),
              "RESILIENCE_DIVERGENCE": hidden["resilience"].get("gold_resilience_score")}
    for variant,score in scores.items():
        threshold = 35 if variant == "TREND_CONTINUATION" else 50
        vetoes = list(blockers)
        if score is None or abs(score) < threshold:
            vetoes.append("SHADOW_SCORE_BELOW_THRESHOLD")
        occupied = conn.execute("SELECT 1 FROM phase3b_shadows WHERE variant=? AND status IN ('PENDING','OPEN')",(variant,)).fetchone()
        if occupied:
            vetoes.append("SHADOW_POSITION_ACTIVE")
        evaluation = {"variant":variant,"score":score,"threshold":threshold,"as_of":stamp(now),
                      "direction":"NO_TRADE" if vetoes else "BUY" if score > 0 else "SELL", "vetoes":vetoes}
        conn.execute("INSERT OR IGNORE INTO phase3b_shadow_decisions VALUES (?,?,?,?)",
                     (result["decision_id"],variant,snapshot_id,canonical(evaluation)))
        if vetoes:
            continue
        identity = digest([result["decision_id"],variant])
        if conn.execute("SELECT 1 FROM phase3b_shadows WHERE id=?",(identity,)).fetchone():
            continue
        eligible = now.replace(second=0,microsecond=0)
        if eligible < now:
            eligible += timedelta(minutes=1)
        risk = max(result["atr"],result["entry"]*.0008)
        direction = "BUY" if score > 0 else "SELL"
        trade = {"id":identity,"variant":variant,"status":"PENDING","created_at":stamp(now),
                 "eligible_from":stamp(eligible),"expires_at":stamp(now+timedelta(minutes=5)),
                 "decision_id":result["decision_id"],"market_mode":snapshot["market_provenance"]["data_mode"],
                 "direction":direction,"sign":1 if score > 0 else -1,"planned_entry":result["entry"],
                 "risk":risk,"checkpoint":None,"r_multiple":None,"closed_at":None,
                 "analytics_context":dict(result["analytics_context"],direction=direction),
                 "model":"SHADOW_NEXT_FULL_1M_1R_STOP_2R_TARGET_60M_GROSS", "mode":"SHADOW_DEMO_ONLY"}
        conn.execute("INSERT INTO phase3b_shadows VALUES (?,?,?,?,?)",(identity,variant,"PENDING",result["decision_id"],canonical(trade)))
        save(conn,trade,snapshot_id,stamp(now),"CREATED")
