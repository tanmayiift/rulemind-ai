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
    val executionId: String? = null,
    val status: String = "completed",
    val trace: List<Map<String, Any?>> = emptyList(),
    val scorecardResults: Map<String, Map<String, Any?>> = emptyMap(),
    val actionResults: List<Map<String, Any?>> = emptyList(),
    val pendingOperations: List<Map<String, Any?>> = emptyList(),
    val reviewTask: Map<String, Any?>? = null,
    val explainability: Map<String, Any?> = emptyMap(),
    val auditSummary: Map<String, Any?> = emptyMap(),
    val source: String? = null,
    val policyId: String? = null,
    val userId: String? = null,
    val payload: Map<String, Any?> = emptyMap(),
    val transformOutputs: Map<String, Map<String, Any?>> = emptyMap(),
    val currentStepIndex: Int = 0,
    val pausedAtStep: Int? = null,
    val reviewResponse: Map<String, Any?> = emptyMap(),
)

data class ReviewDecision(
    val decision: String,
    val response: Map<String, Any?> = emptyMap(),
    val reviewerId: String? = null,
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
