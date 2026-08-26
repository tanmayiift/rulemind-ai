package com.rulemind.core

import com.rulemind.core.models.CompiledVariable
import com.rulemind.core.models.Instruction
import java.util.Locale

class VariableVM {
    fun evaluate(variable: CompiledVariable, payload: Map<String, Any?>, variables: Map<String, Any?>): Any? {
        val sourcePayload = (payload[variable.sourceId] as? Map<String, Any?>) ?: payload
        // `payload` must be the variable's SOURCE-SCOPED payload: that is what the
        // compiler means by {"op":"get","source":"payload"}, mirroring the Python
        // authoring contract where @variable(source = "claim") receives only the
        // claim dict. Binding the FULL payload made every connector-sourced variable
        // fall back to its default (0) whenever the caller sent the documented
        // source-keyed shape, so on-device decisions diverged from the server.
        val registers = mutableMapOf<String, Any?>(
            "payload" to sourcePayload,
            "rootPayload" to payload,
            "variables" to variables,
            "context" to variables,
        )
        registers["source"] = sourcePayload

        for (instruction in variable.instructions) {
            val target = instruction.target
            when (instruction.op.lowercase(Locale.ROOT)) {
                "literal" -> if (target != null) {
                    registers[target] = instruction.value
                }

                "get" -> if (target != null) {
                    val source = resolveRegister(instruction.source, registers) ?: sourcePayload
                    // The compiler passes the lookup key and the default THROUGH REGISTERS:
                    //   {"op":"literal","target":"r1","value":"claim_amount_inr"}
                    //   {"op":"get","source":"payload","key":"r1","default":"r2",...}
                    // Reading "r1" as a literal field name made every compiled variable
                    // miss its field and fall back to the default, which read literally
                    // was the string "r2" and cast to 0.
                    val path = deref(instruction.path ?: instruction.key, registers) as? String
                    val fallback = deref(instruction.defaultValue, registers)
                    // Two path conventions are in use: the compiler emits a bare field
                    // name relative to the variable's source payload, while hand-authored
                    // bundles use a rooted "sourceId.field". Try the scoped payload, then
                    // the root, so both resolve without the caller having to pick one.
                    registers[target] = resolvePath(source, path)
                        ?: resolvePath(registers["rootPayload"], path)
                        ?: fallback
                }

                "filter" -> if (target != null) {
                    val source = asList(resolveRegister(instruction.source, registers))
                    registers[target] = source.filter { item ->
                        evaluatePredicate(
                            item,
                            instruction.predicatePath ?: instruction.path,
                            instruction.predicateOperator,
                            instruction.predicateValue ?: instruction.value,
                        )
                    }
                }

                "find" -> if (target != null) {
                    val source = asList(resolveRegister(instruction.source, registers))
                    registers[target] = source.firstOrNull { item ->
                        evaluatePredicate(
                            item,
                            instruction.predicatePath ?: instruction.path,
                            instruction.predicateOperator,
                            instruction.predicateValue ?: instruction.value,
                        )
                    }
                }

                "sum" -> if (target != null) {
                    registers[target] = extractNumericValues(instruction, registers).sum()
                }

                "count" -> if (target != null) {
                    val value = resolveRegister(instruction.source, registers)
                    registers[target] = when (value) {
                        is Collection<*> -> value.size
                        is Map<*, *> -> value.size
                        null -> 0
                        else -> 1
                    }
                }

                "count_where" -> if (target != null) {
                    val source = asList(resolveRegister(instruction.source, registers))
                    registers[target] = source.count { item ->
                        evaluatePredicate(
                            item,
                            instruction.predicatePath ?: instruction.path,
                            instruction.predicateOperator,
                            instruction.predicateValue ?: instruction.value,
                        )
                    }
                }

                "max" -> if (target != null) {
                    registers[target] = extractNumericValues(instruction, registers).maxOrNull() ?: 0.0
                }

                "min" -> if (target != null) {
                    registers[target] = extractNumericValues(instruction, registers).minOrNull() ?: 0.0
                }

                "divide", "multiply", "add", "subtract" -> if (target != null) {
                    val left = numeric(resolveOperand(instruction.left, registers, instruction.values.getOrNull(0)))
                    val right = numeric(
                        resolveOperand(
                            instruction.right,
                            registers,
                            instruction.values.getOrNull(1) ?: instruction.value ?: instruction.defaultValue,
                        )
                    )
                    registers[target] = when (instruction.op.lowercase(Locale.ROOT)) {
                        "divide" -> if (right == 0.0) 0.0 else left / right
                        "multiply" -> left * right
                        "subtract" -> left - right
                        else -> left + right
                    }
                }

                "cast" -> if (target != null) {
                    registers[target] = castValue(resolveRegister(instruction.source, registers), instruction.type)
                }

                "compare" -> if (target != null) {
                    registers[target] = compare(
                        resolveOperand(instruction.left, registers, null),
                        instruction.predicateOperator ?: instruction.type ?: "==",
                        resolveOperand(instruction.right, registers, instruction.value),
                    )
                }

                "if" -> if (target != null) {
                    val conditionValue = resolveRegister(instruction.source, registers)
                    val branch = when (conditionValue) {
                        is Boolean -> conditionValue
                        is Number -> conditionValue.toDouble() != 0.0
                        null -> false
                        else -> true
                    }
                    registers[target] = if (branch) {
                        resolveOperand(instruction.thenSource, registers, instruction.thenValue)
                    } else {
                        resolveOperand(instruction.elseSource, registers, instruction.elseValue)
                    }
                }

                "return" -> return resolveOperand(instruction.source ?: target, registers, instruction.value)
            }
        }
        return null
    }

