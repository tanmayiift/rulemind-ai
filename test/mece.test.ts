import { describe, expect, it } from "vitest";
import { analyzeMECE, extractRuleConstraintSpace } from "../packages/rule-engine/src";
import type { RuleDefinition } from "../packages/shared/src";
import type { MECERuleInput } from "../packages/rule-engine/src/mece";

// ---------------------------------------------------------------------------
// Helper to build a simple condition rule
// ---------------------------------------------------------------------------

function conditionRule(
  id: string,
  name: string,
  field: string,
  fieldType: "number" | "string" | "boolean" | "date" | "enum",
  operator: string,
  value: string,
  value2?: string,
  outcome: "approve" | "reject" | "review" = "approve"
): MECERuleInput {
  const nodes: RuleDefinition["nodes"] = [
    {
      id: "c1",
      type: "condition",
      label: name,
      x: 0,
      y: 0,
      config: {
        field,
        fieldType,
        operator: operator as any,
        value,
        ...(value2 !== undefined ? { value2 } : {}),
      },
    },
    { id: "out1", type: outcome, label: outcome, x: 0, y: 0, config: {} },
  ];
  return {
    id,
    name,
    definition: { nodes, connections: [{ from: "c1", to: "out1" }] },
    outcome,
  };
}

// ---------------------------------------------------------------------------
// Test Suite
// ---------------------------------------------------------------------------

