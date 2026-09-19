package com.arbnor.xauai

import org.json.JSONArray
import org.json.JSONObject
import java.time.Instant
import java.util.Locale

/** Read-only compatibility boundary. Never computes a trading signal or promotes research. */
class Fields(val json: JSONObject) {
    fun value(path: String): Any? {
        var current: Any? = json
        for (key in path.split('.')) current = (current as? JSONObject)?.opt(key)
        return current?.takeUnless { it == JSONObject.NULL }
    }
    fun text(vararg paths: String): String? = paths.firstNotNullOfOrNull { p ->
        (value(p) as? String)?.trim()?.takeIf { it.isNotEmpty() && it != "null" }?.take(4000)
    }
    fun number(vararg paths: String): Double? = paths.firstNotNullOfOrNull { p ->
        when (val v = value(p)) { is Number -> v.toDouble(); is String -> v.toDoubleOrNull(); else -> null }?.takeIf { it.isFinite() }
    }
    fun obj(vararg paths: String): JSONObject? = paths.firstNotNullOfOrNull { value(it) as? JSONObject }
    fun list(vararg paths: String): List<String> = paths.flatMap { p ->
        when (val v = value(p)) {
            is JSONArray -> (0 until minOf(v.length(), 100)).mapNotNull { (v.opt(it) as? String)?.take(4000) }
            is String -> listOf(v.take(4000))
            else -> emptyList()
        }
    }.filter { it.isNotBlank() }.distinct()
}

data class DisplayRow(val label: String, val value: String)
data class Dashboard(val signal: JSONObject, val performance: JSONObject?, val history: List<JSONObject>,
                     val syncedAt: Instant, val warnings: List<String>)

