package com.arbnor.xauai

import java.io.File
import java.time.Instant
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class MobileIntegrationTest {
    @Test fun baseUrlsAndExistingSignalUrlsResolveSafely() {
        for(base in listOf("https://xau-ai-trader-android.onrender.com", "https://parallel.example.invalid/")) {
            val endpoint=ApiClient.signalEndpoint(base)
            assertEquals(base.trimEnd('/')+"/signal",endpoint)
            assertEquals(base.trimEnd('/')+"/history",ApiClient.sibling(endpoint,"history"))
        }
        assertEquals(ApiClient.DEFAULT_ENDPOINT,ApiClient.signalEndpoint(ApiClient.DEFAULT_ENDPOINT))
        for(url in listOf("http://localhost:8000", "https://user:pass@example.invalid", "https://example.invalid/?key=placeholder")) {
            try { ApiClient.signalEndpoint(url); fail() } catch(_:ApiFailure) {}
        }
    }

    @Test fun originalDefaultRemainsLegacy() {
        assertEquals("https://xau-ai-trader-android.onrender.com/signal",ApiClient.DEFAULT_ENDPOINT)
    }

    @Test fun localModernApiResponsesParseThroughProductionModels() {
        // The optional directory is populated by the local HTTP integration harness,
        // never by production Android and never packaged in the APK.
        val directory=System.getenv("MOBILE_API_CONTRACT_DIR")
        org.junit.Assume.assumeNotNull(directory)
        for(scenario in listOf("empty","fixture")) {
            val parser=ApiClient()
            val api=ApiClient { url -> parser.parsePayload(File(directory,"$scenario/${url.substringAfterLast('/')}.json").readText()) }
            val dashboard=api.fetch("https://parallel.example.invalid")
            assertEquals("NO_TRADE",Presentation.direction(dashboard.signal))
            assertEquals("NOT_READY",Presentation.readiness(dashboard.signal))
            assertTrue(dashboard.warnings.isEmpty())
            assertFalse(Presentation.dataMode(dashboard.signal,Instant.now()).startsWith("LIVE"))
            assertTrue(Presentation.analytics(dashboard.performance,"CHAMPION").any { it.value=="INSUFFICIENT_FORWARD_DATA" })
            if(scenario=="fixture") {
                assertTrue(Presentation.dataMode(dashboard.signal,Instant.now()).startsWith("TEST"))
                assertTrue(Presentation.council(dashboard.signal).isNotEmpty())
                assertTrue(dashboard.history.isNotEmpty())
                assertTrue(Presentation.historyRole(dashboard.history[0]).contains("CHAMPION"))
                val challenger=dashboard.signal.getJSONObject("challenger")
                assertEquals("ADAPTIVE_CHALLENGER",challenger.getString("source"))
            }
        }
    }
}
