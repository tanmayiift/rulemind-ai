package com.rulemind.core

import com.rulemind.core.models.CompiledVariable
import com.rulemind.core.models.Instruction
import kotlin.test.Test
import kotlin.test.assertEquals

/**
 * The compiler passes a variable's lookup key and default THROUGH REGISTERS, and
 * scopes the payload by the variable's source id. Both were mishandled, so every
 * compiled variable evaluated to its default on-device while the server returned
 * the real value — the parity contract silently broken.
 */
class VariableVMRegisterTest {
    private val vm = VariableVM()

    /** Exactly what the server emits for
     *  `@variable(source="claim") def claim_amount_inr(payload, variables, apis)`. */
    private fun claimAmount() = CompiledVariable(
        id = "claim_amount_inr",
        sourceId = "claim",
        name = "Claim Amount (INR)",
        instructions = listOf(
            Instruction(op = "literal", target = "r1", value = "claim_amount_inr"),
            Instruction(op = "literal", target = "r2", value = 0),
            Instruction(op = "get", source = "payload", key = "r1", defaultValue = "r2", target = "r3"),
            Instruction(op = "cast", source = "r3", target = "r4", type = "int"),
            Instruction(op = "return", source = "r4"),
        ),
    )

    @Test
    fun `source-keyed payload reaches the variable`() {
        val value = vm.evaluate(claimAmount(), mapOf("claim" to mapOf("claim_amount_inr" to 28000)), emptyMap())
        assertEquals(28000, (value as Number).toInt())
    }

    @Test
    fun `flat payload still resolves`() {
        val value = vm.evaluate(claimAmount(), mapOf("claim_amount_inr" to 28000), emptyMap())
        assertEquals(28000, (value as Number).toInt())
    }

    @Test
    fun `missing field falls back to the compiled default`() {
        val value = vm.evaluate(claimAmount(), mapOf("claim" to emptyMap<String, Any?>()), emptyMap())
        assertEquals(0, (value as Number).toInt())
    }

    @Test
    fun `an unrelated source does not leak in`() {
        val value = vm.evaluate(claimAmount(), mapOf("checkout" to mapOf("claim_amount_inr" to 999)), emptyMap())
        assertEquals(0, (value as Number).toInt())
    }
}
