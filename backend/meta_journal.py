"""Dedicated Phase 3C journals. No writes to champion trades or shadow tables."""
from dataclasses import asdict
from datetime import timedelta
import json
import math

from .domain import canonical,digest,parse,stamp,closed_frame,allowed_session_gap
from .meta_council import votes,DemoCosts

SCHEMA="""
CREATE TABLE IF NOT EXISTS meta_decisions (
 id TEXT PRIMARY KEY, at TEXT NOT NULL, namespace TEXT NOT NULL, regime_key TEXT NOT NULL,
 inputs TEXT NOT NULL, result TEXT NOT NULL, checksum TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS meta_regime_history ON meta_decisions(namespace,regime_key,at);
CREATE TABLE IF NOT EXISTS meta_outcomes (
 id TEXT PRIMARY KEY, namespace TEXT NOT NULL, known_at TEXT NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS meta_outcome_history ON meta_outcomes(namespace,known_at);
CREATE TABLE IF NOT EXISTS meta_positions (
 id TEXT PRIMARY KEY REFERENCES meta_decisions(id), status TEXT NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS meta_single_active ON meta_positions((1)) WHERE status IN ('PENDING','OPEN');
CREATE TABLE IF NOT EXISTS meta_position_events (
 id TEXT PRIMARY KEY, position_id TEXT NOT NULL REFERENCES meta_positions(id), snapshot_id TEXT NOT NULL REFERENCES snapshots(id),
 at TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL, checksum TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta_control (key TEXT PRIMARY KEY, payload TEXT NOT NULL);
"""


def checked_payload(text,checksum):
    payload=json.loads(text)
    if digest(payload)!=checksum:
        raise ValueError("History checksum mismatch")
    return payload


def checked_champion(conn,decision_id):
    row=conn.execute("SELECT payload,snapshot_id FROM decisions WHERE id=?",(decision_id,)).fetchone()
    if not row:
        raise ValueError("Missing champion")
    decision=json.loads(row[0])
    signed=dict(decision)
    checksum=signed.pop("decision_checksum",None)
    if digest(signed)!=checksum:
        raise ValueError("Champion checksum mismatch")
    snapshot=json.loads(conn.execute("SELECT payload FROM snapshots WHERE id=?",(row[1],)).fetchone()[0])
    if digest(snapshot)!=row[1]:
        raise ValueError("Source snapshot mismatch")
    return decision


def record_outcome(conn,namespace,source,trade,champion,known_at,costs,prediction=None,prediction_target=None,meta=None):
    if trade["status"]!="CLOSED":
        return
    opened,closed,known=(parse(v) for v in (trade["opened_at"],trade["closed_at"],known_at))
    if not opened<closed<=known:
        raise ValueError("Invalid closed outcome chronology")
    risk=trade.get("risk",abs(trade["entry"]-trade["sl"]))
    gross=trade["r_multiple"]
    if type(gross) not in (int,float) or not math.isfinite(gross):
        raise ValueError("Invalid gross R")
    identity=digest([namespace,source,trade["id"]])
    regime=dict(trade["analytics_context"])
    regime["direction"]=trade["direction"]
    payload={"id":identity,"namespace":namespace,"source":source,"trade_id":trade["id"],
             "decision_id":champion["decision_id"],"status":"CLOSED","opened_at":stamp(opened),
             "closed_at":stamp(closed),"known_at":stamp(known),"gross_r":gross,"net_r":costs.net(gross,risk),
             "risk_points":risk,"cost_assumptions":asdict(costs),"regime":regime,"votes":votes(champion),
             "raw_score":meta["challenger"]["raw_score"] if meta else champion["raw_score"],
             "prediction":prediction,"prediction_at":meta["at"] if meta else champion["timestamp_utc"],
             "prediction_target":prediction_target,"leakage_audit_passed":bool(meta and not meta["routing"]["fallback"])}
    existing=conn.execute("SELECT payload,checksum FROM meta_outcomes WHERE id=?",(identity,)).fetchone()
    if existing:
        checked_payload(*existing)
        return
    conn.execute("INSERT INTO meta_outcomes VALUES (?,?,?,?,?)",(identity,namespace,stamp(known),canonical(payload),digest(payload)))


def ingest_base_outcomes(conn,namespace,model_identity,costs,at):
    # First-known time comes from immutable journal snapshots, not economic close alone.
    sources=[("CHAMPION","""SELECT e.payload,s.observed_at,d.id,t.payload,s.payload,s.id FROM trade_events e
             JOIN snapshots s ON s.id=e.snapshot_id JOIN trades t ON t.id=e.trade_id
             JOIN decisions d ON d.id=t.decision_id WHERE e.kind='CLOSED' AND s.observed_at<=?
             ORDER BY e.id DESC LIMIT 10000"""),
             ("SHADOW","""SELECT e.payload,s.observed_at,t.decision_id,t.payload,s.payload,s.id FROM phase3b_shadow_events e
             JOIN snapshots s ON s.id=e.snapshot_id JOIN phase3b_shadows t ON t.id=e.shadow_id
             WHERE json_extract(e.payload,'$.event')='CLOSED' AND s.observed_at<=?
             ORDER BY s.observed_at DESC,e.id DESC LIMIT 10000""")]
    for source,query in sources:
        for row in conn.execute(query,(stamp(at),)):
            event=json.loads(row[0])
            trade=event if source=="CHAMPION" else event["trade"]
            if canonical(trade)!=canonical(json.loads(row[3])) or digest(json.loads(row[4]))!=row[5]:
                raise ValueError("Corrupt source outcome journal")
            champion=checked_champion(conn,row[2])
            if champion["model_identity"]!=model_identity:
                continue
            source_name=source if source=="CHAMPION" else trade["variant"]
            prediction=champion["calibrated_confidence"] if source=="CHAMPION" else None
            record_outcome(conn,namespace,source_name,trade,champion,row[1],costs,prediction,"POSITIVE_GROSS_R" if prediction is not None else None)


