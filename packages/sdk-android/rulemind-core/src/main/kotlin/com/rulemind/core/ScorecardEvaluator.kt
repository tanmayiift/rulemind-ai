package com.rulemind.core

import com.rulemind.core.models.ScoreFactor
import com.rulemind.core.models.ScoreRange
import com.rulemind.core.models.Scorecard
import com.rulemind.core.models.WoeRange
import kotlin.math.ln
import kotlin.math.truncate

/**
 * Offline scorecard scoring, byte-for-byte with the Python core (`app/logic.py:evaluate_scorecard`,
 * the declared source of truth for cross-engine semantics).
 *
 * This used to implement POINTS ONLY and silently ignore `scoring_method`, `weight`, `coefficient`,
 * `woe_values`, `intercept`, `pdo`, `target_score`, `target_odds`, `formula` and `metrics` — so a
 * WoE scorecard returned a *different number* on-device than the server returned for the same
 * applicant, with no error and no signal. All four methods are implemented here:
 *
 *  - **points**    `clamp(base_score + Σ trunc(points × weight))`
 *  - **weighted**  the same path; `weight` is simply not 1.0 (Python has no separate branch)
 *  - **woe**       `clamp(round(offset + factor × (intercept + Σ coefficient × woe)))`
 *  - **formula**   `clamp(trunc(SafeFormula(formula)))`, falling back to `base_score` on error
 *
 * Every clamp is `max(0, min(max_score, x))`, and the WoE `round` is Python's round-half-to-EVEN.
 */
class ScorecardEvaluator {

    fun evaluate(
        scorecard: Scorecard,
        variables: Map<String, Any?>,
        variableLookup: Map<String, Map<String, Any?>> = emptyMap(),
    ): Map<String, Any?> {
        val baseScore = scorecard.baseScore
        val maxScore = scorecard.maxScore
        val scoringMethod = scorecard.scoringMethod

        val breakdown = mutableListOf<Map<String, Any?>>()
        val woeDetails = mutableListOf<Map<String, Any?>>()
        var totalPoints = 0L
        var totalWoeScore = 0.0
        var totalIv = 0.0

        for (factor in scorecard.bins) {
            val variableId = factor.variableId
            // Python: `variable_values.get(variable_id, 0)` — a MISSING variable scores as 0, a
            // variable explicitly set to null matches no bin at all (float(None) raises).
            val value: Any? = if (variableId != null && variables.containsKey(variableId)) variables[variableId] else 0L
            val numericValue = pythonFloat(value)
            val lookup = if (variableId != null) variableLookup[variableId] else null

            val activeRange = activeRange(factor, numericValue)
            val activeWoeRange = activeWoeRange(factor, numericValue)

            val points = activeRange?.points ?: 0
            // Python `int(points * weight)` truncates TOWARD ZERO — not floor, not round.
            val weightedPoints = truncateToLong(points * factor.weight)
            totalPoints += weightedPoints

            val woe = activeWoeRange?.woe ?: 0.0
            val iv = activeWoeRange?.iv ?: 0.0
            totalIv += iv
            totalWoeScore += factor.coefficient * woe

            if (activeWoeRange != null) {
                woeDetails += linkedMapOf(
                    "variable_id" to variableId,
                    "variable_name" to (lookup?.get("name") ?: variableId),
                    "value" to value,
                    "woe" to woe,
                    "iv" to iv,
                    "coefficient" to factor.coefficient,
                    "contribution" to factor.coefficient * woe,
                    "event_rate" to activeWoeRange.eventRate,
                    "non_event_rate" to activeWoeRange.nonEventRate,
                )
            }

            breakdown += linkedMapOf(
                "variable_id" to variableId,
                "variable_name" to (lookup?.get("name") ?: variableId),
                "source_id" to lookup?.get("source_id"),
                "value" to value,
                "active_range" to activeRange?.let {
                    linkedMapOf<String, Any?>("min" to it.min, "max" to it.max, "points" to it.points)
                },
                "points" to points,
                "weighted_points" to weightedPoints,
                "weight" to factor.weight,
                "woe" to woe,
                "coefficient" to factor.coefficient,
            )
        }

        val score: Int = when (scoringMethod) {
            "woe" -> {
                // score = offset + factor × (intercept + Σ coefficient × WoE), where
                // factor = PDO / ln(2) and offset = target_score − factor × ln(target_odds).
                val factorValue = if (scorecard.pdo > 0) scorecard.pdo / ln(2.0) else 1.0
                val offset = if (scorecard.targetOdds > 0) {
                    scorecard.targetScore - factorValue * ln(scorecard.targetOdds)
                } else {
                    scorecard.targetScore
                }
                val raw = offset + factorValue * (scorecard.intercept + totalWoeScore)
                clamp(FormulaFunctions.bankersRound(raw), maxScore)
            }

            "formula" -> {
                val formula = scorecard.formula ?: DEFAULT_FORMULA
                val context = formulaContext(scorecard, variables, breakdown, totalPoints, totalWoeScore)
                val result = evaluateFormula(formula, context)
                // Python: `int(result) if isinstance(result, (int, float)) else base_score`.
                // A formula that errors scores base_score — it never silently falls back to points.
                val raw = when (result) {
                    is Boolean -> if (result) 1L else 0L
                    is Number -> truncateToLong(result.toDouble())
                    else -> baseScore.toLong()
                }
                clamp(raw, maxScore)
            }

            else -> clamp(baseScore.toLong() + totalPoints, maxScore)
        }

        val metrics = linkedMapOf<String, Any?>()
        for (metric in scorecard.metrics) {
            val context = formulaContext(
                scorecard, variables, breakdown, totalPoints, totalWoeScore,
                // Ordering matters: Python seeds these first, so a factor whose variable_id is
                // literally "score" or "total_iv" shadows them.
                extraBase = linkedMapOf("score" to score.toLong(), "total_iv" to totalIv),
            )
            metrics[metric.name] = linkedMapOf(
                "value" to evaluateFormula(metric.formula, context),
                "unit" to metric.unit,
                "category" to metric.category,
            )
        }

        val result = linkedMapOf<String, Any?>(
            "score" to score,
            "base_score" to baseScore,
            "max_score" to maxScore,
            "scoring_method" to scoringMethod,
            "breakdown" to breakdown,
        )
        if (woeDetails.isNotEmpty()) {
            result["woe_details"] = woeDetails
            result["information_value"] = totalIv
        }
        if (metrics.isNotEmpty()) {
            result["metrics"] = metrics
        }
        return result
    }

