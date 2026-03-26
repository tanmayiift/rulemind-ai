import type {
  EvaluationResult,
  ExperimentDefinition,
  PolicyDefinition,
  RuleRecord,
  ScorecardDefinition,
  SegmentDefinition
} from "@rulemind/shared";
import { evaluateRule } from "./evaluator";

export function evaluateScorecard(definition: ScorecardDefinition, input: Record<string, unknown>) {
  const total = definition.factors.reduce((sum, factor) => {
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
          }
        ],
        connections: []
      },
      { [factor.field]: actual }
    );

    return sum + (result.passed ? factor.score : 0);
  }, 0);

  const band = definition.bands.find((candidate) => total >= candidate.min && total <= candidate.max);

  return {
    score: total,
    band
  };
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
