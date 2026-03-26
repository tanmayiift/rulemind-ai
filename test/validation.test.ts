import { describe, expect, it } from "vitest";
import { validateRuleDefinition } from "../packages/rule-engine/src/validator";

describe("validateRuleDefinition", () => {
  it("detects circular references", () => {
    const issues = validateRuleDefinition({
      nodes: [
        { id: "a", type: "condition", label: "A", x: 0, y: 0, config: { field: "one", operator: "==", value: "1" } },
        { id: "b", type: "approve", label: "B", x: 0, y: 0, config: {} }
      ],
      connections: [
        { from: "a", to: "b" },
        { from: "b", to: "a" }
      ]
    });

    expect(issues.some((issue) => issue.message.includes("Circular reference"))).toBe(true);
  });
});