def save_position(conn,trade,snapshot_id,at,kind):
    checksum=digest(trade)
    conn.execute("UPDATE meta_positions SET status=?,payload=?,checksum=? WHERE id=?",(trade["status"],canonical(trade),checksum,trade["id"]))
    identity=digest([trade["id"],snapshot_id,at,kind])
    conn.execute("INSERT OR IGNORE INTO meta_position_events VALUES (?,?,?,?,?,?,?)",
                 (identity,trade["id"],snapshot_id,at,kind,canonical(trade),checksum))


def create_position(conn,meta,snapshot,now,snapshot_id):
    if meta["challenger"]["direction"] not in ("BUY","SELL"):
        return
    if conn.execute("SELECT 1 FROM meta_positions WHERE status IN ('PENDING','OPEN')").fetchone():
        return
    champion=meta["champion"]
    entry,sl,tp=champion["entry"],champion["sl"],champion["tp2"]
    if any(type(x) not in (int,float) or not math.isfinite(x) for x in (entry,sl,tp)) or abs(entry-sl)<=0:
        raise ValueError("Invalid hypothetical plan")
    eligible=now.replace(second=0,microsecond=0)
    if eligible<now:
        eligible+=timedelta(minutes=1)
    trade={"id":champion["decision_id"],"decision_id":champion["decision_id"],"namespace":meta["namespace"],
           "status":"PENDING","direction":meta["challenger"]["direction"],"entry":entry,"sl":sl,"tp":tp,
           "risk":abs(entry-sl),"created_at":stamp(now),"eligible_from":stamp(eligible),
           "expires_at":champion["expires_at"],"checkpoint":None,"opened_at":None,"closed_at":None,
           "market_mode":snapshot["market_provenance"]["data_mode"],"analytics_context":meta["regime"],
           "r_multiple":None,"fill_model":"NEXT_FULL_1M_CHAMPION_PLAN_GROSS_WITH_SEPARATE_SIMULATED_COSTS"}
    conn.execute("INSERT INTO meta_positions VALUES (?,?,?,?)",(trade["id"],trade["status"],canonical(trade),digest(trade)))
    save_position(conn,trade,snapshot_id,stamp(now),"CREATED")


def monitor_positions(conn,snapshot,now,snapshot_id,costs):
    for row in conn.execute("SELECT payload,checksum FROM meta_positions WHERE status IN ('PENDING','OPEN')").fetchall():
        trade=checked_payload(*row)
        if snapshot["market_provenance"]["data_mode"]!=trade["market_mode"] or "1min" in snapshot["source_errors"]:
            continue
        expected=parse(trade["checkpoint"] or trade["eligible_from"])
        try:
            data=[r for r in snapshot["frames"].get("1min",[]) if parse(r["t"])>=expected]
            bars,errors=closed_frame(data,"1min",now,minimum=0,continuity=False)
        except (KeyError,TypeError,ValueError):
            continue
        if errors:
            continue
        for bar in bars:
            at=parse(bar.t)
            if at>expected and allowed_session_gap(expected,at,60):
                expected=at
            if at!=expected:
                break
            if trade["status"]=="PENDING":
                if at>=parse(trade["expires_at"]) or abs(bar.o-trade["entry"])>trade["risk"]*.5:
                    trade.update(status="CANCELLED",closed_at=stamp(at),result="EXPIRED_OR_FILL_GAP")
                    save_position(conn,trade,snapshot_id,stamp(at),"CANCELLED")
                    break
                delta=bar.o-trade["entry"]
                trade.update(status="OPEN",opened_at=stamp(at),entry=bar.o,sl=trade["sl"]+delta,tp=trade["tp"]+delta)
                save_position(conn,trade,snapshot_id,stamp(at),"FILLED")
            buy=trade["direction"]=="BUY"
            stop=bar.l<=trade["sl"] if buy else bar.h>=trade["sl"]
            target=bar.h>=trade["tp"] if buy else bar.l<=trade["tp"]
            # Same exits as champion: no invented time exit, fees do not move the stops.
            if stop or target:
                price=(min(bar.o,trade["sl"]) if buy else max(bar.o,trade["sl"])) if stop else trade["tp"]
                trade.update(status="CLOSED",closed_at=stamp(at+timedelta(minutes=1)),exit_price=price,
                             result="SL" if stop else "TP2",ambiguity=bool(stop and target),
                             r_multiple=round((price-trade["entry"])*(1 if buy else -1)/trade["risk"],8))
            trade["checkpoint"]=stamp(at+timedelta(minutes=1))
            save_position(conn,trade,snapshot_id,trade["checkpoint"],"CLOSED" if trade["status"]=="CLOSED" else "BAR")
            if trade["status"]=="CLOSED":
                row=conn.execute("SELECT inputs,result,checksum FROM meta_decisions WHERE id=?",(trade["id"],)).fetchone()
                inputs,meta=json.loads(row[0]),json.loads(row[1])
                if digest({"inputs":inputs,"result":meta})!=row[2]:
                    raise ValueError("Corrupt originating meta decision")
                record_outcome(conn,trade["namespace"],"ADAPTIVE_CHALLENGER",trade,meta["champion"],stamp(now),
                               DemoCosts(**inputs["costs"]),meta["calibration"]["probability"],"POSITIVE_NET_SIMULATED_R",meta)
                break
            expected=at+timedelta(minutes=1)
