package com.rulemind.core

/**
 * On-device decision-table evaluation. A direct port of the Python core
 * (app/decision_tables.py evaluate_decision_table) so the offline SDK matches the
 * server exactly — validated by the shared fixture packages/shared/decision-tables.spec.json
 * (see DecisionTableConformanceTest). Cell matching reuses [RuleEvaluator.compareCondition]
 * (the same 12-operator contract as rules).
 *
 * Tables are raw maps (rows/cells are keyed by arbitrary input ids), mirroring the
 * dynamic shape the compiler emits into the bundle.
 */
class DecisionTableEvaluator(
    private val ruleEvaluator: RuleEvaluator = RuleEvaluator(),
) {
    private val wildcardOperators = setOf("", "any", "*", "-")
    private val hitPolicies = setOf("first", "priority", "unique", "collect")

    @Suppress("UNCHECKED_CAST")
    fun evaluate(table: Map<String, Any?>, variables: Map<String, Any?>): Map<String, Any?> {
        val inputs = (table["inputs"] as? List<*>)?.mapNotNull { it as? Map<String, Any?> } ?: emptyList()
        val rows = (table["rows"] as? List<*>)?.mapNotNull { it as? Map<String, Any?> } ?: emptyList()
        var hitPolicy = (table["hit_policy"] as? String)?.lowercase() ?: "first"
        if (hitPolicy !in hitPolicies) hitPolicy = "first"
        val outcomeOutputId = outcomeOutputId(table)

        val matched = rows.filter { rowMatches(it, inputs, variables) }

        var outputs: Map<String, Any?> = emptyMap()
        var winningRowId: Any? = null
        if (matched.isNotEmpty()) {
            when (hitPolicy) {
                "collect" -> {
                    val collected = linkedMapOf<String, Any?>()
                    (table["outputs"] as? List<*>)?.forEach { out ->
                        val oid = (out as? Map<*, *>)?.get("id")?.toString() ?: return@forEach
                        collected[oid] = matched.map { (it["outputs"] as? Map<*, *>)?.get(oid) }
                    }
                    outputs = collected
                    winningRowId = matched.map { it["id"] }
                }
                "priority" -> {
                    val winner = matched.maxByOrNull { priority(it) }!!
                    outputs = rowOutputs(winner)
                    winningRowId = winner["id"]
                }
                else -> { // first / unique both take the first match at eval time
                    val winner = matched.first()
                    outputs = rowOutputs(winner)
                    winningRowId = winner["id"]
                }
            }
        } else {
            val default = table["default_row"] as? Map<*, *>
            outputs = ((default?.get("outputs")) as? Map<*, *>)?.entries?.associate { it.key.toString() to it.value } ?: emptyMap()
            winningRowId = if (outputs.isNotEmpty()) "default" else null
        }

        var outcome: Any? = null
        if (outcomeOutputId != null) {
            val raw = outputs[outcomeOutputId]
            outcome = if (raw is List<*> && raw.isNotEmpty()) raw[0] else raw
            if (outcome is String) outcome = outcome.lowercase()
        }

        return mapOf(
            "outputs" to outputs,
            "outcome" to outcome,
            "matchedRowIds" to matched.map { it["id"] },
            "winningRowId" to winningRowId,
            "hitPolicy" to hitPolicy,
            "ambiguous" to (hitPolicy == "unique" && matched.size > 1),
        )
    }

    private fun rowOutputs(row: Map<String, Any?>): Map<String, Any?> =
        (row["outputs"] as? Map<*, *>)?.entries?.associate { it.key.toString() to it.value } ?: emptyMap()

    private fun rowMatches(row: Map<String, Any?>, inputs: List<Map<String, Any?>>, variables: Map<String, Any?>): Boolean {
        val cells = row["cells"] as? Map<*, *> ?: emptyMap<Any, Any?>()
        for (input in inputs) {
            val inputId = input["id"]?.toString() ?: continue
            val cell = cells[inputId] as? Map<*, *>
            if (isWildcard(cell)) continue
            val variableId = (input["variable_id"] ?: input["variable"] ?: inputId).toString()
            val actual = variables[variableId]
            val operator = cell?.get("operator")?.toString() ?: "=="
            val expected = cell?.get("value")
            val value2 = cell?.get("value2")?.takeIf { it != "" }
            val fieldType = (input["field_type"] ?: input["fieldType"])?.toString()
            if (!ruleEvaluator.compareCondition(actual, operator, expected, value2, fieldType)) {
                return false
            }
        }
        return true
    }

    private fun isWildcard(cell: Map<*, *>?): Boolean {
        if (cell == null) return true
        val operator = cell["operator"]?.toString()
        if (operator == null || operator in wildcardOperators) return true
        if (operator !in setOf("exists", "!exists")) {
            val value = cell["value"]
            if (value == null || value == "") return true
        }
        return false
    }

    private fun priority(row: Map<String, Any?>): Double = when (val p = row["priority"]) {
        is Number -> p.toDouble()
        is String -> p.toDoubleOrNull() ?: 0.0
        else -> 0.0
    }

    private fun outcomeOutputId(table: Map<String, Any?>): String? {
        (table["outputs"] as? List<*>)?.forEach { out ->
            val map = out as? Map<*, *> ?: return@forEach
            if ((map["type"] as? String)?.lowercase() == "outcome") return map["id"]?.toString()
        }
        return null
    }
}