    // ── Bin matching ──

    private fun activeRange(factor: ScoreFactor, value: Double?): ScoreRange? {
        if (value == null) return null
        return factor.ranges.firstOrNull { it.matchable && value >= it.lowerBound && value <= it.upperBound }
    }

    private fun activeWoeRange(factor: ScoreFactor, value: Double?): WoeRange? {
        if (value == null) return null
        return factor.woeValues.firstOrNull { it.matchable && value >= it.lowerBound && value <= it.upperBound }
    }

    // ── Formula plumbing ──

    private fun formulaContext(
        scorecard: Scorecard,
        variables: Map<String, Any?>,
        breakdown: List<Map<String, Any?>>,
        totalPoints: Long,
        totalWoeScore: Double,
        extraBase: Map<String, Any?> = emptyMap(),
    ): LinkedHashMap<String, Any?> {
        val context = linkedMapOf<String, Any?>()
        context.putAll(extraBase)
        context["base_score"] = scorecard.baseScore.toLong()
        context["max_score"] = scorecard.maxScore.toLong()
        context["total_points"] = totalPoints
        context["total_woe_score"] = totalWoeScore
        context["variables"] = variables
        for (entry in breakdown) {
            // A bin with no variable_id binds the Python dict key `None`, which no formula Name
            // can ever reference — so there is nothing to bind here either.
            val id = entry["variable_id"] as String? ?: continue
            context[id] = entry["value"]
        }
        // `scope.setdefault` in the Python core: these are names, never a reachable builtins map.
        context.putIfAbsent("True", true)
        context.putIfAbsent("False", false)
        context.putIfAbsent("None", null)
        return context
    }

    /**
     * Mirrors `logic._evaluate_formula`: a bad formula yields `{"error": ...}` rather than raising
     * into the scoring path, so the caller sees the failure instead of a plausible wrong number.
     */
    private fun evaluateFormula(formula: String, context: Map<String, Any?>): Any? = try {
        SafeFormula.evaluate(formula, context)
    } catch (error: FormulaError) {
        mapOf("error" to (error.message ?: "formula evaluation failed"))
    }

    // ── Numeric helpers ──

    /** Python's `float(x)`, returning null where `float()` would raise TypeError/ValueError. */
    private fun pythonFloat(value: Any?): Double? = when (value) {
        is Boolean -> if (value) 1.0 else 0.0
        is Number -> value.toDouble()
        is String -> value.trim().toDoubleOrNull()
        else -> null
    }

    private fun truncateToLong(value: Double): Long =
        if (value.isNaN() || value.isInfinite()) 0L else truncate(value).toLong()

    private fun clamp(value: Long, maxScore: Int): Int =
        maxOf(0L, minOf(maxScore.toLong(), value)).toInt()

    private companion object {
        const val DEFAULT_FORMULA = "base_score + total_points"
    }
}
