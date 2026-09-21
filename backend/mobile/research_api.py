"""Read-only XAU + US2Y research API projection.

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
    with runtime.read() as conn:
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

        decision = checked_research_decision(row["payload"])

        snapshot_row = conn.execute(
            "SELECT payload FROM snapshots WHERE id=?",
            (row["snapshot_id"],),
        ).fetchone()

        if not snapshot_row:
            raise ValueError("MISSING_XAU_SNAPSHOT")

        snapshot = json.loads(snapshot_row[0])

        if digest(snapshot) != row["snapshot_id"]:
            raise ValueError("XAU_SNAPSHOT_INTEGRITY_FAILURE")

        if parse(snapshot["observed_at"]) > now:
            raise ValueError("FUTURE_XAU_SNAPSHOT")

        if parse(decision["timestamp_utc"]) > now:
            raise ValueError("FUTURE_XAU_DECISION")

    value = deepcopy(decision)

    errors = research_xau_freshness_errors(
        value.get("source_candle_closes", {}),
        now,
    )

    xau_status = runtime.collector.status(now)

    if xau_status.get("status") != "HEALTHY":
        errors.append("XAU_PROVIDER_NOT_READY")

    value["veto_codes"] = sorted(
        set(value.get("veto_codes", []) + errors)
    )

    value["research_xau_provenance"] = {
        "provider": "Twelve Data",
        "symbol": "XAU/USD",
        "status": xau_status.get("status", "UNAVAILABLE"),
        "freshness": xau_status.get("freshness", "UNAVAILABLE"),
        "age_seconds": xau_status.get("age_seconds"),
        "data_mode": xau_status.get("data_mode", "UNAVAILABLE"),
    }

    return value


def research_view(runtime, research_runtime, now):
    """Produce current XAU + US2Y research signal without altering champion."""

    xau = latest_xau_candidate(runtime, now)

    us2y_rows = research_runtime.snapshot(
        now=now,
        limit=120,
    )

    result = compose_research_signal(
        xau,
        us2y_rows,
        now,
    )

    xau_status = runtime.collector.status(now)
    us2y_status = research_runtime.status(now)

    result["mode"] = "XAU_US2Y_RESEARCH_ONLY"
    result["strict_signal_unchanged"] = True
    result["automatic_promotion"] = False
    result["promotion"] = "PROMOTION_INELIGIBLE"

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
        },
        "us2y": {
            "status": us2y_status.get(
                "status",
                "UNAVAILABLE",
            ),
            "freshness": us2y_status.get(
                "freshness",
                "UNAVAILABLE",
            ),
            "symbol": "US2Y",
            "vendor": "twelve_data",
            "value": us2y_status.get("value"),
            "age_seconds": us2y_status.get(
                "age_seconds"
            ),
        },
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

    result["served_at"] = stamp(now)

    return result