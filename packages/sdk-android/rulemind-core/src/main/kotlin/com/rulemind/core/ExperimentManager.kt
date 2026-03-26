package com.rulemind.core

import com.rulemind.core.models.Experiment
import com.rulemind.core.models.ExperimentVariant
import java.math.BigInteger
import java.security.MessageDigest

class ExperimentManager {
    fun assign(experiment: Experiment, userId: String?): ExperimentVariant? {
        if (experiment.status != "running" || userId.isNullOrBlank() || experiment.variants.isEmpty()) {
            return null
        }
        val digest = MessageDigest.getInstance("MD5").digest("${experiment.id}:$userId".toByteArray())
        val bucket = BigInteger(1, digest).mod(BigInteger.valueOf(100)).toInt()
        var cumulative = 0
        for (variant in experiment.variants) {
            cumulative += variant.weight
            if (bucket < cumulative) {
                return variant
            }
        }
        return experiment.variants.last()
    }
}
