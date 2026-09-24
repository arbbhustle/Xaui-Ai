"""XAU-only isolated research signal.

This module is intentionally separate from the production/champion policy.

RESEARCH / DEMO ONLY.
No broker execution.
No profitability claim.
"""

from ..domain import parse, stamp


PROFILE = "XAU_ONLY_RESEARCH_V1"

IGNORED_RESEARCH_VETOES = {
    "MISSING_CRITICAL_EVENT_DATA",
    "MACRO_PROVIDER_FAILURE",

    # Research exists specifically to collect forward evidence before
    # the normal calibration requirement has been satisfied.
    "INSUFFICIENT_CALIBRATION",

    # XAU-only intraday research uses the 5m entry engine with 15m as the
    # hard confirmation timeframe. 1h/4h remain useful context, but making
    # both hard vetoes caused otherwise valid intraday candidates to be
    # suppressed by much slower candles.
    "TIMEFRAME_DISAGREEMENT:1h",
    "TIMEFRAME_DISAGREEMENT:4h",

    # Phase-3 hidden-state heuristics are experimental overlays. They remain
    # visible in the archived Champion evidence, but they must not hard-gate
    # the isolated XAU-only research profile.
    "HIGH_MARKET_ENTROPY",
    "EXTREME_TIMEFRAME_TENSION",
    "MARKET_SHOCK",
    "UNRESOLVED_EVENT_SHOCK",
    "UNSTABLE_HIDDEN_STATE",
    "HIDDEN_COUNCIL_CONFLICT",
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


def research_technical_blocks(veto_codes):
    """Return only vetoes that hard-block the XAU-only research profile."""
    return sorted({
        code
        for code in (veto_codes or [])
        if not _research_ignored_veto(code)
    })


def compose_research_signal(xau_decision, now):
    """Project the XAU technical candidate into the isolated research profile."""

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

    # Preserve the XAU engine's read-only decision evidence for the research
    # presentation. This does not change scoring, vetoes, or execution; it only
    # exposes the council/hidden-state fields already computed by the Champion.
    for field in (
        "buy_score", "sell_score", "raw_score", "edge", "rsi_state", "atr",
        "components", "timeframes", "hidden_state", "analytics_context",
        "intelligence", "session", "signal_candle_close", "expires_at",
    ):
        if field in xau_decision:
            result[field] = xau_decision[field]

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

    return result