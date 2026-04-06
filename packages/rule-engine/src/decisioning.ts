import type {
  EvaluationResult,
  ExperimentDefinition,
  PolicyDefinition,
  RuleRecord,
  ScorecardDefinition,
  ScorecardResult,
  SegmentDefinition,
  WoEDetail,
  MetricFormula
} from "@rulemind/shared";
import { evaluateRule } from "./evaluator";
import { EXCEL_FUNCTIONS } from "./excel-functions";

/**
 * Evaluate a simple Excel-style formula string with given context variables.
 */
function evaluateFormula(formula: string, context: Record<string, unknown>): unknown {
  try {
    const fnNames = Object.keys(EXCEL_FUNCTIONS);
    const fnValues = Object.values(EXCEL_FUNCTIONS);
    const ctxNames = Object.keys(context);
    const ctxValues = Object.values(context);
    const fn = new Function(...fnNames, ...ctxNames, `return (${formula});`);
    return fn(...fnValues, ...ctxValues);
  } catch {
    return 0;
  }
}

export function evaluateScorecard(definition: ScorecardDefinition, input: Record<string, unknown>): ScorecardResult {
  const scoringMethod = definition.scoring_method ?? "points";

  if (scoringMethod === "woe") {
    return evaluateScorecardWoE(definition, input);
  }

  if (scoringMethod === "formula" && definition.formula) {
    return evaluateScorecardFormula(definition, input);
  }

  // Default: points-based scoring (backward compatible, now with weights)
  const breakdown: Array<{ factor: string; points: number; weight: number; weighted_points: number }> = [];

  const total = definition.factors.reduce((sum, factor) => {
    const actual = input[factor.field];
    const weight = factor.weight ?? 1.0;
    const result = evaluateRule(
      {
        nodes: [
          {
            id: `${factor.name}_condition`,
            type: "condition",
            label: factor.name,
            x: 0,
            y: 0,
            config: {
              field: factor.field,
              fieldType: "number",
              operator: factor.operator,
              value: factor.value,
              value2: factor.value2
            }
          },
          {
            id: `${factor.name}_approve`,
            type: "approve",
            label: "Pass",
            x: 0,
            y: 0,
            config: { reason: "Factor matched" }
          }
        ],
        connections: [{ from: `${factor.name}_condition`, to: `${factor.name}_approve` }]
      },
      { [factor.field]: actual }
    );

    const points = result.passed ? factor.score : 0;
    const weightedPoints = points * weight;
    breakdown.push({ factor: factor.name, points, weight, weighted_points: weightedPoints });

    return sum + weightedPoints;
  }, 0);

  const band = definition.bands.find((candidate) => total >= candidate.min && total <= candidate.max);

  // Compute metrics if any
  const metrics = computeMetrics(definition.metrics, total, input, breakdown);

  return {
    score: total,
    band,
    breakdown,
    scoring_method: "points",
    metrics
  };
}

function evaluateScorecardWoE(definition: ScorecardDefinition, input: Record<string, unknown>): ScorecardResult {
  const intercept = definition.intercept ?? 0;
  const pdo = definition.pdo ?? 20;
  const targetScore = definition.target_score ?? 600;
  const targetOdds = definition.target_odds ?? 50;

  const factor_ = pdo / Math.LN2;
  const offset = targetScore - factor_ * Math.log(targetOdds);

  const woeDetails: WoEDetail[] = [];
  let totalIV = 0;
  let logOddsSum = intercept;

  for (const factor of definition.factors) {
    const actual = input[factor.field];
    const coefficient = factor.coefficient ?? 0;
    const woeValues = factor.woe_values ?? [];

    // Find matching WoE range
    let matchedWoE = 0;
    let matchedIV = 0;
    const numActual = typeof actual === "number" ? actual : parseFloat(String(actual));

    for (const range of woeValues) {
      if (!isNaN(numActual) && numActual >= range.min && numActual < range.max) {
        matchedWoE = range.woe;
        matchedIV = range.iv;
        break;
      }
    }

    const contribution = coefficient * matchedWoE;
    logOddsSum += contribution;
    totalIV += matchedIV;

    woeDetails.push({
      factor: factor.name,
      woe: matchedWoE,
      coefficient,
      contribution
    });
  }

  const score = Math.round(offset + factor_ * logOddsSum);

  const band = definition.bands.find((candidate) => score >= candidate.min && score <= candidate.max);

  const metrics = computeMetrics(definition.metrics, score, input, []);

  return {
    score,
    band,
    scoring_method: "woe",
    woe_details: woeDetails,
    information_value: totalIV,
    metrics
  };
}

