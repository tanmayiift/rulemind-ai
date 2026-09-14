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

/**
 * One bin of a scorecard factor.
 *
 * [min]/[max] are `null` when the JSON omitted the key, which the Python core reads as
 * `float(candidate.get("min", 0))` — i.e. **0.0**, not an open bound. [matchable] is false when
 * the key was present but null or non-numeric: `float()` raises there and the Python core skips
 * that candidate entirely, so it can never match.
 */
data class ScoreRange(
    val min: Double? = null,
    val max: Double? = null,
    val points: Int = 0,
    val matchable: Boolean = true,
) {
    val lowerBound: Double get() = min ?: 0.0
    val upperBound: Double get() = max ?: 0.0
}

/** A weight-of-evidence bin: the same bound semantics as [ScoreRange], carrying woe/iv instead of points. */
data class WoeRange(
    val min: Double? = null,
    val max: Double? = null,
    val woe: Double = 0.0,
    val iv: Double = 0.0,
    val eventRate: Double? = null,
    val nonEventRate: Double? = null,
    val matchable: Boolean = true,
) {
    val lowerBound: Double get() = min ?: 0.0
    val upperBound: Double get() = max ?: 0.0
}

data class ScoreFactor(
    /** Null when the bin omits `variable_id`: the Python core then looks up the key `None`,
     *  finds nothing, and scores the factor against a value of 0. */
    val variableId: String?,
    val ranges: List<ScoreRange>,
    /** Multiplies this factor's points before they are summed (points / weighted methods). */
    val weight: Double = 1.0,
    /** The logistic-regression coefficient this factor's WoE is multiplied by. */
    val coefficient: Double = 0.0,
    val woeValues: List<WoeRange> = emptyList(),
)

/** A derived metric computed from the scored result by a formula (see [com.rulemind.core.SafeFormula]). */
data class ScorecardMetric(
    val name: String = "unnamed",
    val formula: String = "0",
    val unit: String = "",
    val category: String = "financial",
)

data class Scorecard(
    val id: String,
    val name: String,
    val baseScore: Int = 300,
    val maxScore: Int = 900,
    val bins: List<ScoreFactor>,
    /** "points" (default), "woe" or "formula". Anything else falls through to points, as in Python. */
    val scoringMethod: String = "points",
    // ── WoE (points-to-double-the-odds) calibration ──
    val intercept: Double = 0.0,
    val pdo: Double = 20.0,
    val targetScore: Double = 600.0,
    val targetOdds: Double = 50.0,
    /** Custom scoring expression for `scoringMethod == "formula"`. */
    val formula: String? = null,
    val metrics: List<ScorecardMetric> = emptyList(),
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
    // Decision tables travel as raw maps (rows/cells are dynamic, keyed by arbitrary
    // input ids) — the DecisionTableEvaluator reads them exactly like the Python core.
    val decisionTables: List<Map<String, Any?>> = emptyList(),
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
