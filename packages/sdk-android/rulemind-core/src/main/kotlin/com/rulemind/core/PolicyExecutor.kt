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
    private val decisionTableEvaluator: DecisionTableEvaluator = DecisionTableEvaluator(ruleEvaluator),
) {
    /**
     * Everything a single `evaluate()` mutates. Held in one object because control-flow steps
     * (`loop`, `branch`, `workflow`) re-enter the step loop with the SAME live context, exactly as
     * the Python executor does with its `ExecutionContext`.
     */
    private class RunState(
        val bundle: Bundle,
        val payload: Map<String, Any?>,
        val executionId: String,
    ) {
        val variables = mutableMapOf<String, Any?>()
        val ruleResults = mutableListOf<Map<String, Any?>>()
        val scorecardResults = mutableMapOf<String, Map<String, Any?>>()
        val transformOutputs = mutableMapOf<String, Map<String, Any?>>()
        val actionResults = mutableListOf<Map<String, Any?>>()
        val pendingOperations = mutableListOf<Map<String, Any?>>()
        val trace = mutableListOf<Map<String, Any?>>()
        val skippedSteps = mutableListOf<String>()
        val reviewResponse = mutableMapOf<String, Any?>()

        /** Loop bindings for the innermost `loop` step, visible to sub-step conditions/templates. */
        var loopScope: Map<String, Any?> = emptyMap()

        /** Sub-workflow cycle guard — seeded with the root policy id, like the Python executor. */
        val visitedWorkflows = mutableSetOf<String>()

        var outcome: String = "pending"
        var score: Double? = null
        var status: String = "running"
        var currentStepIndex: Int = 0
        var pausedAtStep: Int? = null
        var reviewTask: Map<String, Any?>? = null
    }

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

        val state = RunState(bundle, effectivePayload, executionId)
        resumeFrom?.let { previous ->
            state.variables.putAll(previous.variables)
            state.ruleResults += previous.ruleResults
            state.scorecardResults.putAll(previous.scorecardResults)
            state.transformOutputs.putAll(previous.transformOutputs)
            state.actionResults += previous.actionResults
            state.pendingOperations += previous.pendingOperations
            state.trace += previous.trace
            state.skippedSteps += previous.serverOnlyStepsSkipped
            state.reviewResponse.putAll(previous.reviewResponse)
            state.outcome = previous.outcome
            state.score = previous.score
            state.status = previous.status
            state.currentStepIndex = previous.currentStepIndex
            state.pausedAtStep = previous.pausedAtStep
            state.reviewTask = previous.reviewTask
        }
        state.visitedWorkflows += policy.id

        if (resumeFrom != null && state.reviewResponse.isNotEmpty()) {
            state.reviewResponse.forEach { (key, value) ->
                if (key != "decision") {
                    state.variables[key] = value
                }
            }
            when (state.reviewResponse["decision"]?.toString()) {
                "approve" -> state.outcome = "approve"
                "reject" -> state.outcome = "reject"
            }
        }

        if (resumeFrom == null) {
            bundle.variables
                .filter { it.compilable && !bundle.serverOnlyVariables.contains(it.id) }
                .forEach { variable ->
                    state.variables[variable.id] = variableVm.evaluate(variable, effectivePayload, state.variables)
                }
        }

        runSteps(policy.steps, state, state.currentStepIndex, depth = 0)

        if (state.status == "running") {
            state.status = "completed"
            state.currentStepIndex = policy.steps.size
        }
        if (state.outcome == "pending") {
            state.outcome = policy.defaultOutcome ?: "review"
        }

        val latencyMs = System.currentTimeMillis() - startedAt
        return Decision(
            outcome = state.outcome,
            score = state.score,
            variables = state.variables,
            ruleResults = state.ruleResults,
            latencyMs = latencyMs,
            serverOnlyStepsSkipped = state.skippedSteps.distinct(),
            executionId = executionId,
            status = state.status,
            trace = state.trace,
            scorecardResults = state.scorecardResults,
            actionResults = state.actionResults,
            pendingOperations = state.pendingOperations,
            reviewTask = state.reviewTask,
            explainability = mapOf(
                "rules" to state.ruleResults,
                "scorecards" to state.scorecardResults,
                "trace" to state.trace,
            ),
            auditSummary = mapOf(
                "traceSteps" to state.trace.size,
                "pendingOperationCount" to state.pendingOperations.count { it["status"] != "delivered" },
                "actionCount" to state.actionResults.size,
                "reviewTaskId" to state.reviewTask?.get("id"),
            ),
            policyId = policy.id,
            payload = effectivePayload,
            transformOutputs = state.transformOutputs,
            currentStepIndex = state.currentStepIndex,
            pausedAtStep = state.pausedAtStep,
            reviewResponse = state.reviewResponse,
        )
    }

    /**
     * Run a list of steps against the live context. Re-entered by `loop`, `branch` and `workflow`
     * at `depth + 1`; only the top level tracks `currentStepIndex` for durable resume.
     */
    private fun runSteps(steps: List<PolicyStep>, state: RunState, startIndex: Int, depth: Int) {
        for (index in startIndex until steps.size) {
            val step = steps[index]
            if (depth == 0) {
                state.currentStepIndex = index
            }
            val config = step.config
            val condition = config["condition"]?.toString()
            if (!condition.isNullOrBlank() && !evaluateCondition(condition, contextView(state))) {
                state.trace += mapOf("step" to step.toTraceStep(), "skipped" to true, "reason" to "Condition not met: $condition")
                continue
            }

            val stepStarted = System.currentTimeMillis()
            var result: Map<String, Any?>? = null
            var error: String? = null
            try {
                val body = executeStepBody(step, state, depth)
                result = body.first
                error = body.second
            } catch (failure: Exception) {
                // A step that throws must not take the whole decision down, and must not vanish.
                error = failure.message ?: failure.toString()
                // Fail CLOSED on a decision GATE: a rule/scorecard/table that errored must never
                // be treated as passed, so escalate to human review (parity with the Python core).
                if (step.type == "rule" || step.type == "scorecard" || step.type == "decision_table") {
                    state.outcome = mergeOutcome(state.outcome, "review")
                }
                if (config["onFailure"]?.toString() == "abort") {
                    state.status = "failed"
                }
            }

            state.trace += mapOf(
                "step" to step.toTraceStep(),
                "result" to result,
                "error" to error,
                "duration_ms" to (System.currentTimeMillis() - stepStarted),
            )

            if (state.status == "failed" || state.status == "paused" || state.status == "pending_sync") {
                return
            }
        }
    }

    /**
     * Dispatch one step. Returns `(result, error)` — mirroring `_execute_step_body` in the Python
     * core, including its final line: an unrecognised step type is an ERROR, not a silent skip.
     */
    private fun executeStepBody(step: PolicyStep, state: RunState, depth: Int): Pair<Map<String, Any?>?, String?> {
        val refId = step.refId ?: step.ref
        when (step.type) {
            "connector" -> {
                val sourcePayload = (state.payload[refId] as? Map<*, *>)?.keys?.map { it.toString() } ?: emptyList()
                return mapOf("status" to "ok", "payloadKeys" to sourcePayload.sorted()) to null
            }

            "rule" -> {
                val rule = state.bundle.rules.firstOrNull { it.id == refId }
                if (rule == null) {
                    state.skippedSteps += step.id ?: refId ?: step.type
                    return null to "Unknown rule: $refId"
                }
                val result = ruleEvaluator.evaluate(rule, state.variables)
                state.ruleResults += result
                state.outcome = mergeOutcome(state.outcome, result["outcome"]?.toString())
                return result to null
            }

            "decision_table" -> {
                val table = state.bundle.decisionTables.firstOrNull { it["id"] == refId }
                if (table == null) {
                    state.skippedSteps += step.id ?: refId ?: step.type
                    return null to "Unknown decision table: $refId"
                }
                val result = decisionTableEvaluator.evaluate(table, state.variables)
                state.outcome = mergeOutcome(state.outcome, result["outcome"]?.toString())
                return result to null
            }

            "scorecard" -> {
                val scorecard = state.bundle.scorecards.firstOrNull { it.id == refId }
                if (scorecard == null) {
                    state.skippedSteps += step.id ?: refId ?: step.type
                    return null to "Unknown scorecard: $refId"
                }
                val result = scorecardEvaluator.evaluate(scorecard, state.variables, variableLookup(state))
                if (refId != null) {
                    state.scorecardResults[refId] = result
                }
                state.score = (result["score"] as? Number)?.toDouble()
                return result to null
            }

            "transform" -> {
                val result = executeTransform(step, contextView(state))
                val outputKey = step.config["outputKey"]?.toString() ?: "transformed"
                state.transformOutputs[outputKey] = result
                return result to null
            }

            "action" -> {
                val pending = queueAction(step, contextView(state))
                state.pendingOperations += pending
                val result = mapOf(
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
                state.actionResults += result
                if (pending["blocking"] == true) {
                    state.status = "pending_sync"
                    if (depth == 0) {
                        state.currentStepIndex += 1
                    }
                }
                return result to null
            }

            "review_gate" -> {
                val task = createReviewTask(step, state)
                state.reviewTask = task
                state.status = "paused"
                state.pausedAtStep = state.currentStepIndex
                return mapOf("paused" to true, "reviewTaskId" to task["id"]) to null
            }

            "outcome" -> {
                state.outcome = mergeOutcome(
                    state.outcome,
                    step.refId ?: step.ref ?: step.config["outcome"]?.toString() ?: step.label ?: state.outcome,
                )
                return mapOf("outcome" to state.outcome) to null
            }

            "loop" -> return executeLoop(step, state, depth) to null

            "branch" -> return executeBranch(step, state, depth) to null

            "workflow" -> return executeSubWorkflow(step, state, depth) to null

            // `model` needs the model blob and an ML runtime that only the server has; `monitor`
            // is post-decision server telemetry (audit events + alert webhooks + scheduled
            // re-evaluation). Neither can be honoured offline. They FAIL LOUDLY here rather than
            // being skipped, because a policy that silently drops its model step still returns a
            // decision — one that was never scored by the model it was authored around.
            "model" -> throw UnsupportedStepException(
                "Model steps require the server runtime and cannot run offline: ${refId ?: step.id ?: "model"}",
            )

            "monitor" -> throw UnsupportedStepException(
                "Monitor steps are server-side telemetry and cannot run offline: ${step.id ?: step.label ?: "monitor"}",
            )

            // Parity with `_execute_step_body`'s final line. Previously this branch marked the step
            // "skipped" and moved on, so a policy carrying a step type this SDK version does not
            // know produced a full decision with that step silently missing.
            else -> return null to "Unknown step type: ${step.type}"
        }
    }

    class UnsupportedStepException(message: String) : RuntimeException(message)

    // ── Control flow ──

    private fun executeLoop(step: PolicyStep, state: RunState, depth: Int): Map<String, Any?> {
        if (depth >= MAX_WORKFLOW_DEPTH) {
            throw IllegalStateException("Max loop/workflow nesting depth ($MAX_WORKFLOW_DEPTH) exceeded")
        }
        val config = step.config
        val asName = config["as"]?.toString() ?: "item"
        val indexName = config["indexAs"]?.toString() ?: "index"
        val subSteps = toSteps(config["steps"])
        val maxIterations = minOf(intConfig(config["maxIterations"], 1000), LOOP_HARD_CAP)

        val collection = resolveCollection(config["over"] ?: config["items"], state)
        val items: List<Any?> = when (collection) {
            // A map iterates as {key, value} pairs; anything non-iterable is a no-op.
            is Map<*, *> -> collection.entries.map { mapOf("key" to it.key?.toString(), "value" to it.value) }
            is List<*> -> collection.toList()
            else -> emptyList()
        }

        val outerScope = state.loopScope
        val truncated = items.size > maxIterations
        val sliced = items.take(maxIterations)
        val mode = config["mode"]?.toString()?.lowercase() ?: "sequential"
        val parallel = mode == "parallel"
        val entryOutcome = state.outcome

        val iterations = mutableListOf<Map<String, Any?>>()
        var cumulative = entryOutcome
        var anyFailed = false
        for ((index, item) in sliced.withIndex()) {
            state.loopScope = outerScope + mapOf(asName to item, indexName to index.toLong())
            val outcomeBefore = if (parallel) cumulative else state.outcome
            // The server runs a parallel loop on isolated per-iteration contexts that each START
            // from the entry outcome and are merged back in index order. Reproducing that merge is
            // what keeps the OUTCOME identical; there is no concurrency to win on-device.
            if (parallel) {
                state.outcome = entryOutcome
            }
            val traceStart = state.trace.size
            runSteps(subSteps, state, 0, depth + 1)
            val iterationOutcome = state.outcome
            if (parallel) {
                cumulative = mergeOutcome(cumulative, iterationOutcome)
                state.outcome = cumulative
            }
            val failed = state.status == "failed"
            anyFailed = anyFailed || failed
            iterations += mapOf(
                "index" to index,
                "item" to item,
                "outcome_before" to outcomeBefore,
                "outcome_after" to (if (parallel) cumulative else state.outcome),
                "steps" to state.trace.subList(traceStart, state.trace.size).toList(),
                "paused" to (state.status == "paused"),
                "failed" to failed,
            )
            if (parallel && state.status == "paused") {
                // Same refusal as the server: a durable pause cannot be represented across
                // concurrent iterations, so it is rejected rather than losing the resume point.
                state.loopScope = outerScope
                throw IllegalStateException(
                    "Review gates / durable pauses are not supported inside a parallel loop; use sequential mode.",
                )
            }
            if (!parallel && (state.status == "paused" || state.status == "failed")) {
                break
            }
            if (parallel) {
                // A parallel loop runs every iteration; a failure is reported after the fact.
                state.status = if (state.status == "failed") "running" else state.status
            }
        }
        state.loopScope = outerScope
        if (parallel && anyFailed) {
            state.status = "failed"
        }

        return mapOf(
            "loop" to (step.id ?: step.label ?: "loop"),
            "over" to config["over"],
            "count" to items.size,
            "iterations_run" to iterations.size,
            "truncated" to truncated,
            "mode" to if (parallel) "parallel" else "sequential",
            "iterations" to iterations,
        )
    }

    private fun executeBranch(step: PolicyStep, state: RunState, depth: Int): Map<String, Any?> {
        val branches = (step.config["branches"] as? List<*>) ?: emptyList<Any?>()
        for ((index, raw) in branches.withIndex()) {
            val branch = raw as? Map<*, *> ?: continue
            val condition = branch["condition"]?.toString()
            if (condition.isNullOrBlank() || evaluateCondition(condition, contextView(state))) {
                runSteps(toSteps(branch["steps"]), state, 0, depth + 1)
                return mapOf("branch" to index, "label" to (branch["label"] ?: index), "matched" to true)
            }
        }
        val defaultSteps = toSteps(step.config["default"])
        if (defaultSteps.isNotEmpty()) {
            runSteps(defaultSteps, state, 0, depth + 1)
            return mapOf("branch" to "default", "matched" to true)
        }
        return mapOf("branch" to null, "matched" to false)
    }

    private fun executeSubWorkflow(step: PolicyStep, state: RunState, depth: Int): Map<String, Any?> {
        if (depth >= MAX_WORKFLOW_DEPTH) {
            throw IllegalStateException("Max sub-workflow depth ($MAX_WORKFLOW_DEPTH) exceeded")
        }
        val refId = (step.refId ?: step.ref).orEmpty()
        if (refId.isBlank()) {
            throw IllegalStateException("Sub-workflow step missing ref_id")
        }
        if (refId in state.visitedWorkflows) {
            throw IllegalStateException("Sub-workflow cycle detected: $refId")
        }
        val subPolicy = state.bundle.policies.firstOrNull { it.id == refId }
            ?: throw IllegalStateException("Unknown sub-workflow: $refId")
        state.visitedWorkflows += refId
        runSteps(subPolicy.steps, state, 0, depth + 1)
        return mapOf("workflow" to refId, "outcome" to state.outcome)
    }

    /** Loop iterables: a literal list, a `$.`-path, or a dotted path into the context view. */
    private fun resolveCollection(over: Any?, state: RunState): Any? {
        if (over is List<*>) return over
        val path = over as? String ?: return null
        if (path.isBlank()) return null
        return resolvePath(contextView(state), path.removePrefix("$."))
    }

    private fun toSteps(raw: Any?): List<PolicyStep> {
        val list = raw as? List<*> ?: return emptyList()
        return list.mapNotNull { item ->
            val map = item as? Map<*, *> ?: return@mapNotNull null
            @Suppress("UNCHECKED_CAST")
            PolicyStep(
                id = map["id"]?.toString(),
                type = map["type"]?.toString() ?: "",
                ref = map["ref"]?.toString(),
                refId = (map["refId"] ?: map["ref_id"])?.toString(),
                label = (map["label"] ?: map["name"])?.toString(),
                config = (map["config"] as? Map<*, *>)?.entries?.associate { it.key.toString() to it.value }
                    ?: emptyMap(),
            )
        }
    }

    private fun intConfig(value: Any?, fallback: Int): Int = when (value) {
        is Number -> value.toInt()
        is String -> value.trim().toIntOrNull() ?: fallback
        else -> fallback
    }

    private fun variableLookup(state: RunState): Map<String, Map<String, Any?>> =
        state.bundle.variables.associate { it.id to mapOf("name" to it.name, "source_id" to it.sourceId) }

    // ── Step helpers ──

    private fun executeTransform(step: PolicyStep, context: Map<String, Any?>): Map<String, Any?> {
        val mapping = step.config["mapping"] as? Map<*, *> ?: emptyMap<String, Any?>()
        val result = linkedMapOf<String, Any?>()
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

    private fun queueAction(step: PolicyStep, context: Map<String, Any?>): Map<String, Any?> {
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

    private fun createReviewTask(step: PolicyStep, state: RunState): Map<String, Any?> {
        return mapOf(
            "id" to "local_review_${UUID.randomUUID().toString().replace("-", "").take(12)}",
            "execution_id" to state.executionId,
            "step_id" to (step.id ?: step.refId ?: step.ref ?: step.type),
            "queue" to (step.config["assignTo"]?.toString() ?: "mobile_review"),
            "status" to "pending",
            "required_fields" to ((step.config["requiredFields"] as? List<*>)?.map { it.toString() } ?: emptyList<String>()),
            "context_snapshot" to mapOf(
                "variables" to state.variables,
                "rule_results" to state.ruleResults,
                "scorecard_results" to state.scorecardResults,
                "outcome_before_review" to state.outcome,
            ),
        )
    }

    private fun contextView(state: RunState): Map<String, Any?> {
        val computed = state.transformOutputs["computed"] ?: state.transformOutputs.values.fold(linkedMapOf<String, Any?>()) { acc, item ->
            acc.putAll(item)
            acc
        }
        return buildMap {
            put("payload", state.payload)
            put("variables", state.variables)
            put("scorecard", state.scorecardResults)
            // Loop bindings live at the top level (referenceable by their `as`/`indexAs` names)
            // plus under a stable `loop` key, exactly as the Python context view exposes them.
            put("loop", state.loopScope)
            putAll(state.loopScope)
            put("computed", computed)
            put("transforms", state.transformOutputs)
            put("outcome", state.outcome)
            put("execution_id", state.executionId)
            put("review", state.reviewResponse)
        }
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

    private companion object {
        /** Matches `_MAX_WORKFLOW_DEPTH` in the Python executor. */
        const val MAX_WORKFLOW_DEPTH = 10
        const val LOOP_HARD_CAP = 10_000
    }
}
