package com.rulemind.core

import com.rulemind.core.models.Scorecard

class ScorecardEvaluator {
    fun evaluate(scorecard: Scorecard, variables: Map<String, Any?>): Map<String, Any?> {
        var total = scorecard.baseScore
        val breakdown = mutableListOf<Map<String, Any?>>()
        for (factor in scorecard.bins) {
            val value = when (val raw = variables[factor.variableId]) {
                is Number -> raw.toDouble()
                is String -> raw.toDoubleOrNull() ?: 0.0
                else -> 0.0
            }
            val activeRange = factor.ranges.firstOrNull { range ->
                val min = range.min ?: Double.NEGATIVE_INFINITY
                val max = range.max ?: Double.POSITIVE_INFINITY
                value >= min && value <= max
            }
            val points = activeRange?.points ?: 0
            total += points
            breakdown += mapOf(
                "variable_id" to factor.variableId,
                "value" to value,
                "points" to points,
                "active_range" to activeRange?.let {
                    mapOf(
                        "min" to it.min,
                        "max" to it.max,
                        "points" to it.points,
                    )
                },
            )
        }
        return mapOf(
            "score" to total.coerceIn(0, scorecard.maxScore),
            "base_score" to scorecard.baseScore,
            "max_score" to scorecard.maxScore,
            "breakdown" to breakdown,
        )
    }
}
