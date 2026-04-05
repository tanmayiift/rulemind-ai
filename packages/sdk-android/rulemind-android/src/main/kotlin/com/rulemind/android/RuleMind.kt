package com.rulemind.android

import android.content.Context
import com.rulemind.core.DecisionCache
import com.rulemind.core.RuleMindEngine
import com.rulemind.core.models.Bundle
import com.rulemind.core.models.Decision
import com.rulemind.core.models.ExperimentVariant
import com.rulemind.core.models.ReviewDecision
import com.rulemind.core.models.RuleMindConfig
import com.rulemind.core.models.ServerDecisionRequest
import org.json.JSONObject
import java.util.concurrent.locks.ReentrantReadWriteLock
import kotlin.concurrent.read
import kotlin.concurrent.write

object RuleMind {
    private val lock = ReentrantReadWriteLock()
    private var config: RuleMindConfig? = null
    private var bundleManager: BundleManager? = null
    private var networkClient: NetworkClient? = null
    private var eventLogger: EventLogger? = null
    private var engine: RuleMindEngine = RuleMindEngine()
    private var cache = DecisionCache(256, 300_000)
    private var executionStore: ExecutionStore? = null

    fun initialize(context: Context, config: RuleMindConfig) = lock.write {
        val appContext = context.applicationContext
        val client = NetworkClient(config)
        val manager = BundleManager(appContext, config, client)
        this.config = config
        this.networkClient = client
        this.bundleManager = manager
        this.eventLogger = EventLogger(appContext, config, client)
        this.cache = DecisionCache(config.decisionCacheMaxEntries, config.decisionCacheTtlMs)
        this.executionStore = ExecutionStore(appContext)
        SyncWorker.schedule(appContext, config.bundleSyncIntervalMinutes)
    }

    fun syncNow(): Bundle? = lock.write {
        val before = bundleManager?.currentBundleVersion() ?: 0
        val updated = requireNotNull(bundleManager) { "RuleMind is not initialized." }.syncNow()
        if (updated != null && updated.bundleVersion != before) {
            cache.clearAll()
        }
        updated
    }

    fun health(): Map<String, Any?> = requireNotNull(networkClient) { "RuleMind is not initialized." }.health()

    fun evaluate(policyId: String, payload: Map<String, Any?>, userId: String? = null): Decision {
        val activeConfig = lock.read { requireNotNull(config) { "RuleMind is not initialized." } }
        val manager = lock.read { requireNotNull(bundleManager) { "RuleMind is not initialized." } }
        val activeBundle = manager.currentBundle() ?: syncNow()
        if (activeBundle == null && activeConfig.enableServerFallback) {
            val serverDecision = requireNotNull(networkClient).decide(
                ServerDecisionRequest(
                    policyId = policyId,
                    payload = payload,
                    userId = userId,
                    sdkVersion = activeConfig.sdkVersion,
                ),
            ).copy(source = "sdk_server", policyId = policyId, userId = userId, payload = payload)
            executionStore?.save(serverDecision)
            return serverDecision
        }
        val bundle = requireNotNull(activeBundle) { "No cached bundle available." }
        val variant = bundle.experiments.firstOrNull { it.targetPolicyId == policyId }?.let { experiment ->
            engine.getExperiment(bundle, experiment.id, userId)
        }
        val cacheKey = cache.buildKey(policyId, canonicalJson(payload), variant?.id)
        cache.get(cacheKey)?.takeIf { it.status == "completed" && it.pendingOperations.isEmpty() }?.let { return it }
        val decision = engine.evaluate(bundle, policyId, payload, userId).copy(source = "edge_bundle", policyId = policyId, userId = userId)
        executionStore?.save(decision)
        if (decision.status == "completed" && decision.pendingOperations.isEmpty()) {
            cache.put(policyId, cacheKey, decision)
        }
        syncDecision(decision)
        return decision
    }

