package com.rulemind.core

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.Decision
import com.rulemind.core.models.Experiment
import com.rulemind.core.models.ExperimentVariant

class RuleMindEngine(
    private val policyExecutor: PolicyExecutor = PolicyExecutor(),
    private val experimentManager: ExperimentManager = ExperimentManager(),
) {
    fun evaluate(bundle: Bundle, policyId: String, payload: Map<String, Any?>, userId: String? = null, resumeFrom: Decision? = null): Decision {
        val policy = bundle.policies.firstOrNull { it.id == policyId }
            ?: error("Unknown policy: $policyId")
        val experiment = findExperiment(bundle, policyId)
        val variant = experiment?.let { experimentManager.assign(it, userId) }
        val decision = policyExecutor.evaluate(bundle, policy, payload, resumeFrom = resumeFrom)
        return decision.copy(
            experimentId = experiment?.id,
            experimentVariant = variant?.id,
            userId = userId ?: resumeFrom?.userId,
        )
    }

    fun getExperiment(bundle: Bundle, experimentId: String, userId: String?): ExperimentVariant? {
        val experiment = bundle.experiments.firstOrNull { it.id == experimentId } ?: return null
        return experimentManager.assign(experiment, userId)
    }

    private fun findExperiment(bundle: Bundle, policyId: String): Experiment? {
        return bundle.experiments.firstOrNull { it.status == "running" && it.targetPolicyId == policyId }
    }
}
