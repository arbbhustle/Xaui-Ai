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


def _anti_chase_blocks(xau_decision):
    """Block fresh entries when reversal evidence directly opposes the candidate."""
    candidate = xau_decision.get("candidate_direction")
    hidden = xau_decision.get("hidden_state") or {}
    state = hidden.get("state")
    dgfe = hidden.get("dgfe") or {}
    liquidity = hidden.get("liquidity") or {}
    pressure = dgfe.get("directional_pressure")
    reclaim = liquidity.get("reclaim_direction")
    blocks = []
    sign = 1 if candidate == "BUY" else -1 if candidate == "SELL" else 0
    if sign and state == "LIQUIDITY_SWEEP" and reclaim in (-1, 1) and reclaim == -sign:
        blocks.append("OPPOSING_LIQUIDITY_SWEEP")
    if sign and state == "EXHAUSTION" and isinstance(pressure, (int, float)) and pressure * sign < 0:
        blocks.append("OPPOSING_EXHAUSTION_PRESSURE")

    # Entry timing must come from the fast frames, not only from the slower
    # 15m/1h/4h trend vote. The immutable Champion can remain strongly
    # directional after an extended move even when 1m and 5m have already
    # stopped confirming it. In RANGE/TRANSITION conditions that is precisely
    # where Research was chasing late entries near reversals.
    #
    # This is a research-only veto: it never creates/reverses a trade and does
    # not modify Champion scoring. Require at least one fast frame to confirm
    # the candidate when the market is not in a clean trend/expansion state.
    timeframes = xau_decision.get("timeframes") or {}
    fast_scores = []
    for timeframe in ("1min", "5min"):
        value = (timeframes.get(timeframe) or {}).get("bias_score")
        if isinstance(value, (int, float)):
            fast_scores.append(value)

    analytics = xau_decision.get("analytics_context") or {}
    regime = analytics.get("volatility_regime") or xau_decision.get("mode")
    range_or_transition = state == "RANGE" or regime in ("RANGE", "TRANSITION")
    if sign and range_or_transition and len(fast_scores) == 2:
        # A late entry must have real fast-frame participation. Requiring only
        # one weakly directional fast frame (>0.15) still allowed Research to
        # chase a move while the other fast frame had already stalled/reversed.
        # In RANGE/TRANSITION require both 1m and 5m to agree with the candidate;
        # Champion itself remains immutable.
        if any(score * sign <= 0.15 for score in fast_scores):
            blocks.append("FAST_TIMEFRAMES_NOT_CONFIRMING")

    # Do not enter after the move is already mature. The failure pattern in
    # forward research was BUY near the top / SELL near the bottom after a
    # large 5m displacement. Hidden State already measures the last 5m candle
    # against a trailing ATR baseline, so reuse that causal evidence instead
    # of adding a second price-history implementation here.
    #
    # displacement = abs(last 5m body) / baseline ATR * 50, clipped to 100.
    # >= 50 therefore means the latest body alone is at least one baseline ATR.
    # In RANGE/TRANSITION that is too extended for a fresh research entry:
    # wait for a reset/retest rather than chase it.
    displacement = liquidity.get("displacement")
    if (
        sign
        and range_or_transition
        and isinstance(displacement, (int, float))
        and displacement >= 50
    ):
        blocks.append("EXTENDED_MOVE_NO_FRESH_ENTRY")

    # A fresh entry must not be taken after price has already travelled a large
    # distance in the candidate direction. Use the causal 5m candles already
    # embedded in the decision evidence: compare the current entry with the
    # recent 5m swing and normalize by the current 5m ATR. This catches the
    # observed late BUY-near-top / SELL-near-bottom pattern even when the last
    # candle itself is no longer a large displacement candle.
    atr = xau_decision.get("atr")
    entry = xau_decision.get("entry")
    recent_high = liquidity.get("swing_high")
    recent_low = liquidity.get("swing_low")
    if (
        sign
        and isinstance(entry, (int, float))
        and isinstance(atr, (int, float))
        and atr > 0
    ):
        if sign < 0 and isinstance(recent_high, (int, float)):
            travelled_atr = (recent_high - entry) / atr
            if travelled_atr >= 1.5:
                blocks.append("LATE_SELL_AFTER_EXTENDED_DROP")
        elif sign > 0 and isinstance(recent_low, (int, float)):
            travelled_atr = (entry - recent_low) / atr
            if travelled_atr >= 1.5:
                blocks.append("LATE_BUY_AFTER_EXTENDED_RALLY")

    return blocks


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
    # Hidden State is not allowed to promote a trade, but explicit reversal
    # evidence may stop Research from chasing a move after a sweep/exhaustion.
    technical_blocks.extend(_anti_chase_blocks(xau_decision))

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