    fun flushPendingOperations(): List<Decision> {
        val bundle = lock.read { bundleManager?.currentBundle() } ?: syncNow()
        val updated = mutableListOf<Decision>()
        for (execution in executionStore?.list().orEmpty().filter { decision ->
            decision.pendingOperations.any { it["status"] != "delivered" }
        }) {
            var next = execution
            val refreshedPending = mutableListOf<Map<String, Any?>>()
            val refreshedActionResults = execution.actionResults.map { it.toMutableMap() as MutableMap<String, Any?> }.toMutableList()
            execution.pendingOperations.forEach { operation ->
                if (operation["status"] == "delivered") {
                    refreshedPending += operation
                    return@forEach
                }
                val result = requireNotNull(networkClient).dispatchPendingOperation(operation)
                val updatedOperation = operation.toMutableMap()
                updatedOperation["status"] = if (result["success"] == true) "delivered" else "failed"
                updatedOperation["lastResult"] = result
                refreshedPending += updatedOperation
                val actionIndex = refreshedActionResults.indexOfFirst { it["operationId"] == operation["id"] }
                if (actionIndex >= 0) {
                    refreshedActionResults[actionIndex].putAll(result)
                } else {
                    refreshedActionResults += result.toMutableMap()
                }
            }
            next = next.copy(
                pendingOperations = refreshedPending,
                actionResults = refreshedActionResults,
                source = "edge_bundle",
            )
            val blockingPending = next.pendingOperations.any { it["blocking"] == true && it["status"] != "delivered" }
            val resumePolicyId = next.policyId
            if (next.status == "pending_sync" && !blockingPending && bundle != null && resumePolicyId != null) {
                next = engine.evaluate(bundle, resumePolicyId, next.payload, next.userId, next.copy(status = "running"))
                    .copy(source = "edge_resume", policyId = resumePolicyId, userId = next.userId)
            }
            executionStore?.save(next)
            syncDecision(next)
            updated += next
        }
        return updated
    }

    fun getExecution(executionId: String): Decision? {
        executionStore?.get(executionId)?.let { return it }
        return networkClient?.getExecution(executionId)?.also { executionStore?.save(it) }
    }

    fun resumeExecution(executionId: String, reviewDecision: ReviewDecision): Decision {
        val local = executionStore?.get(executionId)
        val localPolicyId = local?.policyId
        if (local != null && local.status == "paused" && localPolicyId != null) {
            val bundle = lock.read { bundleManager?.currentBundle() } ?: syncNow()
            requireNotNull(bundle) { "No cached bundle available to resume execution." }
            val resumedSeed = local.copy(
                status = "running",
                outcome = if (reviewDecision.decision == "approve") "approve" else "reject",
                reviewResponse = local.reviewResponse + reviewDecision.response,
                pausedAtStep = null,
                currentStepIndex = (local.pausedAtStep ?: local.currentStepIndex) + 1,
                reviewTask = (local.reviewTask ?: emptyMap()) + mapOf(
                    "status" to if (reviewDecision.decision == "approve") "approved" else "rejected",
                    "reviewer_response" to reviewDecision.response,
                    "reviewed_by" to reviewDecision.reviewerId,
                ),
            )
            val resumed = engine.evaluate(bundle, localPolicyId, local.payload, local.userId, resumedSeed)
                .copy(source = "edge_resume", policyId = localPolicyId, userId = local.userId)
            executionStore?.save(resumed)
            syncDecision(resumed)
            return resumed
        }
        val resumed = requireNotNull(networkClient).resumeExecution(executionId, reviewDecision)
        executionStore?.save(resumed)
        return resumed
    }

    fun getExperiment(experimentId: String, userId: String?): ExperimentVariant? {
        val bundle = lock.read { bundleManager?.currentBundle() } ?: return null
        return engine.getExperiment(bundle, experimentId, userId)
    }

    fun logEvent(type: String, data: Map<String, Any?> = emptyMap()) {
        eventLogger?.queue(type, data)
    }

    fun flushEvents() {
        eventLogger?.flush()
    }

    fun clearCache() {
        cache.clearAll()
    }

    private fun syncDecision(decision: Decision) {
        if (decision.executionId.isNullOrBlank() || decision.policyId.isNullOrBlank()) {
            return
        }
        runCatching { networkClient?.syncExecution(decision) }
    }

    private fun canonicalJson(payload: Map<String, Any?>): String = JSONObject(payload).toString()
}
