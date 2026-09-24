"""Read-only XAU research API projection.

RESEARCH / DEMO ONLY.
The normal /signal champion remains unchanged.
"""

from copy import deepcopy
import json

from ..domain import Policy, digest, freshness_errors, parse, stamp
from .collection import MOBILE_XAU_FRESHNESS_SECONDS
from .research_signal import compose_research_signal


def research_xau_freshness_errors(closes, now):
    errors = freshness_errors(closes, now, Policy())

    try:
        age = (now - parse(closes["1min"])).total_seconds()

        if 0 <= age < MOBILE_XAU_FRESHNESS_SECONDS:
            errors = [
                error
                for error in errors
                if error != "STALE_DATA:1min"
            ]
    except (KeyError, TypeError, ValueError, AttributeError):
        pass

    return errors


def checked_research_decision(payload):
    value = json.loads(payload)
    checksum = value.pop("decision_checksum", None)

    if checksum != digest(value):
        raise ValueError("DECISION_INTEGRITY_FAILURE")

    return value


def latest_xau_candidate(runtime, now):
    # Every completed forward cycle writes its evaluated champion result to the
    # immutable DECISION ledger, even when the strategy's signal candle has not
    # advanced enough to create a new row in the deduplicated decisions table.
    # Research must use that current evaluation rather than an older 5m decision.
    with runtime.read() as conn:
        ledger = conn.execute(
            """
            SELECT payload_id, at
            FROM forward_ledger
            WHERE kind='DECISION' AND at<=?
            ORDER BY seq DESC
            LIMIT 1
            """,
            (stamp(now),),
        ).fetchone()

        if ledger:
            event = runtime.store.get(conn, ledger["payload_id"])
            value = deepcopy(event.get("champion") or {})
            snapshot_id = event.get("snapshot_id")

            if not value or not snapshot_id:
                raise ValueError("MISSING_XAU_LEDGER_EVALUATION")

            snapshot_row = conn.execute(
                "SELECT payload FROM snapshots WHERE id=?",
                (snapshot_id,),
            ).fetchone()

            if not snapshot_row:
                raise ValueError("MISSING_XAU_SNAPSHOT")

            snapshot = json.loads(snapshot_row[0])

            if digest(snapshot) != snapshot_id:
                raise ValueError("XAU_SNAPSHOT_INTEGRITY_FAILURE")

            if parse(snapshot["observed_at"]) > now:
                raise ValueError("FUTURE_XAU_SNAPSHOT")

            if value.get("timestamp_utc") and parse(value["timestamp_utc"]) > now:
                raise ValueError("FUTURE_XAU_DECISION")
        else:
            row = conn.execute(
                """
                SELECT d.*
                FROM decisions d
                JOIN snapshots s ON s.id=d.snapshot_id
                WHERE s.observed_at<=?
                ORDER BY d.candle_close DESC
                LIMIT 1
                """,
                (stamp(now),),
            ).fetchone()

            if not row:
                return None

            value = checked_research_decision(row["payload"])

    # The immutable evaluation already performed interval-specific freshness
    # validation against its own captured observed_at. Do not re-evaluate those
    # candle closes against API wall-clock time: higher-timeframe candles (15m,
    # 1h, 4h) are expected to be older between closes and would otherwise be
    # falsely marked stale while the live 1m feed is healthy.
    #
    # Current feed freshness is exposed separately through collector.status()
    # below and remains fail-closed in the collector/evaluation pipeline.

    # Do not re-gate a completed immutable evaluation with the collector's
    # *current* process status. The evaluation already carries the causal data
    # quality/provider vetoes from its own captured snapshot. A later status
    # transition (for example cadence verification becoming healthy between the
    # DECISION event and this API read) must not be retroactively attached to
    # that older evaluation.
    xau_status = runtime.collector.status(now)

    value["veto_codes"] = sorted(set(value.get("veto_codes", [])))

    value["research_xau_provenance"] = {
        "provider": "Twelve Data",
        "symbol": "XAU/USD",
        "status": xau_status.get("status", "UNAVAILABLE"),
        "freshness": xau_status.get("freshness", "UNAVAILABLE"),
        "age_seconds": xau_status.get("age_seconds"),
        "data_mode": xau_status.get("data_mode", "UNAVAILABLE"),
    }

    return value


