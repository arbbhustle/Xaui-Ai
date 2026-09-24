package com.arbnor.xauai

import org.json.JSONObject
import org.json.JSONArray
import org.junit.Assert.*
import org.junit.Test

class ApiClientTest {
    @Test fun researchEndpointIsSingleRequestFastPath() {
        val urls=mutableListOf<String>()
        val api=ApiClient { url ->
            urls.add(url)
            JSONObject().put("direction","SELL").put("xau",JSONObject()
                .put("signal_candle_close","2026-09-23T17:15:00Z")
                .put("expires_at","2026-09-23T17:20:00Z"))
        }
        val data=api.fetch(ApiClient.DEFAULT_ENDPOINT)
        assertEquals("SELL",data.signal.getString("direction"))
        assertEquals(listOf(ApiClient.DEFAULT_ENDPOINT),urls)
        assertTrue(data.warnings.isEmpty())
    }
    @Test fun optionalFailuresDoNotDiscardSignal() {
        val api=ApiClient { url -> if(url.endsWith("signal")) JSONObject().put("direction","SELL") else throw ApiFailure("Unavailable") }
        val data=api.fetch(ApiClient.STRICT_ENDPOINT)
        assertEquals("SELL",data.signal.getString("direction")); assertNull(data.performance); assertTrue(data.history.isEmpty()); assertEquals(3,data.warnings.size)
    }
    @Test fun embeddedPerformanceFallbackSupported() {
        val api=ApiClient { url -> if(url.endsWith("signal")) JSONObject().put("direction","NO_TRADE").put("performance",JSONObject().put("wins",2)) else throw ApiFailure("Unavailable") }
        assertEquals(2,api.fetch(ApiClient.STRICT_ENDPOINT).performance!!.getInt("wins"))
    }
    @Test fun missingDecisionRejected() {
        try { ApiClient { JSONObject().put("error","unavailable") }.fetch(ApiClient.DEFAULT_ENDPOINT); fail() } catch(_: ApiFailure) { }
    }
    @Test fun malformedOptionalHistoryReported() {
        val api=ApiClient { url -> when { url.endsWith("signal")->JSONObject().put("direction","BUY"); else->JSONObject() } }
        assertTrue(api.fetch(ApiClient.STRICT_ENDPOINT).warnings.any { it.contains("History") })
    }
    @Test fun bodyParsingRejectsTrailingOrPrimitiveContent() {
        for(body in listOf("{}garbage","null","false","23","{bad")) {
            try { ApiClient().parsePayload(body); fail(body) } catch(_: ApiFailure) { }
        }
    }
    @Test fun nestingLimitDoesNotCrash() {
        try { ApiClient().parsePayload("[".repeat(40)+"]".repeat(40)); fail() } catch(_: ApiFailure) { }
    }
    @Test fun bracketsInStringsDoNotCountAsNesting() { assertTrue(ApiClient().parsePayload("{\"message\":\"${"[".repeat(40)}\"}") is JSONObject) }
    @Test fun historyOnlyUsesBackendSiblingEndpoints() {
        val urls=mutableListOf<String>()
        ApiClient { url -> urls.add(url); if(url.endsWith("signal")) JSONObject().put("direction","NO_TRADE") else if(url.endsWith("history")) JSONArray() else JSONObject() }.fetch(ApiClient.STRICT_ENDPOINT)
        assertEquals(listOf("signal","performance","history","trades"),urls.map { it.substringAfterLast('/') })
        assertTrue(urls.all { it.startsWith("https://dardania-xautrade-ai-v2.onrender.com/") })
    }
    @Test fun closedOutcomesJoinedByDecisionIdentity() {
        val history=JSONObject().put("decision_id","one").put("direction","BUY")
        val trade=JSONObject().put("decision_id","one").put("status","CLOSED").put("r_multiple",2).put("result","TP2")
        val merged=Presentation.attachOutcomes(listOf(history),listOf(trade))[0]
        assertEquals(2,merged.getInt("r_multiple")); assertFalse(history.has("r_multiple"))
    }
    @Test fun challengerOutcomesCannotJoinChampionHistory() {
        val history=JSONObject().put("decision_id","one").put("direction","BUY")
        val trade=JSONObject().put("decision_id","one").put("source","ADAPTIVE_CHALLENGER").put("status","CLOSED").put("r_multiple",99)
        assertFalse(Presentation.attachOutcomes(listOf(history),listOf(trade))[0].has("r_multiple"))
    }
    @Test fun researchEndpointParsesEmbeddedResearchHistoryItems() {
        val recorded=JSONObject()
            .put("direction","BUY")
            .put("candidate_direction","BUY")
            .put("entry",4298.54)
            .put("timestamp_utc","2026-09-24T01:00:00Z")
            .put("status","RECORDED_RESEARCH_SETUP")
        val payload=JSONObject()
            .put("direction","NO_TRADE")
            .put("research_history",JSONObject()
                .put("items",JSONArray().put(recorded))
                .put("source","XAU_ONLY_RESEARCH_V1")
                .put("execution","DEMO_ONLY"))
        val data=ApiClient { payload }.fetch(ApiClient.DEFAULT_ENDPOINT)
        assertEquals(1,data.history.size)
        assertEquals("BUY",data.history[0].getString("direction"))
        assertEquals(4298.54,data.history[0].getDouble("entry"),0.0)
    }
}
