package com.rulemind.core

import com.rulemind.core.models.Bundle
import com.rulemind.core.models.CompiledRule
import com.rulemind.core.models.Policy
import com.rulemind.core.models.PolicyStep
import kotlin.test.Test
import kotlin.test.assertEquals

class RuleMindEngineTest {
    @Test
    fun skipsServerOnlyWorkflowStepsInEdgeMode() {
        val bundle = Bundle(
            bundleVersion = 1,
            bundleId = "bundle-1",
            tenantId = "tenant-1",
            compiledAt = "2026-03-26T00:00:00Z",
            expiresAt = "2026-04-26T00:00:00Z",
            variables = emptyList(),
            rules = listOf(CompiledRule(id = "r1", name = "Rule", nodes = emptyList())),
            scorecards = emptyList(),
            policies = listOf(
                Policy(
                    id = "policy-1",
                    name = "Policy",
                    steps = listOf(
                        PolicyStep(type = "action", id = "a1"),
                        PolicyStep(type = "review_gate", id = "rg1")
                    ),
                    defaultOutcome = "review"
                )
            ),
            experiments = emptyList(),
            serverOnlyVariables = emptyList(),
            checksum = "sha256:test"
        )

        val decision = RuleMindEngine().evaluate(bundle, "policy-1", emptyMap())
        assertEquals(listOf("a1", "rg1"), decision.serverOnlyStepsSkipped)
    }
}
