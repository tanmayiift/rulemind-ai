import { describe, expect, it } from "vitest";
import { evaluateScorecard } from "../packages/rule-engine/src/decisioning";
import { EXCEL_FUNCTIONS, EXCEL_FUNCTION_COUNT } from "../packages/rule-engine/src/excel-functions";
import type { ScorecardDefinition } from "../packages/shared/src/types";

describe("Cross-platform parity — Excel functions", () => {
  it("exports a large function catalog", () => {
    expect(EXCEL_FUNCTION_COUNT).toBeGreaterThanOrEqual(100);
    expect(Object.keys(EXCEL_FUNCTIONS).length).toBe(EXCEL_FUNCTION_COUNT);
  });

  it("SUM matches Python behavior", () => {
    expect(EXCEL_FUNCTIONS.SUM(1, 2, 3, 4, 5)).toBe(15);
    expect(EXCEL_FUNCTIONS.SUM([10, 20, 30])).toBe(60);
  });

  it("AVERAGE matches Python behavior", () => {
    expect(EXCEL_FUNCTIONS.AVERAGE(10, 20, 30)).toBe(20);
  });

  it("IF matches Python behavior", () => {
    expect(EXCEL_FUNCTIONS.IF(true, "yes", "no")).toBe("yes");
    expect(EXCEL_FUNCTIONS.IF(false, "yes", "no")).toBe("no");
  });

  it("ROUND matches Python behavior", () => {
    expect(EXCEL_FUNCTIONS.ROUND(3.14159, 2)).toBe(3.14);
    expect(EXCEL_FUNCTIONS.ROUND(2.5, 0)).toBe(3);
  });

  it("PMT matches Python behavior", () => {
    const pmt = EXCEL_FUNCTIONS.PMT(0.05 / 12, 360, 200000);
    expect(pmt).toBeCloseTo(-1073.64, 1);
  });

  it("CONCATENATE matches Python behavior", () => {
    expect(EXCEL_FUNCTIONS.CONCATENATE("Hello", " ", "World")).toBe("Hello World");
  });

  it("AND / OR / NOT match Python behavior", () => {
    expect(EXCEL_FUNCTIONS.AND(true, true, true)).toBe(true);
    expect(EXCEL_FUNCTIONS.AND(true, false)).toBe(false);
    expect(EXCEL_FUNCTIONS.OR(false, false, true)).toBe(true);
    expect(EXCEL_FUNCTIONS.NOT(true)).toBe(false);
  });

  it("MIN / MAX match Python behavior", () => {
    expect(EXCEL_FUNCTIONS.MIN(5, 3, 8, 1)).toBe(1);
    expect(EXCEL_FUNCTIONS.MAX(5, 3, 8, 1)).toBe(8);
  });

  it("COUNTIF matches Python behavior", () => {
    expect(EXCEL_FUNCTIONS.COUNTIF([1, 2, 3, 2, 2], 2)).toBe(3);
  });

  it("TRIM / UPPER / LOWER match Python behavior", () => {
    expect(EXCEL_FUNCTIONS.TRIM("  hello  ")).toBe("hello");
    expect(EXCEL_FUNCTIONS.UPPER("hello")).toBe("HELLO");
    expect(EXCEL_FUNCTIONS.LOWER("HELLO")).toBe("hello");
  });

  it("ABS / SQRT / POWER match Python behavior", () => {
    expect(EXCEL_FUNCTIONS.ABS(-42)).toBe(42);
    expect(EXCEL_FUNCTIONS.SQRT(144)).toBe(12);
    expect(EXCEL_FUNCTIONS.POWER(2, 10)).toBe(1024);
  });

  it("MEDIAN matches Python behavior", () => {
    expect(EXCEL_FUNCTIONS.MEDIAN(1, 3, 5, 7, 9)).toBe(5);
    expect(EXCEL_FUNCTIONS.MEDIAN(1, 2, 3, 4)).toBe(2.5);
  });
});

