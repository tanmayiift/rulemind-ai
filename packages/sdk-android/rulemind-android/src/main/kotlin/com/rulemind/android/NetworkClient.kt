package com.rulemind.android

import com.rulemind.core.models.BundleEnvelope
import com.rulemind.core.models.BundleFetchResult
import com.rulemind.core.models.Decision
import com.rulemind.core.models.RuleMindConfig
import com.rulemind.core.models.SdkEvent
import com.rulemind.core.models.ServerDecisionRequest
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

class NetworkClient(private val config: RuleMindConfig) {
    private val failureCount = AtomicInteger(0)
    @Volatile private var breakerOpenUntilMs: Long = 0
    private val jsonMediaType = "application/json".toMediaType()

    private val client = OkHttpClient.Builder()
        .connectTimeout(config.connectTimeoutMs, TimeUnit.MILLISECONDS)
        .readTimeout(config.readTimeoutMs, TimeUnit.MILLISECONDS)
        .writeTimeout(config.writeTimeoutMs, TimeUnit.MILLISECONDS)
        .build()

    fun fetchBundle(currentVersion: Int, publicKey: String): BundleFetchResult {
        val request = Request.Builder()
            .url("${config.baseUrl.trimEnd('/')}/sdk/v1/bundle")
            .get()
            .header("X-API-Key", config.apiKey)
            .header("X-SDK-Version", config.sdkVersion)
            .header("X-Bundle-Version", currentVersion.toString())
            .header("X-Client-Public-Key", publicKey)
            .build()
        val response = execute(request)
        if (response.code == 304) {
            return BundleFetchResult(changed = false)
        }
        val body = response.body?.string().orEmpty()
        val json = JSONObject(body)
        return BundleFetchResult(
            changed = true,
            envelope = BundleEnvelope(
                bundleVersion = json.optInt("bundleVersion"),
                encryptedBundle = json.optString("encryptedBundle"),
                encryptedKey = json.optString("encryptedKey"),
                signature = json.optString("signature"),
                checksum = json.optString("checksum"),
                compiledAt = json.optString("compiledAt"),
                expiresAt = json.optString("expiresAt"),
            ),
        )
    }

    fun decide(request: ServerDecisionRequest): Decision {
        val payload = JSONObject()
            .put("policyId", request.policyId)
            .put("payload", JSONObject(request.payload))
            .put("userId", request.userId)
            .put("requestId", request.requestId)
            .put("sdkVersion", request.sdkVersion ?: config.sdkVersion)
        val httpRequest = Request.Builder()
            .url("${config.baseUrl.trimEnd('/')}/sdk/v1/decide")
            .post(payload.toString().toRequestBody(jsonMediaType))
            .header("X-API-Key", config.apiKey)
            .header("X-SDK-Version", config.sdkVersion)
            .build()
        val json = JSONObject(execute(httpRequest).body?.string().orEmpty())
        return Decision(
            outcome = json.optString("outcome"),
            score = json.optDouble("score").takeUnless { it.isNaN() },
            variables = json.optJSONObject("variables")?.let { BundleParser.run { it.toMap() } } ?: emptyMap(),
            ruleResults = json.optJSONArray("ruleResults")?.let { array ->
                buildList {
                    for (index in 0 until array.length()) {
                        add((array.optJSONObject(index) ?: JSONObject()).let { BundleParser.run { it.toMap() } })
                    }
                }
            } ?: emptyList(),
            experimentId = json.optString("experimentId").ifBlank { null },
            experimentVariant = json.optString("experimentVariant").ifBlank { null },
            latencyMs = json.optLong("latencyMs"),
            requestId = json.optString("requestId").ifBlank { null },
            serverOnlyStepsSkipped = json.optJSONArray("serverOnlyStepsSkipped")?.let { array ->
                buildList {
                    for (index in 0 until array.length()) {
                        add(array.optString(index))
                    }
                }
            } ?: emptyList(),
        )
    }

    fun uploadEvents(events: List<SdkEvent>): Boolean {
        if (events.isEmpty()) {
            return true
        }
        val body = JSONObject().put(
            "events",
            JSONArray().apply {
                events.forEach { event ->
                    put(
                        JSONObject()
                            .put("type", event.type)
                            .put("timestamp", event.timestamp)
                            .put("data", JSONObject(event.data)),
                    )
                }
            },
        )
        val request = Request.Builder()
            .url("${config.baseUrl.trimEnd('/')}/sdk/v1/events")
            .post(body.toString().toRequestBody(jsonMediaType))
            .header("X-API-Key", config.apiKey)
            .header("X-SDK-Version", config.sdkVersion)
            .build()
        execute(request).close()
        return true
    }

    private fun execute(request: Request): okhttp3.Response {
        if (System.currentTimeMillis() < breakerOpenUntilMs) {
            throw IOException("Circuit breaker is open.")
        }
        var lastError: IOException? = null
        repeat(config.retryCount + 1) { attempt ->
            try {
                val response = client.newCall(request).execute()
                if (response.isSuccessful || response.code == 304) {
                    failureCount.set(0)
                    return response
                }
                response.close()
                throw IOException("Unexpected HTTP ${response.code}")
            } catch (error: IOException) {
                lastError = error
                val failures = failureCount.incrementAndGet()
                if (failures >= config.circuitBreakerFailureThreshold) {
                    breakerOpenUntilMs = System.currentTimeMillis() + config.circuitBreakerResetMs
                }
                if (attempt < config.retryCount) {
                    Thread.sleep((500L * (attempt + 1)).coerceAtMost(2_000L))
                }
            }
        }
        throw lastError ?: IOException("Network request failed.")
    }
}
