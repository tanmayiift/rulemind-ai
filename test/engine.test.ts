import { describe, expect, it } from "vitest";
import { evaluateRule } from "../packages/rule-engine/src/evaluator";

describe("evaluateRule", () => {
  it("evaluates numeric and outcome nodes", () => {
    const result = evaluateRule(
      {
        nodes: [
          {
            id: "t",
            type: "trigger",
            label: "Trigger",
            x: 0,
            y: 0,
            config: { event: "application_submitted" }
          },
          {
            id: "c",
            type: "condition",
            label: "Score above 700",
            x: 0,
            y: 0,
            config: { field: "credit_score", fieldType: "number", operator: ">=", value: 700 }
          },
          {
            id: "a",
            type: "approve",
            label: "Approve",
            x: 0,
            y: 0,
            config: { reason: "Eligible" }
          }
        ],
        connections: [
          { from: "t", to: "c" },
          { from: "c", to: "a" }
        ]
      },
      {
        event: "application_submitted",
        credit_score: 735
      }
    );

    expect(result.passed).toBe(true);
    expect(result.outcome).toBe("approve");
    expect(result.conditionResults.c?.pass).toBe(true);
  });
});
