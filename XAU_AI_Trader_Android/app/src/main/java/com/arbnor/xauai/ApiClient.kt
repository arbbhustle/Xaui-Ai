package com.arbnor.xauai

import org.json.JSONObject
import org.json.JSONTokener
import java.net.URI
import java.net.HttpURLConnection
import java.time.Instant

class ApiFailure(val category: String) : Exception(category)

class ApiClient(private val transport: ((String) -> Any)? = null) {
    @Volatile private var activeConnection: HttpURLConnection? = null
    fun cancel() { activeConnection?.disconnect() }
    companion object {
        const val DEFAULT_ENDPOINT = "https://xau-ai-trader-android.onrender.com/signal"
        fun signalEndpoint(address: String): String {
            val uri=validate(address.trim())
            return if(uri.path.isNullOrEmpty() || uri.path=="/") uri.resolve("/signal").toString() else uri.toString()
        }
        fun validate(endpoint: String): URI {
            val uri = try { URI(endpoint) } catch (_: Exception) { throw ApiFailure("Invalid HTTPS endpoint") }
            if (uri.scheme != "https" || uri.host.isNullOrBlank() || uri.userInfo != null || uri.query != null || uri.fragment != null)
                throw ApiFailure("Use an HTTPS endpoint without credentials or query parameters")
            return uri
        }
        fun sibling(endpoint: String, resource: String): String = validate(endpoint).resolve(resource).toString()
    }
    private fun read(endpoint: String): Any {
        if (Thread.currentThread().isInterrupted) throw ApiFailure("Request cancelled")
        validate(endpoint)
        transport?.let { return it(endpoint) }
        val connection = validate(endpoint).toURL().openConnection() as HttpURLConnection
        activeConnection = connection
        try {
            connection.requestMethod = "GET"; connection.instanceFollowRedirects = false
            connection.connectTimeout = 15000; connection.readTimeout = 20000
            connection.setRequestProperty("Accept", "application/json")
            connection.setRequestProperty("User-Agent", "DardaniaXAUTRADE-Android/2.0")
            if (connection.responseCode !in 200..299) throw ApiFailure("Backend unavailable")
            val deadline = System.nanoTime() + 20_000_000_000L
            val bytes = connection.inputStream.use { input ->
                val output = java.io.ByteArrayOutputStream()
                val buffer = ByteArray(8192)
                while (true) {
                    if (Thread.currentThread().isInterrupted) throw ApiFailure("Request cancelled")
                    if (System.nanoTime() > deadline) throw ApiFailure("Request timed out")
                    val count = input.read(buffer); if (count < 0) break
                    output.write(buffer, 0, count)
                    if (output.size() > 2 * 1024 * 1024) throw ApiFailure("Response exceeds safe limit")
                }
                output.toByteArray()
            }
            if (bytes.size > 2 * 1024 * 1024) throw ApiFailure("Response exceeds safe limit")
            return parsePayload(String(bytes, Charsets.UTF_8))
        } catch (e: java.net.SocketTimeoutException) { throw ApiFailure("Request timed out") }
        catch (e: ApiFailure) { throw e }
        catch (_: Exception) { throw ApiFailure("Malformed response or connection unavailable") }
        finally { connection.disconnect(); if (activeConnection === connection) activeConnection = null }
    }
    internal fun parsePayload(body: String): Any {
        var depth = 0; var quoted = false; var escaped = false
        for (c in body) {
            if (quoted) { if (escaped) escaped=false else if(c=='\\') escaped=true else if(c=='"') quoted=false }
            else when(c) { '"' -> quoted=true; '{', '[' -> { depth++; if(depth>32) throw ApiFailure("Response nesting exceeds safe limit") }; '}', ']' -> depth-- }
        }
        try {
            val parser=JSONTokener(body); val value=parser.nextValue()
            if (parser.nextClean() != '\u0000' || (value !is JSONObject && value !is org.json.JSONArray)) throw ApiFailure("Malformed response")
            return value
        } catch(e: ApiFailure) { throw e } catch(_: Exception) { throw ApiFailure("Malformed response") }
    }
    fun fetch(endpoint: String): Dashboard {
        val signalAddress=signalEndpoint(endpoint)
        val signal = read(signalAddress) as? JSONObject ?: throw ApiFailure("Malformed signal response")
        if (Fields(signal).text("direction", "signal", "champion.direction") == null) throw ApiFailure("Signal response is missing a decision")
        val warnings = mutableListOf<String>()
        val performance = try { read(sibling(signalAddress, "performance")) as? JSONObject ?: throw ApiFailure("Malformed performance") }
            catch (_: Exception) { warnings.add("Performance unavailable for this sync"); null }
        val history = try {
            val raw=read(sibling(signalAddress, "history"))
            if (raw !is org.json.JSONArray && (raw !is JSONObject || (raw.optJSONArray("items") == null && raw.optJSONArray("history") == null))) throw ApiFailure("Malformed history")
            Presentation.history(raw)
        }
            catch (_: Exception) { warnings.add("History unavailable for this backend"); emptyList() }
        val trades=try { Presentation.history(read(sibling(signalAddress,"trades"))) }
            catch(_: Exception) { warnings.add("Closed trade outcomes unavailable for this sync"); emptyList() }
        return Dashboard(signal, performance ?: signal.optJSONObject("performance"), Presentation.attachOutcomes(history,trades), Instant.now(), warnings)
    }
}
