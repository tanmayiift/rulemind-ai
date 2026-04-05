package com.rulemind.android

import com.rulemind.core.models.Decision
import org.json.JSONArray
import org.json.JSONObject

internal object DecisionCodec {
    fun fromJson(json: JSONObject): Decision {
        val experiment = json.optJSONObject("experiment")
        return Decision(
            outcome = json.optString("outcome"),
            score = json.optDouble("score").takeUnless { it.isNaN() },
            variables = json.optJSONObject("variables")?.toMap() ?: emptyMap(),
            ruleResults = json.optJSONArray("ruleResults")?.toMapList() ?: emptyList(),
            experimentId = json.optString("experimentId").ifBlank { experiment?.optString("id")?.ifBlank { null } },
            experimentVariant = json.optString("experimentVariant").ifBlank { experiment?.optString("variant")?.ifBlank { null } },
            latencyMs = json.optLong("latencyMs"),
            requestId = json.optString("requestId").ifBlank { null },
            serverOnlyStepsSkipped = json.optJSONArray("serverOnlyStepsSkipped")?.toStringList() ?: emptyList(),
            executionId = json.optString("executionId").ifBlank { null },
            status = json.optString("status").ifBlank { "completed" },
            trace = json.optJSONArray("trace")?.toMapList() ?: emptyList(),
            scorecardResults = json.optJSONObject("scorecardResults")?.toNestedMap() ?: emptyMap(),
            actionResults = json.optJSONArray("actionResults")?.toMapList() ?: emptyList(),
            pendingOperations = json.optJSONArray("pendingOperations")?.toMapList() ?: emptyList(),
            reviewTask = json.optJSONObject("reviewTask")?.toMap(),
            explainability = json.optJSONObject("explainability")?.toMap() ?: emptyMap(),
            auditSummary = json.optJSONObject("auditSummary")?.toMap() ?: emptyMap(),
            source = json.optString("source").ifBlank { null },
            policyId = json.optString("policyId").ifBlank { null },
            userId = json.optString("userId").ifBlank { null },
            payload = json.optJSONObject("payload")?.toMap() ?: emptyMap(),
            transformOutputs = json.optJSONObject("transformOutputs")?.toNestedMap() ?: emptyMap(),
            currentStepIndex = json.optInt("currentStepIndex", 0),
            pausedAtStep = json.opt("pausedAtStep").takeUnless { it == JSONObject.NULL }?.toString()?.toIntOrNull(),
            reviewResponse = json.optJSONObject("reviewResponse")?.toMap() ?: emptyMap(),
        )
    }

    fun toJson(decision: Decision): JSONObject {
        return JSONObject()
            .put("outcome", decision.outcome)
            .put("score", decision.score)
            .put("variables", toJsonValue(decision.variables))
            .put("ruleResults", toJsonValue(decision.ruleResults))
            .put("experimentId", decision.experimentId)
            .put("experimentVariant", decision.experimentVariant)
            .put("latencyMs", decision.latencyMs)
            .put("requestId", decision.requestId)
            .put("serverOnlyStepsSkipped", toJsonValue(decision.serverOnlyStepsSkipped))
            .put("executionId", decision.executionId)
            .put("status", decision.status)
            .put("trace", toJsonValue(decision.trace))
            .put("scorecardResults", toJsonValue(decision.scorecardResults))
            .put("actionResults", toJsonValue(decision.actionResults))
            .put("pendingOperations", toJsonValue(decision.pendingOperations))
            .put("reviewTask", toJsonValue(decision.reviewTask))
            .put("explainability", toJsonValue(decision.explainability))
            .put("auditSummary", toJsonValue(decision.auditSummary))
            .put("source", decision.source)
            .put("policyId", decision.policyId)
            .put("userId", decision.userId)
            .put("payload", toJsonValue(decision.payload))
            .put("transformOutputs", toJsonValue(decision.transformOutputs))
            .put("currentStepIndex", decision.currentStepIndex)
            .put("pausedAtStep", decision.pausedAtStep)
            .put("reviewResponse", toJsonValue(decision.reviewResponse))
    }

    fun toJsonValue(value: Any?): Any? = when (value) {
        null -> JSONObject.NULL
        is JSONObject, is JSONArray, is Number, is Boolean, is String -> value
        is Map<*, *> -> JSONObject().apply {
            value.forEach { (key, item) ->
                if (key != null) {
                    put(key.toString(), toJsonValue(item))
                }
            }
        }
        is Iterable<*> -> JSONArray().apply {
            value.forEach { put(toJsonValue(it)) }
        }
        is Array<*> -> JSONArray().apply {
            value.forEach { put(toJsonValue(it)) }
        }
        else -> value.toString()
    }

    private fun JSONObject.toMap(): Map<String, Any?> {
        val result = linkedMapOf<String, Any?>()
        val iterator = keys()
        while (iterator.hasNext()) {
            val key = iterator.next()
            result[key] = opt(key).toAny()
        }
        return result
    }

    private fun JSONObject.toNestedMap(): Map<String, Map<String, Any?>> {
        val result = linkedMapOf<String, Map<String, Any?>>()
        val iterator = keys()
        while (iterator.hasNext()) {
            val key = iterator.next()
            result[key] = optJSONObject(key)?.toMap() ?: emptyMap()
        }
        return result
    }

    private fun JSONArray.toMapList(): List<Map<String, Any?>> = buildList {
        for (index in 0 until length()) {
            val item = opt(index)
            if (item is JSONObject) {
                add(item.toMap())
            }
        }
    }

    private fun JSONArray.toStringList(): List<String> = buildList {
        for (index in 0 until length()) {
            add(optString(index))
        }
    }

    private fun Any?.toAny(): Any? = when (this) {
        JSONObject.NULL, null -> null
        is JSONObject -> toMap()
        is JSONArray -> buildList {
            for (index in 0 until length()) {
                add(opt(index).toAny())
            }
        }
        else -> this
    }
}
