package com.rulemind.android

import android.content.Context
import com.rulemind.core.DecisionCache
import com.rulemind.core.RuleMindEngine
import com.rulemind.core.models.Bundle
import com.rulemind.core.models.Decision
import com.rulemind.core.models.ExperimentVariant
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

    fun initialize(context: Context, config: RuleMindConfig) = lock.write {
        val appContext = context.applicationContext
        val client = NetworkClient(config)
        val manager = BundleManager(appContext, config, client)
        this.config = config
        this.networkClient = client
        this.bundleManager = manager
        this.eventLogger = EventLogger(appContext, config, client)
        this.cache = DecisionCache(config.decisionCacheMaxEntries, config.decisionCacheTtlMs)
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

    fun evaluate(policyId: String, payload: Map<String, Any?>, userId: String? = null): Decision {
        val activeConfig = lock.read { requireNotNull(config) { "RuleMind is not initialized." } }
        val manager = lock.read { requireNotNull(bundleManager) { "RuleMind is not initialized." } }
        val activeBundle = manager.currentBundle() ?: syncNow()
        if (activeBundle == null && activeConfig.enableServerFallback) {
            return requireNotNull(networkClient).decide(
                ServerDecisionRequest(
                    policyId = policyId,
                    payload = payload,
                    userId = userId,
                    sdkVersion = activeConfig.sdkVersion,
                ),
            )
        }
        val bundle = requireNotNull(activeBundle) { "No cached bundle available." }
        val variant = bundle.experiments.firstOrNull { it.targetPolicyId == policyId }?.let { experiment ->
            engine.getExperiment(bundle, experiment.id, userId)
        }
        val cacheKey = cache.buildKey(policyId, canonicalJson(payload), variant?.id)
        cache.get(cacheKey)?.let { return it }
        val decision = engine.evaluate(bundle, policyId, payload, userId)
        cache.put(policyId, cacheKey, decision)
        return decision
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

    private fun canonicalJson(payload: Map<String, Any?>): String = JSONObject(payload).toString()
}
