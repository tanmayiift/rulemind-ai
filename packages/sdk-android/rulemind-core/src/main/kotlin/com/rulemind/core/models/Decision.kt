package com.rulemind.core.models

data class Decision(
    val outcome: String,
    val score: Double? = null,
    val variables: Map<String, Any?> = emptyMap(),
    val ruleResults: List<Map<String, Any?>> = emptyList(),
    val experimentId: String? = null,
    val experimentVariant: String? = null,
    val latencyMs: Long = 0,
    val requestId: String? = null,
    val serverOnlyStepsSkipped: List<String> = emptyList(),
)

data class RuleMindConfig(
    val baseUrl: String,
    val apiKey: String,
    val sdkVersion: String = "4.1.0",
    val bundleSyncIntervalMinutes: Long = 15,
    val decisionCacheTtlMs: Long = 300_000,
    val decisionCacheMaxEntries: Int = 256,
    val connectTimeoutMs: Long = 5_000,
    val readTimeoutMs: Long = 10_000,
    val writeTimeoutMs: Long = 10_000,
    val retryCount: Int = 2,
    val circuitBreakerFailureThreshold: Int = 5,
    val circuitBreakerResetMs: Long = 30_000,
    val eventFlushBatchSize: Int = 50,
    val eventFlushIntervalMs: Long = 60_000,
    val enableServerFallback: Boolean = true,
    val serverPublicKeyPem: String? = null,
)

data class ServerDecisionRequest(
    val policyId: String,
    val payload: Map<String, Any?>,
    val userId: String? = null,
    val requestId: String? = null,
    val sdkVersion: String? = null,
)