function evaluateScorecardFormula(definition: ScorecardDefinition, input: Record<string, unknown>): ScorecardResult {
  const context: Record<string, unknown> = { ...input };

  // Build factor scores for formula access
  const factorScores: Record<string, number> = {};
  for (const factor of definition.factors) {
    const actual = input[factor.field];
    const result = evaluateRule(
      {
        nodes: [
          {
            id: `${factor.name}_condition`,
            type: "condition",
            label: factor.name,
            x: 0,
            y: 0,
            config: {
              field: factor.field,
              fieldType: "number",
              operator: factor.operator,
              value: factor.value,
              value2: factor.value2
            }
          },
          {
            id: `${factor.name}_approve`,
            type: "approve",
            label: "Pass",
            x: 0,
            y: 0,
            config: { reason: "Factor matched" }
          }
        ],
        connections: [{ from: `${factor.name}_condition`, to: `${factor.name}_approve` }]
      },
      { [factor.field]: actual }
    );
    factorScores[factor.name] = result.passed ? factor.score : 0;
  }

  context["factors"] = factorScores;
  context["factor_total"] = Object.values(factorScores).reduce((a, b) => a + b, 0);

  const score = Number(evaluateFormula(definition.formula!, context)) || 0;
  const band = definition.bands.find((candidate) => score >= candidate.min && score <= candidate.max);

  const metrics = computeMetrics(definition.metrics, score, input, []);

  return {
    score,
    band,
    scoring_method: "formula",
    metrics
  };
}

function computeMetrics(
  metricDefs: MetricFormula[] | undefined,
  score: number,
  input: Record<string, unknown>,
  breakdown: Array<{ factor: string; points: number; weight: number; weighted_points: number }>
): Record<string, unknown> | undefined {
  if (!metricDefs || metricDefs.length === 0) return undefined;

  const context: Record<string, unknown> = {
    ...input,
    score,
    breakdown
  };

  const results: Record<string, unknown> = {};
  for (const metric of metricDefs) {
    results[metric.name] = evaluateFormula(metric.formula, context);
  }
  return results;
}

export function resolveSegments(segments: SegmentDefinition[], input: Record<string, unknown>) {
  return [...segments]
    .sort((left, right) => left.priority - right.priority)
    .filter((segment) => evaluateRule(segment.criteria, input).passed);
}

export function resolvePolicy(policy: PolicyDefinition, ruleMap: Map<string, RuleRecord>) {
  return {
    requiredRules: policy.requiredRuleIds?.map((id) => ruleMap.get(id)).filter(Boolean) ?? [],
    regularRules: policy.ruleIds.map((id) => ruleMap.get(id)).filter(Boolean),
    overrideRules: policy.overrideRuleIds?.map((id) => ruleMap.get(id)).filter(Boolean) ?? []
  };
}

export function assignExperimentVariant(experiment: ExperimentDefinition, subjectId: string) {
  const hash = [...subjectId].reduce((sum, character, index) => sum + character.charCodeAt(0) * (index + 1), 0);
  const bucket = hash % 100;
  let cumulative = 0;

  return (
    experiment.variants.find((variant) => {
      cumulative += variant.traffic;
      return bucket < cumulative;
    }) ?? experiment.variants[0]
  );
}

export function summarizePolicyEvaluation(results: EvaluationResult[]) {
  const hasReject = results.some((result) => result.outcome === "reject");
  const hasReview = results.some((result) => result.outcome === "review");

  if (hasReject) {
    return "reject";
  }

  if (hasReview) {
    return "review";
  }

  return "approve";
}
