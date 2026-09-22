"""XAU + US2Y isolated research signal.

This module is intentionally separate from the production/champion policy.

RESEARCH / DEMO ONLY.
No broker execution.
No profitability claim.
"""

from datetime import timedelta

from ..domain import parse, stamp


PROFILE = "XAU_ONLY_RESEARCH_V1"

US2Y_FRESHNESS_SECONDS = 900
US2Y_MOMENTUM_SECONDS = 3600
US2Y_ANCHOR_TOLERANCE_SECONDS = 600

# US2Y is quoted in percentage points.
# 0.01 percentage point = 1 basis point.
US2Y_CONFIRM_THRESHOLD_BPS = 1.0


IGNORED_RESEARCH_VETOES = {
    "MISSING_CRITICAL_EVENT_DATA",
    "MACRO_PROVIDER_FAILURE",

    # Research exists specifically to collect forward evidence before
    # the normal calibration requirement has been satisfied.
    "INSUFFICIENT_CALIBRATION",
}


IGNORED_RESEARCH_PREFIXES = (
    "MISSING_CRITICAL_INTELLIGENCE:",
    "STALE_INTELLIGENCE:",
    "FUTURE_INTELLIGENCE:",
    "CONFLICTING_HIGH_IMPACT_SOURCES:",
    "PROVIDER_NOT_READY:",
)


def _research_ignored_veto(code):
    return (
        code in IGNORED_RESEARCH_VETOES
        or any(code.startswith(prefix) for prefix in IGNORED_RESEARCH_PREFIXES)
    )


