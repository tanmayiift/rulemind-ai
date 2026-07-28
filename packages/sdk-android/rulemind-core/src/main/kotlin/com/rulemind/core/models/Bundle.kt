package com.rulemind.core.models

data class Instruction(
    val op: String,
    val target: String? = null,
    val source: String? = null,
    val sources: List<String> = emptyList(),
    val left: String? = null,
    val right: String? = null,
    val args: List<String> = emptyList(),
    val path: String? = null,
    val key: String? = null,
    val defaultValue: Any? = null,
    val type: String? = null,
    val value: Any? = null,
    val values: List<Any?> = emptyList(),
    val predicatePath: String? = null,
    val predicateOperator: String? = null,
    val predicateValue: Any? = null,
    val thenSource: String? = null,
    val elseSource: String? = null,
    val thenValue: Any? = null,
    val elseValue: Any? = null,
)

data class CompiledVariable(
    val id: String,
    val sourceId: String,
    val name: String,
    val returnType: String? = null,
    val instructions: List<Instruction>,
    val compilable: Boolean = true,
)

data class RuleTreeNode(
    val type: String,
    val id: String? = null,
    val variable: String? = null,
    val operator: String? = null,
    val value: Any? = null,
    val value2: Any? = null,
    val fieldType: String? = null,
    val logic: String? = null,
    val children: List<RuleTreeNode> = emptyList(),
    val child: RuleTreeNode? = null,
    val onPass: String? = null,
    val onFail: String? = null,
)

data class CompiledRule(
    val id: String,
    val name: String,
    val ruleFormat: String = "v1",
    val tree: RuleTreeNode? = null,
    val nodes: List<Map<String, Any?>> = emptyList(),
    val expression: String? = null,
)

data class ScoreRange(
    val min: Double? = null,
    val max: Double? = null,
    val points: Int = 0,
)

data class ScoreFactor(
    val variableId: String,
    val ranges: List<ScoreRange>,
)

data class Scorecard(
    val id: String,
    val name: String,
    val baseScore: Int = 300,
    val maxScore: Int = 900,
    val bins: List<ScoreFactor>,
)

data class PolicyStep(
    val id: String? = null,
    val type: String,
    val ref: String? = null,
    val refId: String? = null,
    val label: String? = null,
    val config: Map<String, Any?> = emptyMap(),
)

data class Policy(
    val id: String,
    val name: String,
    val steps: List<PolicyStep>,
    val serverOnlySteps: List<String> = emptyList(),
    val defaultOutcome: String? = null,
)

data class ExperimentVariant(
    val id: String,
    val weight: Int,
    val overrides: Map<String, Any?> = emptyMap(),
)

data class Experiment(
    val id: String,
    val name: String,
    val status: String,
    val variants: List<ExperimentVariant>,
    val targetPolicyId: String? = null,
)

data class Bundle(
    val bundleVersion: Int,
    val bundleId: String,
    val tenantId: String,
    val compiledAt: String,
    val expiresAt: String,
    val variables: List<CompiledVariable>,
    val rules: List<CompiledRule>,
    val scorecards: List<Scorecard>,
    val policies: List<Policy>,
    val experiments: List<Experiment>,
    val serverOnlyVariables: List<String>,
    val checksum: String,
)

data class BundleEnvelope(
    val bundleVersion: Int,
    val encryptedBundle: String,
    val encryptedKey: String,
    val signature: String,
    val checksum: String,
    val compiledAt: String,
    val expiresAt: String,
)

data class BundleFetchResult(
    val changed: Boolean,
    val envelope: BundleEnvelope? = null,
)

data class SdkEvent(
    val type: String,
    val timestamp: String,
    val data: Map<String, Any?> = emptyMap(),
)
