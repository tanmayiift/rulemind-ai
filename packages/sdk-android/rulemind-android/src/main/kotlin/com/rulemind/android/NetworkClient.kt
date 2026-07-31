package com.rulemind.android

import com.rulemind.core.models.BundleEnvelope
import com.rulemind.core.models.BundleFetchResult
import com.rulemind.core.models.Decision
import com.rulemind.core.models.ReviewDecision
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
        return DecisionCodec.fromJson(JSONObject(execute(httpRequest).body?.string().orEmpty()))
    }

    fun health(): Map<String, Any?> {
        val request = Request.Builder()
            .url("${config.baseUrl.trimEnd('/')}/sdk/v1/health")
            .get()
            .header("X-API-Key", config.apiKey)
            .header("X-SDK-Version", config.sdkVersion)
            .build()
        return BundleParser.run { JSONObject(execute(request).body?.string().orEmpty()).toMap() }
    }

    fun syncExecution(decision: Decision): Decision {
        val payload = DecisionCodec.toJson(decision)
        val request = Request.Builder()
            .url("${config.baseUrl.trimEnd('/')}/sdk/v1/executions/sync")
            .post(payload.toString().toRequestBody(jsonMediaType))
            .header("X-API-Key", config.apiKey)
            .header("X-SDK-Version", config.sdkVersion)
            .build()
        return DecisionCodec.fromJson(JSONObject(execute(request).body?.string().orEmpty()))
    }

    fun getExecution(executionId: String): Decision? {
        val request = Request.Builder()
            .url("${config.baseUrl.trimEnd('/')}/sdk/v1/executions/$executionId")
            .get()
            .header("X-API-Key", config.apiKey)
            .header("X-SDK-Version", config.sdkVersion)
            .build()
        return runCatching { DecisionCodec.fromJson(JSONObject(execute(request).body?.string().orEmpty())) }.getOrNull()
    }

    fun resumeExecution(executionId: String, reviewDecision: ReviewDecision): Decision {
        val payload = JSONObject()
            .put("decision", reviewDecision.decision)
            .put("reviewerId", reviewDecision.reviewerId)
            .put("response", DecisionCodec.toJsonValue(reviewDecision.response))
        val request = Request.Builder()
            .url("${config.baseUrl.trimEnd('/')}/sdk/v1/executions/$executionId/resume")
            .post(payload.toString().toRequestBody(jsonMediaType))
            .header("X-API-Key", config.apiKey)
            .header("X-SDK-Version", config.sdkVersion)
            .build()
        return DecisionCodec.fromJson(JSONObject(execute(request).body?.string().orEmpty()))
    }

    fun dispatchPendingOperation(operation: Map<String, Any?>): Map<String, Any?> {
        val startedAt = System.currentTimeMillis()
        val url = operation["url"]?.toString().orEmpty()
        val method = operation["method"]?.toString()?.uppercase() ?: "POST"
        if (url.startsWith("rulemind://simulate/")) {
            return mapOf(
                "operationId" to operation["id"],
                "stepId" to operation["stepId"],
                "url" to url,
                "method" to method,
                "requestBody" to operation["requestBody"],
                "responseStatus" to 202,
                "responseBody" to """{"simulated":true}""",
                "latencyMs" to (System.currentTimeMillis() - startedAt),
                "success" to true,
                "status" to "delivered",
            )
        }
        val headersMap = (operation["headers"] as? Map<*, *>)?.entries?.associate { it.key.toString() to (it.value?.toString() ?: "") } ?: emptyMap()
        val requestBuilder = Request.Builder().url(url)
        headersMap.forEach { (key, value) -> requestBuilder.header(key, value) }
        val requestBody = operation["requestBody"]
        val serializedBody = when (val value = DecisionCodec.toJsonValue(requestBody)) {
            null, JSONObject.NULL -> ""
            is JSONObject, is JSONArray -> value.toString()
            else -> value.toString()
        }
        val body = serializedBody.toRequestBody(jsonMediaType)
        val request = when (method) {
            "GET" -> requestBuilder.get().build()
            "PUT" -> requestBuilder.put(body).build()
            "PATCH" -> requestBuilder.patch(body).build()
            else -> requestBuilder.post(body).build()
        }
        return try {
            val response = client.newCall(request).execute()
            val responseBody = response.body?.string().orEmpty()
            mapOf(
                "operationId" to operation["id"],
                "stepId" to operation["stepId"],
                "url" to url,
                "method" to method,
                "requestBody" to requestBody,
                "responseStatus" to response.code,
                "responseBody" to responseBody,
                "latencyMs" to (System.currentTimeMillis() - startedAt),
                "success" to (response.code in 200..299),
                "status" to if (response.code in 200..299) "delivered" else "failed",
            )
        } catch (error: IOException) {
            mapOf(
                "operationId" to operation["id"],
                "stepId" to operation["stepId"],
                "url" to url,
                "method" to method,
                "requestBody" to requestBody,
                "latencyMs" to (System.currentTimeMillis() - startedAt),
                "success" to false,
                "status" to "failed",
                "error" to error.message,
            )
        }
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

    /**
     * Upload a batch of on-device decisions to POST /sdk/v1/decisions and return the server-acked
     * ids. Idempotent server-side (dedupe by client id), so a retry after a lost ack never
     * double-counts. Returns [com.rulemind.core.UploadResult.failure] on any network/HTTP error so
     * the syncer keeps the rows and retries with backoff.
     */
    fun uploadDecisions(batch: List<com.rulemind.core.PendingDecision>): com.rulemind.core.UploadResult {
        if (batch.isEmpty()) return com.rulemind.core.UploadResult.success(emptyList())
        val decisions = JSONArray().apply {
            batch.forEach { put(JSONObject(it.payloadJson)) }
        }
        val request = Request.Builder()
            .url("${config.baseUrl.trimEnd('/')}/sdk/v1/decisions")
            .post(JSONObject().put("decisions", decisions).toString().toRequestBody(jsonMediaType))
            .header("X-API-Key", config.apiKey)
            .header("X-SDK-Version", config.sdkVersion)
            .build()
        return try {
            execute(request).use { response ->
                val payload = response.body?.string().orEmpty()
                val ackedJson = JSONObject(payload).optJSONArray("acked") ?: JSONArray()
                val acked = (0 until ackedJson.length()).map { ackedJson.getString(it) }
                com.rulemind.core.UploadResult.success(acked)
            }
        } catch (_: Exception) {
            com.rulemind.core.UploadResult.failure()
        }
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
