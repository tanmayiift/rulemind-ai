package com.rulemind.core

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.Decision
import com.rulemind.core.models.Policy
import com.rulemind.core.models.PolicyStep
import java.util.UUID

class PolicyExecutor(
    private val variableVm: VariableVM = VariableVM(),
    private val ruleEvaluator: RuleEvaluator = RuleEvaluator(),
    private val scorecardEvaluator: ScorecardEvaluator = ScorecardEvaluator(),
) {
    private fun mergeOutcome(current: String?, candidate: String?): String {
        val currentValue = current ?: "pending"
        val candidateValue = candidate ?: currentValue
        val precedence = mapOf(
            "pending" to 0,
            "pass" to 1,
            "approve" to 2,
            "review" to 3,
            "reject" to 4,
        )
        val currentRank = precedence[currentValue] ?: 0
        val candidateRank = precedence[candidateValue] ?: 0
        return when {
            candidateRank > currentRank -> candidateValue
            candidateRank < currentRank -> currentValue
            currentValue == "pass" && candidateValue == "approve" -> candidateValue
            else -> currentValue
        }
    }

    fun evaluate(bundle: Bundle, policy: Policy, payload: Map<String, Any?>, resumeFrom: Decision? = null): Decision {
        val startedAt = System.currentTimeMillis()
        val executionId = resumeFrom?.executionId ?: UUID.randomUUID().toString()
        val effectivePayload = resumeFrom?.payload?.takeIf { it.isNotEmpty() } ?: payload
        val variables = resumeFrom?.variables?.toMutableMap() ?: mutableMapOf()
        val ruleResults = resumeFrom?.ruleResults?.map { it.toMutableMap() as Map<String, Any?> }.orEmpty().toMutableList()
        val scorecardResults = resumeFrom?.scorecardResults?.toMutableMap() ?: mutableMapOf()
        val transformOutputs = resumeFrom?.transformOutputs?.toMutableMap() ?: mutableMapOf()
        val actionResults = resumeFrom?.actionResults?.map { it.toMutableMap() as Map<String, Any?> }.orEmpty().toMutableList()
        val pendingOperations = resumeFrom?.pendingOperations?.map { it.toMutableMap() as Map<String, Any?> }.orEmpty().toMutableList()
        val trace = resumeFrom?.trace?.map { it.toMutableMap() as Map<String, Any?> }.orEmpty().toMutableList()
        val skippedSteps = resumeFrom?.serverOnlyStepsSkipped?.toMutableList() ?: mutableListOf()
        var outcome = resumeFrom?.outcome ?: "pending"
        var score: Double? = resumeFrom?.score
        var status = resumeFrom?.status ?: "running"
        var currentStepIndex = resumeFrom?.currentStepIndex ?: 0
        var pausedAtStep = resumeFrom?.pausedAtStep
        var reviewTask = resumeFrom?.reviewTask
        val reviewResponse = resumeFrom?.reviewResponse?.toMutableMap() ?: mutableMapOf()

        if (resumeFrom != null && reviewResponse.isNotEmpty()) {
            reviewResponse.forEach { (key, value) ->
                if (key != "decision") {
                    variables[key] = value
                }
            }
            when (reviewResponse["decision"]?.toString()) {
                "approve" -> outcome = "approve"
                "reject" -> outcome = "reject"
            }
        }

        if (resumeFrom == null) {
            bundle.variables
                .filter { it.compilable && !bundle.serverOnlyVariables.contains(it.id) }
                .forEach { variable ->
                    variables[variable.id] = variableVm.evaluate(variable, effectivePayload, variables)
                }
        }

        for (index in currentStepIndex until policy.steps.size) {
            val step = policy.steps[index]
            currentStepIndex = index
            val config = step.config
            val condition = config["condition"]?.toString()
            if (!condition.isNullOrBlank() && !evaluateCondition(condition, contextView(effectivePayload, variables, scorecardResults, transformOutputs, outcome, executionId, reviewResponse))) {
                trace += mapOf("step" to step.toTraceStep(), "skipped" to true, "reason" to "Condition not met: $condition")
                continue
            }

            val stepStarted = System.currentTimeMillis()
            var result: Map<String, Any?>? = null
            var error: String? = null
            when (step.type) {
                "connector" -> {
                    val refId = step.refId ?: step.ref
                    val sourcePayload = (effectivePayload[refId] as? Map<*, *>)?.keys?.map { it.toString() } ?: emptyList()
                    result = mapOf(
                        "status" to "ok",
                        "payloadKeys" to sourcePayload.sorted(),
                    )
                }

                "rule" -> {
                    val refId = step.refId ?: step.ref
                    val rule = bundle.rules.firstOrNull { it.id == refId }
                    if (rule == null) {
                        skippedSteps += step.id ?: refId ?: step.type
                        error = "Unknown rule: $refId"
                    } else {
                        result = ruleEvaluator.evaluate(rule, variables)
                        ruleResults += result
                        outcome = mergeOutcome(outcome, result["outcome"]?.toString())
                    }
                }

                "scorecard" -> {
                    val refId = step.refId ?: step.ref
                    val scorecard = bundle.scorecards.firstOrNull { it.id == refId }
                    if (scorecard == null) {
                        skippedSteps += step.id ?: refId ?: step.type
                        error = "Unknown scorecard: $refId"
                    } else {
                        result = scorecardEvaluator.evaluate(scorecard, variables)
                        if (refId != null) {
                            scorecardResults[refId] = result
                        }
                        score = (result["score"] as? Number)?.toDouble()
                    }
                }

                "transform" -> {
                    result = executeTransform(step, effectivePayload, variables, scorecardResults, transformOutputs, outcome, executionId, reviewResponse)
                    val outputKey = step.config["outputKey"]?.toString() ?: "transformed"
                    transformOutputs[outputKey] = result
                }

                "action" -> {
                    val pending = queueAction(step, effectivePayload, variables, scorecardResults, transformOutputs, outcome, executionId, reviewResponse)
                    pendingOperations += pending
                    result = mapOf(
                        "operationId" to pending["id"],
                        "stepId" to (step.id ?: step.refId ?: step.ref ?: step.type),
                        "queued" to true,
                        "success" to false,
                        "status" to "queued",
                        "blocking" to (pending["blocking"] == true),
                        "url" to pending["url"],
                        "method" to pending["method"],
                        "requestBody" to pending["requestBody"],
                    )
                    actionResults += result
                    if (pending["blocking"] == true) {
                        status = "pending_sync"
                        currentStepIndex = index + 1
                    }
                }

                "review_gate" -> {
                    reviewTask = createReviewTask(step, variables, ruleResults, scorecardResults, outcome, executionId)
                    result = mapOf("paused" to true, "reviewTaskId" to reviewTask["id"])
                    status = "paused"
                    pausedAtStep = index
                }

                "outcome" -> {
                    outcome = mergeOutcome(
                        outcome,
                        step.refId ?: step.ref ?: step.config["outcome"]?.toString() ?: step.label ?: outcome,
                    )
                    result = mapOf("outcome" to outcome)
                }

                else -> {
                    skippedSteps += step.id ?: step.refId ?: step.ref ?: step.type
                    result = mapOf("skipped" to true)
                }
            }

            trace += mapOf(
                "step" to step.toTraceStep(),
                "result" to result,
                "error" to error,
                "duration_ms" to (System.currentTimeMillis() - stepStarted),
            )

            if (status == "paused" || status == "pending_sync") {
                break
            }
        }

        if (status == "running") {
            status = "completed"
            currentStepIndex = policy.steps.size
        }
        if (outcome == "pending") {
            outcome = policy.defaultOutcome ?: "review"
        }

        val latencyMs = System.currentTimeMillis() - startedAt
        return Decision(
            outcome = outcome,
            score = score,
            variables = variables,
            ruleResults = ruleResults,
            latencyMs = latencyMs,
            serverOnlyStepsSkipped = skippedSteps.distinct(),
            executionId = executionId,
            status = status,
            trace = trace,
            scorecardResults = scorecardResults,
            actionResults = actionResults,
            pendingOperations = pendingOperations,
            reviewTask = reviewTask,
            explainability = mapOf(
                "rules" to ruleResults,
                "scorecards" to scorecardResults,
                "trace" to trace,
            ),
            auditSummary = mapOf(
                "traceSteps" to trace.size,
                "pendingOperationCount" to pendingOperations.count { it["status"] != "delivered" },
                "actionCount" to actionResults.size,
                "reviewTaskId" to reviewTask?.get("id"),
            ),
            policyId = policy.id,
            payload = effectivePayload,
            transformOutputs = transformOutputs,
            currentStepIndex = currentStepIndex,
            pausedAtStep = pausedAtStep,
            reviewResponse = reviewResponse,
        )
    }

    private fun executeTransform(
        step: PolicyStep,
        payload: Map<String, Any?>,
        variables: Map<String, Any?>,
        scorecardResults: Map<String, Map<String, Any?>>,
        transformOutputs: Map<String, Map<String, Any?>>,
        outcome: String,
        executionId: String,
        reviewResponse: Map<String, Any?>,
    ): Map<String, Any?> {
        val mapping = step.config["mapping"] as? Map<*, *> ?: emptyMap<String, Any?>()
        val result = linkedMapOf<String, Any?>()
        val context = contextView(payload, variables, scorecardResults, transformOutputs, outcome, executionId, reviewResponse)
        mapping.forEach { (key, value) ->
            val target = key?.toString() ?: return@forEach
            result[target] = when (value) {
                is String -> if (value.startsWith("$.")) resolvePath(context, value.removePrefix("$."))
                else resolveTemplate(value, context)
                is Map<*, *> -> {
                    val expr = value["expr"]?.toString()
                    if (expr.isNullOrBlank()) {
                        value.mapKeys { it.key.toString() }.mapValues { entry -> resolveTemplate(entry.value, context) }
                    } else {
                        evaluateExpression(expr, context)
                    }
                }
                else -> value
            }
        }
        return result
    }

    private fun queueAction(
        step: PolicyStep,
        payload: Map<String, Any?>,
        variables: Map<String, Any?>,
        scorecardResults: Map<String, Map<String, Any?>>,
        transformOutputs: Map<String, Map<String, Any?>>,
        outcome: String,
        executionId: String,
        reviewResponse: Map<String, Any?>,
    ): Map<String, Any?> {
        val context = contextView(payload, variables, scorecardResults, transformOutputs, outcome, executionId, reviewResponse)
        val blocking = step.config["onFailure"]?.toString() == "abort"
        val requestBody = resolveTemplate(step.config["bodyTemplate"] ?: emptyMap<String, Any?>(), context)
        return mapOf(
            "id" to "op_${UUID.randomUUID().toString().replace("-", "").take(12)}",
            "stepId" to (step.id ?: step.refId ?: step.ref ?: step.type),
            "actionName" to (step.label ?: step.config["name"]?.toString() ?: step.type),
            "url" to resolveTemplate(step.config["url"]?.toString() ?: "", context),
            "method" to (step.config["method"]?.toString() ?: "POST").uppercase(),
            "headers" to resolveTemplate(step.config["headers"] ?: emptyMap<String, Any?>(), context),
            "requestBody" to requestBody,
            "blocking" to blocking,
            "status" to "queued",
            "createdAt" to System.currentTimeMillis(),
        )
    }

    private fun createReviewTask(
        step: PolicyStep,
        variables: Map<String, Any?>,
        ruleResults: List<Map<String, Any?>>,
        scorecardResults: Map<String, Map<String, Any?>>,
        outcome: String,
        executionId: String,
    ): Map<String, Any?> {
        return mapOf(
            "id" to "local_review_${UUID.randomUUID().toString().replace("-", "").take(12)}",
            "execution_id" to executionId,
            "step_id" to (step.id ?: step.refId ?: step.ref ?: step.type),
            "queue" to (step.config["assignTo"]?.toString() ?: "mobile_review"),
            "status" to "pending",
            "required_fields" to ((step.config["requiredFields"] as? List<*>)?.map { it.toString() } ?: emptyList<String>()),
            "context_snapshot" to mapOf(
                "variables" to variables,
                "rule_results" to ruleResults,
                "scorecard_results" to scorecardResults,
                "outcome_before_review" to outcome,
            ),
        )
    }

    private fun contextView(
        payload: Map<String, Any?>,
        variables: Map<String, Any?>,
        scorecardResults: Map<String, Map<String, Any?>>,
        transformOutputs: Map<String, Map<String, Any?>>,
        outcome: String,
        executionId: String,
        reviewResponse: Map<String, Any?>,
    ): Map<String, Any?> {
        val computed = transformOutputs["computed"] ?: transformOutputs.values.fold(linkedMapOf<String, Any?>()) { acc, item ->
            acc.putAll(item)
            acc
        }
        return mapOf(
            "payload" to payload,
            "variables" to variables,
            "scorecard" to scorecardResults,
            "computed" to computed,
            "transforms" to transformOutputs,
            "outcome" to outcome,
            "execution_id" to executionId,
            "review" to reviewResponse,
        )
    }

    private fun resolveTemplate(value: Any?, context: Map<String, Any?>): Any? = when (value) {
        is String -> {
            val template = Regex("\\{\\{\\s*([^}]+)\\s*\\}\\}")
            val matches = template.findAll(value).toList()
            if (matches.isEmpty()) {
                value
            } else if (matches.size == 1 && matches[0].value == value) {
                resolvePath(context, matches[0].groupValues[1].trim())
            } else {
                var resolvedText: String = value
                for (match in matches) {
                    val raw = resolvePath(context, match.groupValues[1].trim())
                    resolvedText = resolvedText.replace(match.value, raw?.toString() ?: "")
                }
                resolvedText
            }
        }
        is Map<*, *> -> value.entries.associate { it.key.toString() to resolveTemplate(it.value, context) }
        is List<*> -> value.map { resolveTemplate(it, context) }
        else -> value
    }

    private fun evaluateCondition(expression: String, context: Map<String, Any?>): Boolean {
        val trimmed = expression.trim()
        if (" and " in trimmed) {
            return trimmed.split(" and ").all { evaluateCondition(it, context) }
        }
        if (" or " in trimmed) {
            return trimmed.split(" or ").any { evaluateCondition(it, context) }
        }
        val match = Regex("^([A-Za-z0-9_\\.]+)\\s*(==|!=|>=|<=|>|<)\\s*(.+)$").find(trimmed) ?: return false
        val left = resolvePath(context, match.groupValues[1])
        val right = parseLiteral(match.groupValues[3])
        return compare(left, match.groupValues[2], right)
    }

    private fun evaluateExpression(expression: String, context: Map<String, Any?>): Any? {
        val match = Regex("^(.+)\\s*([+\\-*/])\\s*(.+)$").find(expression.trim()) ?: return resolvePath(context, expression.trim().removePrefix("$."))
        val left = numeric(parseOperand(match.groupValues[1], context))
        val right = numeric(parseOperand(match.groupValues[3], context))
        return when (match.groupValues[2]) {
            "+" -> left + right
            "-" -> left - right
            "*" -> left * right
            "/" -> if (right == 0.0) 0.0 else left / right
            else -> left
        }
    }

    private fun parseOperand(value: String, context: Map<String, Any?>): Any? {
        val trimmed = value.trim()
        return if (trimmed.startsWith("$.")) resolvePath(context, trimmed.removePrefix("$."))
        else parseLiteral(trimmed)
    }

    private fun parseLiteral(value: String): Any? {
        val trimmed = value.trim()
        if ((trimmed.startsWith("'") && trimmed.endsWith("'")) || (trimmed.startsWith("\"") && trimmed.endsWith("\""))) {
            return trimmed.substring(1, trimmed.length - 1)
        }
        return trimmed.toDoubleOrNull() ?: when (trimmed.lowercase()) {
            "true" -> true
            "false" -> false
            "null" -> null
            else -> trimmed
        }
    }

    private fun compare(left: Any?, operator: String, right: Any?): Boolean = when (operator) {
        ">" -> numeric(left) > numeric(right)
        ">=" -> numeric(left) >= numeric(right)
        "<" -> numeric(left) < numeric(right)
        "<=" -> numeric(left) <= numeric(right)
        "!=" -> normalize(left) != normalize(right)
        else -> normalize(left) == normalize(right)
    }

    private fun numeric(value: Any?): Double = when (value) {
        is Number -> value.toDouble()
        is String -> value.toDoubleOrNull() ?: 0.0
        is Boolean -> if (value) 1.0 else 0.0
        else -> 0.0
    }

    private fun normalize(value: Any?): Any? = when (value) {
        is Number -> value.toDouble()
        is String -> value.trim()
        else -> value
    }

    private fun resolvePath(root: Any?, rawPath: String): Any? {
        if (root == null) {
            return null
        }
        val path = rawPath.removePrefix("$.")
        if (path.isBlank()) {
            return root
        }
        var current: Any? = root
        for (segment in path.split(".")) {
            if (current == null) {
                return null
            }
            val match = Regex("([A-Za-z0-9_\\-]+)(\\[(\\-?\\d+)])?").matchEntire(segment) ?: return null
            val key = match.groupValues[1]
            current = when (current) {
                is Map<*, *> -> current[key]
                else -> null
            }
            val index = match.groupValues.getOrNull(3).orEmpty()
            if (index.isNotBlank()) {
                val list = current as? List<*> ?: return null
                val requested = index.toInt()
                val resolved = if (requested < 0) list.size + requested else requested
                current = list.getOrNull(resolved)
            }
        }
        return current
    }

    private fun PolicyStep.toTraceStep(): Map<String, Any?> = buildMap {
        id?.let { put("id", it) }
        put("type", type)
        ref?.let { put("ref", it) }
        refId?.let { put("refId", it) }
        label?.let { put("label", it) }
        if (config.isNotEmpty()) {
            put("config", config)
        }
    }
}