object Presentation {
    const val MISSING = "Not available"
    val directions = setOf("BUY", "SELL", "NO_TRADE")
    fun number(value: Double?, digits: Int = 2): String = value?.let { String.format(Locale.US, "%.${digits}f", it) } ?: MISSING
    fun readable(value: String?): String = value?.replace('_', ' ') ?: MISSING
    fun instant(value: String?): Instant? = try { value?.let { Instant.parse(it) } } catch (_: Exception) { null }
    fun time(value: String?): String = instant(value)?.let {
        java.time.format.DateTimeFormatter.ofPattern("dd MMM yyyy · HH:mm:ss 'UTC'",Locale.US).withZone(java.time.ZoneOffset.UTC).format(it)
    } ?: MISSING
    fun champion(root: JSONObject): Fields = Fields(root.optJSONObject("champion") ?: root)
    fun direction(root: JSONObject): String = champion(root).text("direction", "signal")?.uppercase(Locale.ROOT)?.takeIf { it in directions } ?: "NO_TRADE"
    fun readiness(root: JSONObject): String {
        val f = Fields(root)
        return f.text("readiness.status", "readiness", "system.status", "forward_status.status")
            ?.takeIf { it in setOf("NOT_READY", "DATA_READY", "FORWARD_DEMO_READY") } ?: "NOT_READY"
    }
    fun confidence(root: JSONObject): String {
        val f = champion(root)
        val calibrated = f.number("calibrated_confidence")
        val kind = f.text("confidence_kind")
        if (kind == "EMPIRICAL_DEMO_BETA_BIN" && calibrated != null && calibrated in 0.0..1.0)
            return "${number(calibrated * 100, 1)}% · historical DEMO calibration"
        if (kind?.contains("WARMUP") == true || kind?.contains("UNCALIBRATED") == true)
            return "Uncalibrated · index ${number(f.number("raw_score"), 1)}"
        val legacy = f.number("confidence") ?: return MISSING
        if (legacy !in 0.0..100.0) return MISSING
        return "${number(if (legacy <= 1) legacy * 100 else legacy, 1)}% · backend confidence, calibration unverified"
    }
    fun stale(root: JSONObject, now: Instant): Boolean {
        val f = champion(root)
        val expiry = instant(f.text("expires_at"))
        if (expiry != null && !now.isBefore(expiry)) return true
        val stamp = instant(f.text("timestamp_utc", "timestamp")) ?: return true
        val age = now.epochSecond - stamp.epochSecond
        return age !in 0..300 || f.text("data_status", "data_mode") in setOf("STALE", "DELAYED", "DELAYED_DATA")
    }
    fun dataMode(root: JSONObject, now: Instant, connected: Boolean = true): String {
        val f = champion(root)
        val declared = f.text("data_mode", "data_status", "market_provenance.data_mode") ?: Fields(root).text("data_mode")
        if (containsSynthetic(root)) return "TEST / FIXTURE · research only"
        if (!connected) return "OFFLINE · cached data"
        if (stale(root, now)) return "STALE / TIMESTAMP UNVERIFIED"
        if (declared in setOf("HISTORICAL", "HISTORICAL_POINT_IN_TIME")) return "HISTORICAL"
        if (declared in setOf("DELAYED", "DELAYED_DATA")) return "DELAYED_DATA"
        // Legacy LIVE strings and HTTP success are insufficient evidence for this badge.
        val health = Fields(root).obj("provider_health", "readiness.health", "system.health")
        val channels = listOf("xau", "dxy", "us2y", "us10y", "calendar", "news")
        val verified = health != null && channels.all { channel ->
            val entry = health.optJSONObject(channel) ?: return@all false
            val providers = entry.optJSONArray("providers") ?: return@all false
            entry.optString("status") == "HEALTHY" && (0 until providers.length()).any { i ->
                val row = providers.optJSONObject(i) ?: return@any false
                val native = row.optJSONObject("native") ?: return@any false
                val proof = native.optJSONObject("verification") ?: return@any false
                val observed = instant(row.optString("observed_at")) ?: return@any false
                val received = instant(row.optString("received_at")) ?: return@any false
                val until = instant(proof.optString("entitlement_until")) ?: return@any false
                val checked = instant(native.optString("checked_at")) ?: return@any false
                val symbol=mapOf("xau" to "XAU/USD","dxy" to "DXY","us2y" to "US2Y","us10y" to "US10Y","calendar" to "US_CALENDAR","news" to "GOLD_USD_NEWS")[channel]
                row.optString("data_mode") == "LIVE_DATA" && row.optString("health") == "HEALTHY" &&
                    row.optString("symbol") == symbol && row.optString("provider_identity").isNotBlank() && !checked.isAfter(now) && !checked.isBefore(observed) &&
                    native.opt("live_verified") == true && listOf("authenticated", "real_transport", "entitled", "complete").all { proof.opt(it) == true } &&
                    proof.optString("provider_identity").isNotEmpty() && proof.optString("provider_identity") == row.optString("approval") &&
                    !received.isAfter(now) && !observed.isAfter(received) && until.isAfter(now) &&
                    now.epochSecond - observed.epochSecond in 0 until when(channel) { "xau" -> 150L; "dxy" -> 300L; "calendar" -> 3600L; else -> 900L }
            }
        }
        return if (declared in setOf("LIVE", "LIVE_DATA") && verified && readiness(root) != "NOT_READY") "LIVE_DATA · verified DEMO" else "UNVERIFIED · provider provenance not validated"
    }
    private fun containsSynthetic(value: Any?): Boolean = when(value) {
        is JSONObject -> value.opt("is_synthetic") == true || value.optString("data_mode") in setOf("TEST_DATA", "FIXTURE", "FIXTURE_DATA") ||
            value.optString("data_status") == "FIXTURE_DATA" || value.keys().asSequence().any { containsSynthetic(value.opt(it)) }
        is JSONArray -> (0 until value.length()).any { containsSynthetic(value.opt(it)) }
        else -> false
    }
    fun blueprint(root: JSONObject, now: Instant, connected: Boolean): List<DisplayRow> {
        val f = champion(root)
        if (direction(root) == "NO_TRADE") return listOf(DisplayRow("No active trade setup", f.list("reasons", "veto_codes", "vetoes").joinToString("\n").ifEmpty { "Backend has not supplied a trade setup." }))
        if (f.list("veto_codes","vetoes").isNotEmpty()) return listOf(DisplayRow("Setup withheld",f.list("veto_codes","vetoes").joinToString("\n")))
        if (!connected || stale(root, now)) return listOf(DisplayRow("Setup withheld", "Offline, expired or timestamp-unverified data. Refresh before reviewing levels."))
        return listOf("Entry" to arrayOf("entry"), "Stop Loss" to arrayOf("sl", "stop_loss"), "TP1" to arrayOf("tp1"), "TP2" to arrayOf("tp2"),
            "Risk / reward" to arrayOf("risk_reward", "rr")).map { (label, keys) -> DisplayRow(label, number(f.number(*keys))) } +
            DisplayRow("Direction", direction(root)) + DisplayRow("Setup timestamp", f.text("timestamp_utc", "timestamp") ?: MISSING)
    }
    fun council(root: JSONObject): List<DisplayRow> {
        val f = champion(root)
        val components = listOf("Technical" to "technical", "Momentum" to "momentum", "Market structure" to "market_structure", "Volatility" to "volatility", "Timeframe alignment" to "multi_timeframe_alignment")
        return components.mapNotNull { (label, key) ->
            val comp = f.obj("components.$key", "council.components.$key")
            val sides = comp?.let { c -> listOf("BUY" to "buy", "SELL" to "sell").mapNotNull { (side, field) -> Fields(c).number(field)?.let { "$side ${number(it, 1)}" } } }.orEmpty()
            if (sides.isNotEmpty()) DisplayRow(label, sides.joinToString(" / ") + " · indices")
            else f.number("${key}_score", "scores.${key}_score")?.let { DisplayRow(label, number(it, 1)) }
        } + listOf(
            "Macro" to arrayOf("intelligence.scores.macro_score", "macro_score"),
            "News" to arrayOf("intelligence.scores.news_sentiment_score", "news_sentiment_score"),
            "DGFE fracture" to arrayOf("hidden_state.dgfe.fracture_score", "hidden_state.scores.fracture_score", "fracture_score"),
            "Resilience" to arrayOf("hidden_state.gold_resilience_score", "hidden_state.resilience.gold_resilience_score", "gold_resilience_score"),
            "Liquidity" to arrayOf("hidden_state.dgfe.liquidity_pressure", "hidden_state.scores.liquidity_pressure", "liquidity_pressure"),
            "Entropy" to arrayOf("hidden_state.entropy.score", "hidden_state.entropy.entropy_score", "entropy_score"),
            "Event absorption" to arrayOf("hidden_state.event_absorption.score", "event_absorption_score"),
            "Event risk" to arrayOf("intelligence.scores.event_risk_score", "event_risk_score")
        ).mapNotNull { (label, paths) -> f.number(*paths)?.let { DisplayRow(label, number(it, 1)) } }
    }
    fun legacyMetrics(root: JSONObject): List<DisplayRow> {
        val f = champion(root)
        return listOf("Buy score" to "buy_score", "Sell score" to "sell_score", "RSI" to "rsi", "ATR" to "atr",
            "Efficiency" to "efficiency", "Edge" to "edge", "Structure" to "structure", "Pressure" to "pressure",
            "Volatility" to "volatility", "Breakout bias" to "breakout_bias").mapNotNull { (label, key) ->
            val paths = if(key == "rsi") arrayOf("rsi", "rsi_state") else arrayOf(key)
            val value = f.number(*paths)?.let { number(it, 3) } ?: f.text(*paths)?.takeUnless { it.equals("NaN", true) || it.equals(MISSING, true) }
            value?.let { DisplayRow(label, it) }
        }
    }
    fun market(root: JSONObject): List<DisplayRow> {
        val f = champion(root)
        return listOf("Hidden state" to arrayOf("hidden_state.state", "analytics_context.hidden_state"), "Regime" to arrayOf("regime", "mode"),
            "Session" to arrayOf("session", "analytics_context.session"), "Volatility regime" to arrayOf("analytics_context.volatility_regime", "volatility"),
            "Timeframe alignment" to arrayOf("analytics_context.timeframe_alignment", "timeframe_alignment"),
            "Macro context" to arrayOf("analytics_context.macro_regime", "macro_regime")).map { (label, paths) -> DisplayRow(label, readable(f.text(*paths))) }.filter { it.value != MISSING }
    }
    fun analytics(root: JSONObject?, cohort: String): List<DisplayRow> {
        if (root == null) return listOf(DisplayRow("Evidence", "INSUFFICIENT_FORWARD_DATA"), DisplayRow("Performance", "Not enough data"))
        val f = Fields(root)
        val group = f.obj("cohorts.$cohort")
        // Never use unlabelled legacy totals for Challenger or combine cohorts.
        val data = Fields(group?.optJSONObject("metrics") ?: if (cohort == "CHAMPION" && f.obj("cohorts") == null) root else JSONObject())
        val labels = listOf("Eligible trades" to (if(group!=null) arrayOf("sample_count") else arrayOf("eligible_trades")), "Recorded closed trades" to arrayOf("closed_trades"), "Wins" to arrayOf("wins"), "Losses" to arrayOf("losses"),
            "Breakevens" to arrayOf("breakeven", "breakevens"), "Net R" to arrayOf("net_total_r", "net_r"), "Gross R" to arrayOf("gross_total_r", "total_r"),
            "Expectancy (net R)" to arrayOf("net_expectancy_r", "expectancy"), "Profit factor" to arrayOf("profit_factor"), "Max drawdown (R)" to arrayOf("max_net_drawdown_r", "max_drawdown"),
            "Average winner" to arrayOf("average_winner"), "Average loser" to arrayOf("average_loser"), "Longest losing streak" to arrayOf("longest_losing_streak"),
            "Calibration Brier score" to arrayOf("brier_score"), "Calibration sample count" to arrayOf("calibration_sample_count"), "NO_TRADE percentage" to arrayOf("no_trade_pct"))
        val rate = data.number("win_rate_pct") ?: data.number("hit_rate")?.takeIf { it in 0.0..1.0 }?.times(100)
        val evidence = Fields(group ?: JSONObject()).text("qualification.status") ?: f.text("research_status") ?: "INSUFFICIENT_FORWARD_DATA"
        val insufficient = evidence == "INSUFFICIENT_FORWARD_DATA"
        if (insufficient) return listOf(DisplayRow("Evidence", evidence), DisplayRow("Performance", "Not enough data"))
        return listOf(DisplayRow("Evidence", evidence),
            DisplayRow("Win rate", rate?.takeUnless { insufficient }?.let { "${number(it, 1)}% · observed DEMO sample" } ?: MISSING)) + labels.map { (label, paths) -> DisplayRow(label, number(data.number(*paths)?.takeUnless { insufficient && label == "NO_TRADE percentage" })) }
    }
    fun history(raw: Any?): List<JSONObject> {
        val items = when(raw) { is JSONArray -> raw; is JSONObject -> raw.optJSONArray("items") ?: raw.optJSONArray("history"); else -> null } ?: return emptyList()
        return (0 until minOf(items.length(), 200)).mapNotNull { items.optJSONObject(it) }
    }
    fun historyDataMode(item: JSONObject): String {
        val declared=Fields(item).text("observed_data_status","data_mode","data_status") ?: return MISSING
        return if(declared.uppercase(Locale.ROOT) in setOf("LIVE","LIVE_DATA"))
            "UNVERIFIED · provider provenance not validated" else readable(declared)
    }
    fun historyRole(item: JSONObject): String = Fields(item).text("source", "role", "strategy_role")?.let {
        when { it.contains("CHALLENGER") -> "CHALLENGER · research"; it.contains("SHADOW") -> "SHADOW · research"; it == "CHAMPION" || it == "PHASE3B_CHAMPION" -> "CHAMPION · DEMO"; else -> "Legacy record" }
    } ?: "Legacy record"
    fun attachOutcomes(history: List<JSONObject>, trades: List<JSONObject>): List<JSONObject> = history.map { item ->
        val f=Fields(item); val id=f.text("decision_id", "id")
        val role=f.text("source", "role", "strategy_role")
        val trade=if(role == null || role in setOf("CHAMPION","PHASE3B_CHAMPION")) trades.firstOrNull {
            val t=Fields(it); t.text("decision_id") == id && id != null &&
                t.text("source", "role", "strategy_role") in setOf(null,"CHAMPION","PHASE3B_CHAMPION") && t.text("status") == "CLOSED"
        } else null
        val copy=JSONObject(item.toString())
        if(trade!=null) for(key in listOf("result","status","closed_at","gross_r","r_multiple","net_r","simulated_net_r"))
            if(trade.has(key) && !trade.isNull(key)) copy.put(key,trade.opt(key))
        copy
    }
}