describe("Cross-platform parity — Scorecard evaluation", () => {
  const baseDefinition: ScorecardDefinition = {
    factors: [
      { name: "credit_score", field: "credit_score", operator: ">=", value: 700, score: 50 },
      { name: "income", field: "income", operator: ">=", value: 50000, score: 30 }
    ],
    bands: [
      { min: 0, max: 40, label: "Low", decision: "reject" },
      { min: 41, max: 70, label: "Medium", decision: "review" },
      { min: 71, max: 100, label: "High", decision: "approve" }
    ]
  };

  it("points scoring — both pass", () => {
    const result = evaluateScorecard(baseDefinition, { credit_score: 750, income: 60000 });
    expect(result.score).toBe(80);
    expect(result.band?.label).toBe("High");
    expect(result.scoring_method).toBe("points");
  });

  it("points scoring — one fails", () => {
    const result = evaluateScorecard(baseDefinition, { credit_score: 600, income: 60000 });
    expect(result.score).toBe(30);
    expect(result.band?.label).toBe("Low");
  });

  it("points scoring — none pass", () => {
    const result = evaluateScorecard(baseDefinition, { credit_score: 500, income: 30000 });
    expect(result.score).toBe(0);
    expect(result.band?.label).toBe("Low");
  });

  it("weighted points scoring", () => {
    const def: ScorecardDefinition = {
      ...baseDefinition,
      factors: [
        { name: "credit_score", field: "credit_score", operator: ">=", value: 700, score: 50, weight: 2.0 },
        { name: "income", field: "income", operator: ">=", value: 50000, score: 30, weight: 0.5 }
      ]
    };
    const result = evaluateScorecard(def, { credit_score: 750, income: 60000 });
    expect(result.score).toBe(50 * 2.0 + 30 * 0.5); // 115
    expect(result.breakdown).toBeDefined();
    expect(result.breakdown![0].weighted_points).toBe(100);
    expect(result.breakdown![1].weighted_points).toBe(15);
  });

  it("WoE scoring produces correct formula result", () => {
    const woeDef: ScorecardDefinition = {
      factors: [
        {
          name: "bureau_score",
          field: "bureau_score",
          operator: ">=",
          value: 0,
          score: 0,
          coefficient: 1.5,
          woe_values: [
            { min: 0, max: 500, woe: -0.8, iv: 0.1, event_rate: 0.3, non_event_rate: 0.1 },
            { min: 500, max: 700, woe: 0.2, iv: 0.05, event_rate: 0.4, non_event_rate: 0.5 },
            { min: 700, max: 900, woe: 0.9, iv: 0.15, event_rate: 0.3, non_event_rate: 0.4 }
          ]
        }
      ],
      bands: [
        { min: 0, max: 500, label: "Poor" },
        { min: 501, max: 700, label: "Fair" },
        { min: 701, max: 999, label: "Good" }
      ],
      scoring_method: "woe",
      intercept: -1.2,
      pdo: 20,
      target_score: 600,
      target_odds: 50
    };

    const result = evaluateScorecard(woeDef, { bureau_score: 650 });
    expect(result.scoring_method).toBe("woe");
    expect(result.woe_details).toBeDefined();
    expect(result.woe_details!.length).toBe(1);
    expect(result.woe_details![0].woe).toBe(0.2);
    expect(result.woe_details![0].coefficient).toBe(1.5);
    expect(result.information_value).toBeCloseTo(0.05, 2);
    expect(typeof result.score).toBe("number");
    expect(isFinite(result.score)).toBe(true);
  });

  it("WoE scoring math is correct", () => {
    const woeDef: ScorecardDefinition = {
      factors: [
        {
          name: "age",
          field: "age",
          operator: ">=",
          value: 0,
          score: 0,
          coefficient: 0.8,
          woe_values: [
            { min: 18, max: 30, woe: -0.5, iv: 0.08, event_rate: 0.4, non_event_rate: 0.2 },
            { min: 30, max: 60, woe: 0.6, iv: 0.12, event_rate: 0.3, non_event_rate: 0.5 }
          ]
        }
      ],
      bands: [{ min: 0, max: 999, label: "All" }],
      scoring_method: "woe",
      intercept: -0.5,
      pdo: 20,
      target_score: 600,
      target_odds: 50
    };

    const result = evaluateScorecard(woeDef, { age: 35 });
    // Expected: offset + factor * (intercept + coeff * woe)
    const factor = 20 / Math.LN2;
    const offset = 600 - factor * Math.log(50);
    const expectedScore = Math.round(offset + factor * (-0.5 + 0.8 * 0.6));
    expect(result.score).toBe(expectedScore);
  });

  it("formula scoring uses custom expression", () => {
    const formulaDef: ScorecardDefinition = {
      factors: [
        { name: "credit_score", field: "credit_score", operator: ">=", value: 700, score: 50 }
      ],
      bands: [{ min: 0, max: 999, label: "All" }],
      scoring_method: "formula",
      formula: "factor_total * 2 + 100"
    };

    const result = evaluateScorecard(formulaDef, { credit_score: 750 });
    expect(result.scoring_method).toBe("formula");
    // factor_total = 50 (credit_score passes), formula = 50*2 + 100 = 200
    expect(result.score).toBe(200);
  });

  it("metrics are computed from score", () => {
    const def: ScorecardDefinition = {
      ...baseDefinition,
      metrics: [
        { name: "discount_pct", formula: "score > 70 ? 0.15 : 0.05" },
        { name: "loan_amount", formula: "score * 1000" }
      ]
    };

    const result = evaluateScorecard(def, { credit_score: 750, income: 60000 });
    expect(result.metrics).toBeDefined();
    expect(result.metrics!.discount_pct).toBe(0.15);
    expect(result.metrics!.loan_amount).toBe(80000);
  });

  it("metrics with low score", () => {
    const def: ScorecardDefinition = {
      ...baseDefinition,
      metrics: [
        { name: "discount_pct", formula: "score > 70 ? 0.15 : 0.05" }
      ]
    };

    const result = evaluateScorecard(def, { credit_score: 500, income: 30000 });
    expect(result.metrics!.discount_pct).toBe(0.05);
  });
});