def us2y_momentum(rows, now):
    """Return causal 1-hour US2Y momentum using observations known by now."""

    eligible = []

    for row in rows:
        try:
            observed = parse(row["observed_at"])
            retrieved = parse(row["retrieved_at"])
            value = float(row["value"])

            if observed > now or retrieved > now:
                continue

            if observed > retrieved:
                continue

            eligible.append(
                {
                    "observed_at": observed,
                    "retrieved_at": retrieved,
                    "value": value,
                }
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue

    eligible.sort(key=lambda row: row["observed_at"])

    if not eligible:
        return {
            "status": "UNAVAILABLE",
            "bias": "UNAVAILABLE",
            "change_bps": None,
            "age_seconds": None,
            "reason": "NO_US2Y_DATA",
        }

    latest = eligible[-1]
    age = (now - latest["observed_at"]).total_seconds()

    if not 0 <= age < US2Y_FRESHNESS_SECONDS:
        return {
            "status": "STALE",
            "bias": "UNAVAILABLE",
            "change_bps": None,
            "age_seconds": age,
            "latest_at": stamp(latest["observed_at"]),
            "latest_value": latest["value"],
            "reason": "STALE_US2Y_DATA",
        }

    target = latest["observed_at"] - timedelta(
        seconds=US2Y_MOMENTUM_SECONDS
    )

    anchors = [
        row for row in eligible
        if row["observed_at"] <= target
    ]

    if not anchors:
        return {
            "status": "INSUFFICIENT_HISTORY",
            "bias": "UNAVAILABLE",
            "change_bps": None,
            "age_seconds": age,
            "latest_at": stamp(latest["observed_at"]),
            "latest_value": latest["value"],
            "reason": "MISSING_US2Y_MOMENTUM_HISTORY",
        }

    anchor = anchors[-1]
    anchor_gap = (target - anchor["observed_at"]).total_seconds()

    if anchor_gap > US2Y_ANCHOR_TOLERANCE_SECONDS:
        return {
            "status": "INSUFFICIENT_HISTORY",
            "bias": "UNAVAILABLE",
            "change_bps": None,
            "age_seconds": age,
            "latest_at": stamp(latest["observed_at"]),
            "latest_value": latest["value"],
            "anchor_at": stamp(anchor["observed_at"]),
            "anchor_value": anchor["value"],
            "reason": "US2Y_HISTORY_GAP",
        }

    change_bps = round(
        (latest["value"] - anchor["value"]) * 100,
        4,
    )

    # Falling yields are treated only as a research confirmation for gold BUY.
    # Rising yields are treated only as a research confirmation for gold SELL.
    if change_bps <= -US2Y_CONFIRM_THRESHOLD_BPS:
        bias = "BUY"
    elif change_bps >= US2Y_CONFIRM_THRESHOLD_BPS:
        bias = "SELL"
    else:
        bias = "NEUTRAL"

    return {
        "status": "READY",
        "bias": bias,
        "change_bps": change_bps,
        "age_seconds": age,
        "latest_at": stamp(latest["observed_at"]),
        "latest_value": latest["value"],
        "anchor_at": stamp(anchor["observed_at"]),
        "anchor_value": anchor["value"],
        "window_seconds": US2Y_MOMENTUM_SECONDS,
        "threshold_bps": US2Y_CONFIRM_THRESHOLD_BPS,
        "reason": "US2Y_1H_MOMENTUM_RESEARCH_HEURISTIC",
    }


def compose_research_signal(xau_decision, us2y_rows, now):
    """Combine XAU technical candidate with US2Y research confirmation."""

    result = {
        "profile": PROFILE,
        "direction": "NO_TRADE",
        "action": "WAIT",
        "execution": "DEMO_ONLY",
        "research_only": True,
        "predictive_edge_established": False,
        "confidence": None,
        "confidence_kind": "UNVALIDATED_RESEARCH",
        "symbol": "XAU/USD",
        "timestamp_utc": stamp(now),
        "candidate_direction": "NO_TRADE",
        "entry": None,
        "us2y": None,
        "veto_codes": [],
        "reasons": [],
    }

    if not isinstance(xau_decision, dict):
        result["veto_codes"].append("NO_XAU_DECISION")
        result["reasons"].append(
            "No XAU technical decision is available."
        )
        return result

    candidate = xau_decision.get(
        "candidate_direction",
        "NO_TRADE",
    )

    result["candidate_direction"] = candidate
    result["entry"] = xau_decision.get("entry")

    if candidate not in ("BUY", "SELL"):
        result["veto_codes"].append(
            "NO_XAU_TECHNICAL_CANDIDATE"
        )

    technical_blocks = [
        code
        for code in xau_decision.get("veto_codes", [])
        if not _research_ignored_veto(code)
    ]

    result["xau_technical_blocks"] = sorted(
        set(technical_blocks)
    )

    result["veto_codes"].extend(technical_blocks)

    # Expiry is an execution gate for an otherwise actionable technical setup.
    # When the current evaluation is already blocked by technical conditions,
    # reporting expiry as an additional cause is misleading: the setup is not
    # actionable regardless of its clock. Keep malformed expiry fail-closed for
    # candidates that otherwise pass the technical gates.
    if candidate in ("BUY", "SELL") and not technical_blocks:
        expires_at = xau_decision.get("expires_at")
        if expires_at:
            try:
                if now >= parse(expires_at):
                    result["veto_codes"].append(
                        "XAU_SIGNAL_EXPIRED"
                    )
            except (TypeError, ValueError, AttributeError):
                result["veto_codes"].append(
                    "INVALID_XAU_EXPIRY"
                )

    us2y = us2y_momentum(us2y_rows, now)
    result["us2y"] = us2y


    result["veto_codes"] = sorted(
        set(result["veto_codes"])
    )

    if not result["veto_codes"]:
        result["direction"] = candidate
        result["action"] = (
            f"{candidate} RESEARCH SETUP"
        )
        result["reasons"].append(
            "XAU technical candidate passed the XAU-only research gates."
        )
    else:
        result["reasons"].append(
            "Research setup blocked by XAU technical conditions."
        )

    result["reasons"].append(
        "XAU-only research test; US2Y does not gate this signal."
    )

    return result