    /** Compiler-allocated registers are named `r<n>`; such a token is a reference to a
     * previously computed value, not a literal. Anything else passes through. */
    private fun deref(token: Any?, registers: Map<String, Any?>): Any? {
        if (token is String && REGISTER_NAME.matches(token) && registers.containsKey(token)) {
            return registers[token]
        }
        return token
    }

    private fun resolveRegister(name: String?, registers: Map<String, Any?>): Any? {
        if (name.isNullOrBlank()) {
            return null
        }
        if (registers.containsKey(name)) {
            return registers[name]
        }
        return resolvePath(registers["source"], name)
            ?: resolvePath(registers["payload"], name)
            ?: resolvePath(registers["variables"], name)
    }

    private fun resolveOperand(name: String?, registers: Map<String, Any?>, fallback: Any?): Any? {
        return resolveRegister(name, registers) ?: fallback
    }

    private fun extractNumericValues(instruction: Instruction, registers: Map<String, Any?>): List<Double> {
        val directSources = instruction.sources.ifEmpty {
            buildList {
                if (!instruction.source.isNullOrBlank()) add(instruction.source)
                addAll(instruction.args)
            }
        }
        if (directSources.isNotEmpty()) {
            return directSources.map { numeric(resolveRegister(it, registers)) }
        }

        val base = resolveRegister(instruction.source, registers)
        val list = asList(base)
        if (list.isEmpty()) {
            return emptyList()
        }
        return list.map { item ->
            numeric(
                if (instruction.path.isNullOrBlank()) {
                    item
                } else {
                    resolvePath(item, instruction.path)
                }
            )
        }
    }

    private fun asList(value: Any?): List<Any?> = when (value) {
        is List<*> -> value
        is Array<*> -> value.toList()
        null -> emptyList()
        else -> listOf(value)
    }

    private fun resolvePath(root: Any?, rawPath: String?): Any? {
        if (root == null || rawPath.isNullOrBlank()) {
            return root
        }
        val path = rawPath.removePrefix("$.")
        if (path.isBlank()) {
            return root
        }
        var current: Any? = root
        val segments = path.split(".")
        for (segment in segments) {
            if (current == null) {
                return null
            }
            val match = Regex("([A-Za-z0-9_\\-]+)(\\[(\\-?\\d+)])?").matchEntire(segment) ?: return null
            val key = match.groupValues[1]
            current = when (current) {
                is Map<*, *> -> current[key]
                else -> null
            }
            val indexGroup = match.groupValues.getOrNull(3).orEmpty()
            if (indexGroup.isNotBlank()) {
                val list = current as? List<*> ?: return null
                val requested = indexGroup.toInt()
                val index = if (requested < 0) list.size + requested else requested
                current = list.getOrNull(index)
            }
        }
        return current
    }

    private fun evaluatePredicate(item: Any?, path: String?, operator: String?, expected: Any?): Boolean {
        val actual = if (path.isNullOrBlank()) item else resolvePath(item, path)
        return compare(actual, operator ?: "==", expected)
    }

    private fun compare(actual: Any?, operator: String, expected: Any?): Boolean {
        val actualNumber = actual as? Number
        val expectedNumber = expected as? Number
        return when (operator) {
            ">" -> numeric(actualNumber) > numeric(expectedNumber)
            ">=" -> numeric(actualNumber) >= numeric(expectedNumber)
            "<" -> numeric(actualNumber) < numeric(expectedNumber)
            "<=" -> numeric(actualNumber) <= numeric(expectedNumber)
            "!=" -> normalize(actual) != normalize(expected)
            "in" -> asList(expected).map(::normalize).contains(normalize(actual))
            "not_in" -> !asList(expected).map(::normalize).contains(normalize(actual))
            else -> normalize(actual) == normalize(expected)
        }
    }

    private fun castValue(value: Any?, type: String?): Any? = when ((type ?: "string").lowercase(Locale.ROOT)) {
        "int", "integer" -> numeric(value).toInt()
        "float", "double", "number" -> numeric(value)
        "bool", "boolean" -> when (value) {
            is Boolean -> value
            is Number -> value.toDouble() != 0.0
            is String -> value.equals("true", ignoreCase = true) || value == "1"
            else -> value != null
        }
        "list" -> asList(value)
        else -> value?.toString()
    }

    private fun normalize(value: Any?): Any? = when (value) {
        is Number -> value.toDouble()
        is String -> value.trim()
        else -> value
    }

    private fun numeric(value: Any?): Double = when (value) {
        is Number -> value.toDouble()
        is String -> value.toDoubleOrNull() ?: 0.0
        is Boolean -> if (value) 1.0 else 0.0
        else -> 0.0
    }

    companion object {
        private val REGISTER_NAME = Regex("^r\\d+$")
    }
}
