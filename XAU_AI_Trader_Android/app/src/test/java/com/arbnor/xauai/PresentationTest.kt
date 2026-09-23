package com.arbnor.xauai

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.time.Instant

class PresentationTest {
    private val now = Instant.parse("2026-09-18T12:00:10Z")
    private fun signal(direction: String="BUY") = JSONObject().put("direction",direction).put("timestamp_utc",now.toString())
    @Test fun missingFieldsNeverBecomeMeasuredZero() {
        assertEquals(Presentation.MISSING,Presentation.confidence(JSONObject()))
        assertEquals("NOT_READY",Presentation.readiness(JSONObject()))
        assertTrue(Presentation.council(JSONObject()).isEmpty())
        assertEquals("NO_TRADE",Presentation.direction(JSONObject()))
    }
    @Test fun buyAndSellArePreserved() { for(d in listOf("BUY","SELL")) assertEquals(d,Presentation.direction(signal(d))) }
    @Test fun noTradeDoesNotShowFakeLevels() {
        val rows=Presentation.blueprint(signal("NO_TRADE").put("entry",4300).put("reasons",JSONArray().put("Risk veto")),now,true)
        assertFalse(rows.any { it.label=="Entry" }); assertTrue(rows[0].value.contains("Risk veto"))
    }
    @Test fun legacyConfidenceFormatsBothScales() {
        for(v in listOf(.75,75.0)) assertTrue(Presentation.confidence(signal().put("confidence",v)).startsWith("75.0%"))
    }
    @Test fun uncalibratedNeverProbability() {
        val value=Presentation.confidence(signal().put("confidence_kind","UNCALIBRATED_WARMUP").put("confidence",.8).put("raw_score",81))
        assertFalse(value.contains("%")); assertTrue(value.contains("index 81"))
    }
    @Test fun calibratedConfidenceIsNotDividedTwice() {
        assertTrue(Presentation.confidence(signal().put("confidence_kind","EMPIRICAL_DEMO_BETA_BIN").put("calibrated_confidence",.8)).startsWith("80.0%"))
    }
    @Test fun invalidConfidenceUnavailable() { for(v in listOf(-1,101,"NaN",JSONObject.NULL)) assertEquals(Presentation.MISSING,Presentation.confidence(signal().put("confidence",v))) }
    @Test fun legacyLiveLabelIsNotProof() { assertFalse(Presentation.dataMode(signal().put("data_status","LIVE_DATA"),now).startsWith("LIVE_DATA")) }
    @Test fun malformedTimestampNeverLive() { assertTrue(Presentation.dataMode(signal().put("timestamp_utc","bad"),now).startsWith("STALE")) }
    @Test fun researchFreshnessUsesBackendProviderHealthNotPhoneClock() {
        val root=signal().put("profile","XAU_ONLY_RESEARCH_V1")
            .put("timestamp_utc",now.plusSeconds(2).toString())
            .put("readiness",JSONObject().put("status","DATA_READY"))
            .put("provider_health",JSONObject().put("xau",JSONObject().put("status","HEALTHY").put("freshness","FRESH")))
        assertFalse(Presentation.stale(root,now))
        assertEquals("FRESH XAU · research",Presentation.dataMode(root,now))
        root.getJSONObject("provider_health").getJSONObject("xau").put("freshness","STALE")
        assertTrue(Presentation.stale(root,now))
    }
    @Test fun futureTimestampNeverLive() { assertTrue(Presentation.stale(signal().put("timestamp_utc",now.plusSeconds(1)),now)) }
    @Test fun staleDataAgesWithoutAnotherRequest() { assertTrue(Presentation.stale(signal(),now.plusSeconds(301))); assertFalse(Presentation.stale(signal(),now)) }
    @Test fun expiredAndOfflinePlansWithheld() {
        assertEquals("Setup withheld",Presentation.blueprint(signal().put("expires_at",now.minusSeconds(1).toString()),now,true)[0].label)
        assertEquals("Setup withheld",Presentation.blueprint(signal(),now,false)[0].label)
    }
    @Test fun offlineNeverLive() { assertTrue(Presentation.dataMode(signal(),now,false).startsWith("OFFLINE")) }
    @Test fun nestedFixtureCannotBeLive() {
        val root=signal().put("provider_health",JSONObject().put("xau",JSONObject().put("data_mode","FIXTURE")))
        assertTrue(Presentation.dataMode(root,now).startsWith("TEST"))
    }
    @Test fun malformedNumericValuesMissing() {
        assertNull(Fields(JSONObject().put("entry","NaN").put("sl",true)).number("entry")); assertNull(Fields(JSONObject().put("sl",true)).number("sl"))
    }
    @Test fun championNeverFallsBackToChallenger() {
        val root=signal("BUY").put("challenger",signal("SELL"))
        assertEquals("BUY",Presentation.direction(root)); assertEquals("NO_TRADE",Presentation.direction(JSONObject().put("challenger",signal("SELL"))))
    }
    @Test fun wrappedChampionSupported() { assertEquals("SELL",Presentation.direction(JSONObject().put("champion",signal("SELL")))) }
    @Test fun legacyTotalsNeverBecomeChallengerResults() {
        val rows=Presentation.analytics(JSONObject().put("total_r",100).put("wins",10),"ADAPTIVE_CHALLENGER")
        assertFalse(rows.any { it.label=="Wins" })
    }
    @Test fun cohortResultsSeparate() {
        val root=JSONObject().put("research_status","QUALIFIED").put("cohorts",JSONObject().put("CHAMPION",JSONObject().put("metrics",JSONObject().put("net_total_r",2)))
            .put("ADAPTIVE_CHALLENGER",JSONObject().put("metrics",JSONObject().put("net_total_r",99))))
        assertEquals("2.00",Presentation.analytics(root,"CHAMPION").first { it.label=="Net R" }.value)
    }
    @Test fun insufficientEvidenceExplicit() { assertTrue(Presentation.analytics(null,"CHAMPION").any { it.value=="INSUFFICIENT_FORWARD_DATA" }) }
    @Test fun grossNeverRelabelledNet() {
        val rows=Presentation.analytics(JSONObject().put("research_status","QUALIFIED").put("total_r",5),"CHAMPION")
        assertEquals(Presentation.MISSING,rows.first { it.label=="Net R" }.value); assertEquals("5.00",rows.first { it.label=="Gross R" }.value)
    }
    @Test fun zeroFromBackendIsRetained() { assertEquals("0.00",Presentation.analytics(JSONObject().put("research_status","QUALIFIED").put("wins",0),"CHAMPION").first { it.label=="Wins" }.value) }
    @Test fun nullHistorySafe() { assertTrue(Presentation.history(null).isEmpty()); assertTrue(Presentation.history(JSONObject()).isEmpty()) }
    @Test fun historyFormatsSupported() {
        val a=JSONArray().put(signal()).put(JSONObject.NULL)
        assertEquals(1,Presentation.history(a).size); assertEquals(1,Presentation.history(JSONObject().put("items",a)).size)
    }
    @Test fun historyRoleExplicit() {
        assertTrue(Presentation.historyRole(JSONObject().put("source","ADAPTIVE_CHALLENGER")).contains("research"))
        assertEquals("Legacy record",Presentation.historyRole(JSONObject()))
    }
    @Test fun realCouncilKeysMapped() {
        val c=JSONObject().put("buy",65).put("sell",35)
        val r=signal().put("components",JSONObject().put("market_structure",c).put("multi_timeframe_alignment",c))
        assertTrue(Presentation.council(r).first { it.label=="Market structure" }.value.contains("65.0"))
        assertTrue(Presentation.council(r).first { it.label=="Timeframe alignment" }.value.contains("65.0"))
    }
    @Test fun hiddenStatesPassThrough() {
        for(s in listOf("ACCUMULATION","COMPRESSION","LIQUIDITY_SWEEP","FRACTURE","EXPANSION","EXHAUSTION","RANGE","SHOCK"))
            assertEquals(s.replace('_',' '),Presentation.market(signal().put("hidden_state",JSONObject().put("state",s)))[0].value)
    }
    @Test fun notReadyRemainsNotReady() { assertEquals("NOT_READY",Presentation.readiness(signal().put("readiness","NOT_READY"))) }
    @Test fun invalidReadinessFailsClosed() { assertEquals("NOT_READY",Presentation.readiness(signal().put("readiness","LIVE"))) }
    @Test fun httpsOnly() {
        for(url in listOf("http://example.com/signal","https://user:pass@example.com/signal","https://example.com/signal?key=value","https://example.com/signal#secret","garbage")) {
            try { ApiClient.validate(url); fail(url) } catch(_: ApiFailure) { }
        }
    }
    @Test fun modernEndpointAndSiblingsResolved() {
        assertEquals("https://dardania-xautrade-ai-v2.onrender.com/performance",ApiClient.sibling(ApiClient.DEFAULT_ENDPOINT,"performance"))
    }
    @Test fun timestampFormattingIsUtcAndFailsClosed() {
        assertEquals("18 Sep 2026 · 12:00:10 UTC",Presentation.time("2026-09-18T14:00:10+02:00"))
        assertEquals(Presentation.MISSING,Presentation.time("bad"))
    }
    private fun verified(): JSONObject {
        val health=JSONObject()
        for(c in listOf("xau","dxy","us2y","us10y","calendar","news")) {
            val proof=JSONObject().put("authenticated",true).put("real_transport",true).put("entitled",true).put("complete",true)
                .put("provider_identity","unit-proof").put("entitlement_until",now.plusSeconds(3600).toString())
            val row=JSONObject().put("data_mode","LIVE_DATA").put("health","HEALTHY").put("approval","unit-proof")
                .put("symbol",mapOf("xau" to "XAU/USD","dxy" to "DXY","us2y" to "US2Y","us10y" to "US10Y","calendar" to "US_CALENDAR","news" to "GOLD_USD_NEWS")[c])
                .put("provider_identity","unit-provider")
                .put("observed_at",now.toString()).put("received_at",now.toString()).put("native",JSONObject().put("live_verified",true).put("checked_at",now.toString()).put("verification",proof))
            health.put(c,JSONObject().put("status","HEALTHY").put("providers",JSONArray().put(row)))
        }
        return signal().put("data_mode","LIVE_DATA").put("readiness","DATA_READY").put("provider_health",health)
    }
    @Test fun liveRequiresAllCriticalEvidence() { assertTrue(Presentation.dataMode(verified(),now).startsWith("LIVE_DATA")) }
    @Test fun providerHealthStatesNeverPromote() {
        for(s in listOf("DEGRADED","STALE","UNAVAILABLE","CONFLICTING")) {
            val root=verified();root.getJSONObject("provider_health").getJSONObject("xau").put("status",s)
            assertFalse(Presentation.dataMode(root,now).startsWith("LIVE_DATA"))
        }
    }
    @Test fun staleProviderCannotHideBehindFreshDecision() {
        val root=verified();root.getJSONObject("provider_health").getJSONObject("xau").getJSONArray("providers").getJSONObject(0).put("observed_at",now.minusSeconds(151).toString())
        assertFalse(Presentation.dataMode(root,now).startsWith("LIVE_DATA"))
    }
    @Test fun stringAuthenticationIsNotTrue() {
        val root=verified();root.getJSONObject("provider_health").getJSONObject("xau").getJSONArray("providers").getJSONObject(0).getJSONObject("native").getJSONObject("verification").put("authenticated","true")
        assertFalse(Presentation.dataMode(root,now).startsWith("LIVE_DATA"))
    }
    @Test fun wrongProviderInstrumentNeverLive() {
        val root=verified();root.getJSONObject("provider_health").getJSONObject("xau").getJSONArray("providers").getJSONObject(0).put("symbol","XAG/USD")
        assertFalse(Presentation.dataMode(root,now).startsWith("LIVE_DATA"))
    }
    @Test fun contradictoryTradeVetoWithholdsLevels() {
        assertEquals("Setup withheld",Presentation.blueprint(signal().put("veto_codes",JSONArray().put("RISK_VETO")),now,true)[0].label)
    }
    @Test fun unverifiedLegacyLiveTextNeverEchoed() {
        for(mode in listOf("LIVE", "LIVE_DATA")) {
            val label=Presentation.dataMode(signal().put("data_status",mode),now)
            assertFalse(label.contains("LIVE")); assertTrue(label.contains("UNVERIFIED"))
        }
    }
    @Test fun allLegacyMetricsMappedWithoutFabrication() {
        val root=signal()
        val keys=listOf("buy_score","sell_score","rsi","atr","efficiency","edge","structure","pressure","volatility","breakout_bias")
        keys.forEachIndexed { i,key -> root.put(key,i) }
        val rows=Presentation.legacyMetrics(root)
        assertEquals(10,rows.size)
        rows.forEachIndexed { i,row -> assertEquals(Presentation.number(i.toDouble(),3),row.value) }
        assertTrue(Presentation.legacyMetrics(JSONObject()).isEmpty())
    }
    @Test fun partialCouncilShowsOnlySuppliedSideAndMetrics() {
        val root=signal().put("components",JSONObject().put("technical",JSONObject().put("buy",0)).put("momentum",JSONObject()))
        val rows=Presentation.council(root)
        assertEquals(listOf(DisplayRow("Technical","BUY 0.0 · indices")),rows)
    }
    @Test fun legacyTextStructureAndRegimeSurvive() {
        val root=signal().put("structure","BULLISH").put("volatility","NORMAL").put("regime","RANGE").put("session","LONDON")
        assertTrue(Presentation.legacyMetrics(root).contains(DisplayRow("Structure","BULLISH")))
        assertEquals(listOf("Regime","Session","Volatility regime"),Presentation.market(root).map { it.label })
    }
    @Test fun insufficientEvidenceDoesNotDisplayZeroPercent() {
        val root=JSONObject().put("research_status","INSUFFICIENT_FORWARD_DATA").put("win_rate_pct",0).put("no_trade_pct",0).put("wins",0)
        val rows=Presentation.analytics(root,"CHAMPION")
        assertEquals(listOf(DisplayRow("Evidence","INSUFFICIENT_FORWARD_DATA"),DisplayRow("Performance","Not enough data")),rows)
    }
    @Test fun qualifiedMeasuredZeroWinRateRemainsVisible() {
        val root=JSONObject().put("cohorts",JSONObject().put("CHAMPION",JSONObject()
            .put("qualification",JSONObject().put("status","QUALIFIED"))
            .put("metrics",JSONObject().put("win_rate_pct",0))))
        assertTrue(Presentation.analytics(root,"CHAMPION").first { it.label=="Win rate" }.value.startsWith("0.0%"))
    }
    @Test fun legacyRsiStateAliasSupported() {
        assertEquals(listOf(DisplayRow("RSI","54.200")),Presentation.legacyMetrics(signal().put("rsi_state",54.2)))
    }
    @Test fun insufficientEvidenceHidesCountsAndTradeMetrics() {
        val metrics=JSONObject().put("wins",0).put("losses",0).put("sample_count",0).put("net_total_r",0).put("win_rate_pct",0)
        val root=JSONObject().put("cohorts",JSONObject().put("CHAMPION",JSONObject().put("metrics",metrics)
            .put("qualification",JSONObject().put("status","INSUFFICIENT_FORWARD_DATA")))).put("research_status","QUALIFIED")
        assertEquals(listOf("Evidence","Performance"),Presentation.analytics(root,"CHAMPION").map { it.label })
    }
    @Test fun legacyHistoryLiveLabelIsNotVerifiedProvenance() {
        for(key in listOf("observed_data_status","data_mode","data_status"))
            assertEquals("UNVERIFIED · provider provenance not validated",Presentation.historyDataMode(JSONObject().put(key,"LIVE_DATA")))
        assertEquals(Presentation.MISSING,Presentation.historyDataMode(JSONObject()))
    }
}
