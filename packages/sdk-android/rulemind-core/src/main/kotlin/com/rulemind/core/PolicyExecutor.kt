package com.rulemind.core

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.Decision
import com.rulemind.core.models.Policy

class PolicyExecutor(
    private val variableVm: VariableVM = VariableVM(),
    private val ruleEvaluator: RuleEvaluator = RuleEvaluator(),
    private val scorecardEvaluator: ScorecardEvaluator = ScorecardEvaluator(),
) {
    fun evaluate(bundle: Bundle, policy: Policy, payload: Map<String, Any?>): Decision {
        val startedAt = System.currentTimeMillis()
        val variables = mutableMapOf<String, Any?>()
        bundle.variables
            .filter { it.compilable && !bundle.serverOnlyVariables.contains(it.id) }
            .forEach { variable ->
                variables[variable.id] = variableVm.evaluate(variable, payload, variables)
            }

        val ruleResults = mutableListOf<Map<String, Any?>>()
        val skippedSteps = mutableListOf<String>()
        var outcome = policy.defaultOutcome ?: "review"
        var score: Double? = null

        for (step in policy.steps) {
            val refId = step.refId ?: step.ref
            when (step.type) {
                "rule" -> {
                    val rule = bundle.rules.firstOrNull { it.id == refId } ?: continue
                    val result = ruleEvaluator.evaluate(rule, variables)
                    ruleResults += result
                    if ((result["passed"] as? Boolean) == true) {
                        outcome = (result["outcome"] as? String) ?: outcome
                    }
                }

                "scorecard" -> {
                    val scorecard = bundle.scorecards.firstOrNull { it.id == refId } ?: continue
                    score = (scorecardEvaluator.evaluate(scorecard, variables)["score"] as? Number)?.toDouble()
                }

                "outcome" -> outcome = refId ?: step.label ?: outcome

                "action", "transform", "review_gate" -> skippedSteps += step.id ?: refId ?: step.type

                else -> skippedSteps += step.id ?: refId ?: step.type
            }
        }

        return Decision(
            outcome = outcome,
            score = score,
            variables = variables,
            ruleResults = ruleResults,
            latencyMs = System.currentTimeMillis() - startedAt,
            serverOnlyStepsSkipped = skippedSteps.distinct(),
        )
    }
}