def research_history(runtime, now, limit=30):
    """Project immutable XAU research setups without mixing Champion history.

    Only actionable BUY/SELL evaluations are shown. This is evidence history,
    not a second strategy and not a source of broker execution.
    """
    items = []
    seen = set()

    with runtime.read() as conn:
        # Do not cap the search to the latest 500 evaluations. At the normal
        # two-minute cadence that is only about 16.7 hours, so a quiet market
        # can make valid BUY/SELL research setups disappear from the phone even
        # though the immutable ledger still contains them. Page backwards until
        # the requested number of actionable setups is found or history ends.
        before_seq = None
        exhausted = False

        while len(items) < limit and not exhausted:
            if before_seq is None:
                rows = conn.execute(
                    """
                    SELECT seq, at, payload_id
                    FROM forward_ledger
                    WHERE kind='DECISION' AND at<=?
                    ORDER BY seq DESC
                    LIMIT 500
                    """,
                    (stamp(now),),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT seq, at, payload_id
                    FROM forward_ledger
                    WHERE kind='DECISION' AND at<=? AND seq<?
                    ORDER BY seq DESC
                    LIMIT 500
                    """,
                    (stamp(now), before_seq),
                ).fetchall()

            if not rows:
                break

            exhausted = len(rows) < 500
            before_seq = rows[-1]["seq"]

            for row in rows:
                event = runtime.store.get(conn, row["payload_id"])
                decision = event.get("champion") or {}
                if not isinstance(decision, dict):
                    continue

                direction = decision.get("candidate_direction")
                if direction not in ("BUY", "SELL"):
                    continue

                technical_blocks = sorted({
                    code
                    for code in decision.get("veto_codes", [])
                    if not str(code).startswith((
                        "MISSING_CRITICAL_INTELLIGENCE:",
                        "STALE_INTELLIGENCE:",
                        "FUTURE_INTELLIGENCE:",
                        "CONFLICTING_HIGH_IMPACT_SOURCES:",
                        "PROVIDER_NOT_READY:",
                    ))
                    and code not in {
                        "MISSING_CRITICAL_EVENT_DATA",
                        "MACRO_PROVIDER_FAILURE",
                        "INSUFFICIENT_CALIBRATION",
                        "TIMEFRAME_DISAGREEMENT:1h",
                        "TIMEFRAME_DISAGREEMENT:4h",
                        "HIGH_MARKET_ENTROPY",
                        "EXTREME_TIMEFRAME_TENSION",
                        "MARKET_SHOCK",
                        "UNRESOLVED_EVENT_SHOCK",
                        "UNSTABLE_HIDDEN_STATE",
                        "HIDDEN_COUNCIL_CONFLICT",
                    }
                })
                if technical_blocks:
                    continue

                signal_close = decision.get("signal_candle_close")
                expires_at = decision.get("expires_at")
                key = (direction, signal_close, expires_at)
                if key in seen:
                    continue
                seen.add(key)

                items.append({
                    "source": "XAU_ONLY_RESEARCH_V1",
                    "execution": "DEMO_ONLY",
                    "direction": direction,
                    "candidate_direction": direction,
                    "entry": decision.get("entry"),
                    "sl": decision.get("sl"),
                    "tp1": decision.get("tp1"),
                    "tp2": decision.get("tp2"),
                    "timestamp_utc": signal_close or row["at"],
                    "signal_candle_close": signal_close,
                    "expires_at": expires_at,
                    "buy_score": decision.get("buy_score"),
                    "sell_score": decision.get("sell_score"),
                    "raw_score": decision.get("raw_score"),
                    "edge": decision.get("edge"),
                    "rsi_state": decision.get("rsi_state"),
                    "atr": decision.get("atr"),
                    "mode": decision.get("mode"),
                    "session": decision.get("session"),
                    "components": deepcopy(decision.get("components", {})),
                    "hidden_state": deepcopy(decision.get("hidden_state", {})),
                    "analytics_context": deepcopy(decision.get("analytics_context", {})),
                    "status": "RECORDED_RESEARCH_SETUP",
                    "data_status": "HISTORICAL_POINT_IN_TIME",
                    "cursor": row["seq"],
                })
                if len(items) >= limit:
                    break

    return {
        "items": items,
        "source": "XAU_ONLY_RESEARCH_V1",
        "execution": "DEMO_ONLY",
    }


def research_view(runtime, research_runtime, now):
    """Produce current XAU-only research signal without altering champion."""

    xau = latest_xau_candidate(runtime, now)

    result = compose_research_signal(xau, now)

    xau_status = runtime.collector.status(now)

    result["mode"] = "XAU_ONLY_RESEARCH"
    result["strict_signal_unchanged"] = True
    result["automatic_promotion"] = False
    result["promotion"] = "PROMOTION_INELIGIBLE"

    # Research readiness is intentionally XAU-only. The strict /signal
    # readiness still requires every critical provider, but this isolated
    # research profile must not report NOT_READY merely because US2Y or the
    # strict macro stack is unavailable.
    xau_ready = (
        xau_status.get("status") == "HEALTHY"
        and xau_status.get("freshness") == "FRESH"
        and xau_status.get("approval") == "APPROVED_CONFIGURATION"
    )
    result["readiness"] = {
        "status": "DATA_READY" if xau_ready else "NOT_READY",
        "scope": "XAU_ONLY_RESEARCH",
        "reasons": [] if xau_ready else ["XAU_PROVIDER_NOT_READY"],
    }
    result["data_status"] = "FRESH_XAU_RESEARCH" if xau_ready else "UNAVAILABLE"
    result["data_mode"] = "RESEARCH_PROVIDER_DATA" if xau_ready else "UNAVAILABLE"

    result["provider_health"] = {
        "xau": {
            "status": xau_status.get(
                "status",
                "UNAVAILABLE",
            ),
            "freshness": xau_status.get(
                "freshness",
                "UNAVAILABLE",
            ),
            "symbol": "XAU/USD",
            "vendor": "twelve_data",
            "age_seconds": xau_status.get("age_seconds"),
            "last_observed_at": xau_status.get("last_observed_at"),
            "received_at": xau_status.get("received_at"),
            "last_attempt_status": xau_status.get("last_attempt_status"),
            "approval": xau_status.get("approval"),
            "data_mode": xau_status.get("data_mode"),
            "credential_configured": xau_status.get("credential_configured"),
            "validation_checks": xau_status.get("validation_checks", []),
        }
    }

    if xau:
        result["xau"] = {
            "candidate_direction": xau.get(
                "candidate_direction",
                "NO_TRADE",
            ),
            "entry": xau.get("entry"),
            "signal_candle_close": xau.get(
                "signal_candle_close"
            ),
            "expires_at": xau.get("expires_at"),
            "buy_score": xau.get("buy_score"),
            "sell_score": xau.get("sell_score"),
            "technical_blocks": result.get(
                "xau_technical_blocks",
                [],
            ),
        }
    else:
        result["xau"] = {
            "candidate_direction": "NO_TRADE",
            "entry": None,
            "technical_blocks": [
                "NO_XAU_DECISION"
            ],
        }

    # Embed a small, read-only research history in the same response so the
    # phone keeps its fast single-request Home sync. Champion /history and
    # /performance remain isolated and are never mixed into this profile.
    # Some unit-test runtimes intentionally provide only collector state.
    # Production Runtime has the read/store interfaces required for ledger history.
    if hasattr(runtime, "read") and hasattr(runtime, "store"):
        result["research_history"] = research_history(runtime, now, limit=30)
    else:
        result["research_history"] = {
            "items": [],
            "source": "XAU_ONLY_RESEARCH_V1",
            "execution": "DEMO_ONLY",
        }

    result["served_at"] = stamp(now)

    return result