describe("MECE Analysis", () => {
  // ========================================================================
  // 1. BASIC NUMERIC PARTITION
  // ========================================================================

  it("detects perfect MECE partition (>= and <)", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "High Score", "score", "number", ">=", "700", undefined, "approve"),
      conditionRule("r2", "Low Score", "score", "number", "<", "700", undefined, "reject"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.isCollectivelyExhaustive).toBe(true);
    expect(result.diagnostics.filter((d) => d.type === "overlap")).toHaveLength(0);
    expect(result.diagnostics.filter((d) => d.type === "gap" && d.severity === "error")).toHaveLength(0);
    expect(result.ruleCount).toBe(2);
    expect(result.analyzedFields).toContain("score");
  });

  // ========================================================================
  // 2. NUMERIC OVERLAP
  // ========================================================================

  it("detects numeric overlap (>= 700 and >= 650)", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "High Score", "score", "number", ">=", "700"),
      conditionRule("r2", "Medium Score", "score", "number", ">=", "650"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(false);
    expect(result.diagnostics.some((d) => d.type === "overlap")).toBe(true);
    const overlap = result.diagnostics.find((d) => d.type === "overlap")!;
    expect(overlap.involvedRules).toContain("r1");
    expect(overlap.involvedRules).toContain("r2");
    expect(overlap.fields).toContain("score");
    expect(overlap.severity).toBe("error");
    // Should include node IDs for UI highlighting
    expect(overlap.involvedNodeIds).toBeDefined();
    expect(overlap.involvedNodeIds!.length).toBeGreaterThan(0);
  });

  // ========================================================================
  // 3. NUMERIC GAP
  // ========================================================================

  it("detects numeric gap (>= 700 and < 650)", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "High Score", "score", "number", ">=", "700", undefined, "approve"),
      conditionRule("r2", "Low Score", "score", "number", "<", "650", undefined, "reject"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.isCollectivelyExhaustive).toBe(false);
    expect(result.diagnostics.some((d) => d.type === "gap" && d.severity === "error")).toBe(true);
  });

  // ========================================================================
  // 4. CATEGORICAL OVERLAP
  // ========================================================================

  it("detects categorical value set overlap (shared 'UK')", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "US/UK", "country", "string", "in", "US, UK"),
      conditionRule("r2", "UK/FR", "country", "string", "in", "UK, FR"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(false);
    const overlap = result.diagnostics.find((d) => d.type === "overlap")!;
    expect(overlap.description).toContain("UK");
  });

  // ========================================================================
  // 5. BOOLEAN COMPLETENESS
  // ========================================================================

  it("detects boolean completeness (true + false = MECE)", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Verified", "verified", "boolean", "==", "true", undefined, "approve"),
      conditionRule("r2", "Unverified", "verified", "boolean", "==", "false", undefined, "reject"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.diagnostics.filter((d) => d.type === "overlap")).toHaveLength(0);
    // Boolean exhaustiveness: both true and false are covered
    expect(result.diagnostics.filter((d) => d.type === "gap" && d.severity === "error" && d.fields.includes("verified"))).toHaveLength(0);
  });

  it("detects missing boolean value (only true covered)", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Verified", "verified", "boolean", "==", "true", undefined, "approve"),
      conditionRule("r2", "Also Verified", "verified", "boolean", "==", "true", undefined, "review"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(false); // Both fire on true
    expect(result.diagnostics.some((d) => d.type === "gap" && d.fields.includes("verified"))).toBe(true);
  });

  // ========================================================================
  // 6. BETWEEN RANGE OVERLAP AT BOUNDARY
  // ========================================================================

  it("detects between-range overlap at boundary", () => {
    // [500, 700] and [700, 900] overlap at exactly 700
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Mid Range", "score", "number", "between", "500", "700"),
      conditionRule("r2", "High Range", "score", "number", "between", "700", "900"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(false);
    expect(result.diagnostics.some((d) => d.type === "overlap" && d.fields.includes("score"))).toBe(true);
  });

  // ========================================================================
  // 7. AND-COMBINED MULTI-FIELD RULES
  // ========================================================================

  it("handles AND-combined multi-field rules", () => {
    const rule1: MECERuleInput = {
      id: "r1",
      name: "High Score Adult",
      outcome: "approve",
      definition: {
        nodes: [
          { id: "a1", type: "and", label: "AND", x: 0, y: 0, config: {} },
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } },
          { id: "c2", type: "condition", label: "Age", x: 0, y: 0, config: { field: "age", fieldType: "number", operator: ">=", value: "18" } },
          { id: "out1", type: "approve", label: "Approve", x: 0, y: 0, config: {} },
        ],
        connections: [
          { from: "a1", to: "c1" },
          { from: "a1", to: "c2" },
          { from: "a1", to: "out1" },
        ],
      },
    };
    const rule2: MECERuleInput = {
      id: "r2",
      name: "Low Score Adult",
      outcome: "reject",
      definition: {
        nodes: [
          { id: "a1", type: "and", label: "AND", x: 0, y: 0, config: {} },
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: "<", value: "700" } },
          { id: "c2", type: "condition", label: "Age", x: 0, y: 0, config: { field: "age", fieldType: "number", operator: ">=", value: "18" } },
          { id: "out1", type: "reject", label: "Reject", x: 0, y: 0, config: {} },
        ],
        connections: [
          { from: "a1", to: "c1" },
          { from: "a1", to: "c2" },
          { from: "a1", to: "out1" },
        ],
      },
    };
    const result = analyzeMECE([rule1, rule2]);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.isCollectivelyExhaustive).toBe(false);
    expect(result.diagnostics.some((d) => d.type === "gap" && d.fields.includes("age"))).toBe(true);
  });

  // ========================================================================
  // 8. OR-BRANCH EXPANSION
  // ========================================================================

  it("handles OR-branch expansion", () => {
    const rule1: MECERuleInput = {
      id: "r1",
      name: "Acceptable Score",
      outcome: "approve",
      definition: {
        nodes: [
          { id: "o1", type: "or", label: "OR", x: 0, y: 0, config: {} },
          { id: "c1", type: "condition", label: "High", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } },
          { id: "c2", type: "condition", label: "Mid", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: "between", value: "500", value2: "699" } },
          { id: "out1", type: "approve", label: "Approve", x: 0, y: 0, config: {} },
        ],
        connections: [
          { from: "o1", to: "c1" },
          { from: "o1", to: "c2" },
        ],
      },
    };
    const branches = extractRuleConstraintSpace(rule1.definition);
    expect(branches.length).toBe(2);
    expect(branches.every((b) => b.length > 0)).toBe(true);
  });

  // ========================================================================
  // 9. SINGLE RULE
  // ========================================================================

  it("reports single rule as ME but not CE", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "High Score", "score", "number", ">=", "700"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.isCollectivelyExhaustive).toBe(false);
    expect(result.ruleCount).toBe(1);
  });

  // ========================================================================
  // 10. EMPTY POLICY
  // ========================================================================

  it("reports empty policy as not exhaustive", () => {
    const result = analyzeMECE([]);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.isCollectivelyExhaustive).toBe(false);
    expect(result.diagnostics).toHaveLength(1);
    expect(result.diagnostics[0].type).toBe("gap");
    expect(result.diagnostics[0].description).toContain("no rules");
  });

  // ========================================================================
  // 11. NOT OPERATOR
  // ========================================================================

  it("handles NOT operator inversion", () => {
    const rule1: MECERuleInput = {
      id: "r1",
      name: "Not High Score",
      outcome: "reject",
      definition: {
        nodes: [
          { id: "n1", type: "not", label: "NOT", x: 0, y: 0, config: {} },
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } },
          { id: "out1", type: "reject", label: "Reject", x: 0, y: 0, config: {} },
        ],
        connections: [{ from: "n1", to: "c1" }],
      },
    };
    const branches = extractRuleConstraintSpace(rule1.definition);
    expect(branches.length).toBeGreaterThan(0);
    const constraint = branches[0][0];
    expect(constraint.field).toBe("score");
    expect(constraint.intervals).toBeDefined();
    expect(constraint.intervals![0].upper).toBe(700);
    expect(constraint.intervals![0].upperInclusive).toBe(false);
  });

  // ========================================================================
  // 12. DISJOINT CATEGORICAL SETS
  // ========================================================================

  it("detects disjoint categorical sets as ME", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "US Only", "country", "string", "in", "US"),
      conditionRule("r2", "UK Only", "country", "string", "in", "UK"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.diagnostics.filter((d) => d.type === "overlap")).toHaveLength(0);
  });

  // ========================================================================
  // 13. ADJACENT RANGES (NO OVERLAP)
  // ========================================================================

  it("accepts adjacent between ranges with no overlap (exclusive boundary)", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Mid Range", "score", "number", "between", "500", "699"),
      conditionRule("r2", "High Range", "score", "number", "between", "700", "900"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.diagnostics.filter((d) => d.type === "overlap")).toHaveLength(0);
  });

  // ========================================================================
  // 14. TRIGGER NODE PASSTHROUGH
  // ========================================================================

  it("passes through trigger nodes correctly", () => {
    const rule: MECERuleInput = {
      id: "r1",
      name: "Triggered Rule",
      outcome: "approve",
      definition: {
        nodes: [
          { id: "t1", type: "trigger", label: "On Submit", x: 0, y: 0, config: { event: "on_submit" } },
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } },
          { id: "out1", type: "approve", label: "Approve", x: 0, y: 0, config: {} },
        ],
        connections: [
          { from: "t1", to: "c1" },
          { from: "c1", to: "out1" },
        ],
      },
    };
    const branches = extractRuleConstraintSpace(rule.definition);
    expect(branches.length).toBeGreaterThan(0);
    expect(branches[0].some((c) => c.field === "score")).toBe(true);
  });

  // ========================================================================
  // 15. THREE-WAY PARTITION
  // ========================================================================

  it("validates three-way numeric partition", () => {
    const r1 = conditionRule("r1", "Low", "score", "number", "<", "500", undefined, "reject");
    const r2: MECERuleInput = {
      id: "r2",
      name: "Mid",
      outcome: "review",
      definition: {
        nodes: [
          { id: "a1", type: "and", label: "AND", x: 0, y: 0, config: {} },
          { id: "c1", type: "condition", label: "ScoreLow", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "500" } },
          { id: "c2", type: "condition", label: "ScoreHigh", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: "<", value: "700" } },
          { id: "out1", type: "review", label: "Review", x: 0, y: 0, config: {} },
        ],
        connections: [
          { from: "a1", to: "c1" },
          { from: "a1", to: "c2" },
          { from: "a1", to: "out1" },
        ],
      },
    };
    const r3 = conditionRule("r3", "High", "score", "number", ">=", "700", undefined, "approve");
    const result = analyzeMECE([r1, r2, r3]);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.isCollectivelyExhaustive).toBe(true);
    expect(result.diagnostics.filter((d) => d.type === "overlap")).toHaveLength(0);
    expect(result.diagnostics.filter((d) => d.type === "gap" && d.severity === "error")).toHaveLength(0);
    expect(result.ruleCount).toBe(3);
  });

  // ========================================================================
  // 16. != OPERATOR (NUMERIC)
  // ========================================================================

  it("handles != operator correctly (excludes single point)", () => {
    // Rule 1: score != 500 and Rule 2: score == 500 → perfect MECE
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Not 500", "score", "number", "!=", "500", undefined, "review"),
      conditionRule("r2", "Exactly 500", "score", "number", "==", "500", undefined, "approve"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
  });

  // ========================================================================
  // 17. REGEX OPERATOR (OPAQUE)
  // ========================================================================

  it("warns about regex operators being opaque", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Pattern Match", "email", "string", "regex", ".*@example\\.com"),
      conditionRule("r2", "Other", "email", "string", "regex", ".*@other\\.com"),
    ];
    const result = analyzeMECE(rules);
    expect(result.hasOpaqueConstraints).toBe(true);
    expect(result.warnings.some((w) => w.includes("opaque"))).toBe(true);
  });

  // ========================================================================
  // 18. CATEGORICAL GAP WARNING
  // ========================================================================

  it("warns when categorical field only covers specific values", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "US", "country", "string", "in", "US", undefined, "approve"),
      conditionRule("r2", "UK", "country", "string", "in", "UK", undefined, "reject"),
    ];
    const result = analyzeMECE(rules);
    // Should warn that other countries aren't covered
    expect(result.diagnostics.some((d) =>
      d.type === "gap" && d.severity === "warning" && d.fields.includes("country")
    )).toBe(true);
  });

  // ========================================================================
  // 19. MULTI-INDUSTRY: INSURANCE RISK BANDS
  // ========================================================================

  it("validates insurance risk banding (4-tier with continuous ranges)", () => {
    // Using AND-combined >= and < to create proper continuous partitions
    const mkBand = (id: string, name: string, lo: string, hi: string, outcome: "approve" | "review" | "reject"): MECERuleInput => ({
      id, name, outcome,
      definition: {
        nodes: [
          { id: "a1", type: "and", label: "AND", x: 0, y: 0, config: {} },
          { id: "c1", type: "condition", label: "Lo", x: 0, y: 0, config: { field: "risk_score", fieldType: "number", operator: ">=", value: lo } },
          { id: "c2", type: "condition", label: "Hi", x: 0, y: 0, config: { field: "risk_score", fieldType: "number", operator: "<", value: hi } },
          { id: "out1", type: outcome, label: outcome, x: 0, y: 0, config: {} },
        ],
        connections: [{ from: "a1", to: "c1" }, { from: "a1", to: "c2" }, { from: "a1", to: "out1" }],
      },
    });
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Low Risk", "risk_score", "number", "<", "25", undefined, "approve"),
      mkBand("r2", "Medium Risk", "25", "50", "review"),
      mkBand("r3", "High Risk", "50", "75", "review"),
      conditionRule("r4", "Critical Risk", "risk_score", "number", ">=", "75", undefined, "reject"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
    expect(result.isCollectivelyExhaustive).toBe(true);
    expect(result.ruleCount).toBe(4);
  });

  // ========================================================================
  // 20. MULTI-INDUSTRY: FRAUD DETECTION WITH not_in
  // ========================================================================

  it("detects overlap with not_in vs in on shared values", () => {
    // not_in {US, UK} overlaps with in {UK, FR} on FR (both accept FR)
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Non-US/UK", "country", "string", "not_in", "US, UK"),
      conditionRule("r2", "UK/FR", "country", "string", "in", "UK, FR"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(false);
    const overlap = result.diagnostics.find((d) => d.type === "overlap")!;
    expect(overlap.description).toContain("FR");
  });

  // ========================================================================
  // 21. EMPTY/DEGENERATE RULE DEFINITIONS
  // ========================================================================

  it("handles rules with no conditions gracefully", () => {
    const rules: MECERuleInput[] = [
      {
        id: "r1",
        name: "Empty Rule",
        outcome: "approve",
        definition: {
          nodes: [{ id: "a1", type: "approve", label: "Approve", x: 0, y: 0, config: {} }],
          connections: [],
        },
      },
      conditionRule("r2", "Has Condition", "score", "number", ">=", "700", undefined, "reject"),
    ];
    // Should not throw
    const result = analyzeMECE(rules);
    expect(result.ruleCount).toBe(2);
  });

  // ========================================================================
  // 22. exists vs !exists
  // ========================================================================

  it("treats exists and !exists as non-overlapping", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Has Flag", "flag", "string", "exists", "", undefined, "approve"),
      conditionRule("r2", "No Flag", "flag", "string", "!exists", "", undefined, "reject"),
    ];
    const result = analyzeMECE(rules);
    expect(result.isMutuallyExclusive).toBe(true);
  });

  // ========================================================================
  // 23. NaN value handling
  // ========================================================================

  it("handles NaN values safely", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Bad Value", "score", "number", ">=", "abc"),
      conditionRule("r2", "Good Score", "score", "number", ">=", "700"),
    ];
    // Should not throw
    const result = analyzeMECE(rules);
    expect(result.ruleCount).toBe(2);
  });

  // ========================================================================
  // 24. Swapped between bounds
  // ========================================================================

  it("handles swapped between bounds (value > value2)", () => {
    // between 900 500 should be treated as between 500 900
    const rules: MECERuleInput[] = [
      conditionRule("r1", "Range", "score", "number", "between", "900", "500"),
    ];
    const branches = extractRuleConstraintSpace(rules[0].definition);
    expect(branches[0][0].intervals![0].lower).toBe(500);
    expect(branches[0][0].intervals![0].upper).toBe(900);
  });

  // ========================================================================
  // 25. hasOpaqueConstraints and warnings fields
  // ========================================================================

  it("returns correct metadata fields", () => {
    const rules: MECERuleInput[] = [
      conditionRule("r1", "High", "score", "number", ">=", "700", undefined, "approve"),
      conditionRule("r2", "Low", "score", "number", "<", "700", undefined, "reject"),
    ];
    const result = analyzeMECE(rules);
    expect(typeof result.hasOpaqueConstraints).toBe("boolean");
    expect(Array.isArray(result.warnings)).toBe(true);
    expect(result.hasOpaqueConstraints).toBe(false);
    expect(result.warnings).toHaveLength(0);
  });
});
