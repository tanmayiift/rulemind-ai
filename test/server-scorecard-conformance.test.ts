import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { evaluateServerScorecard } from "../packages/rule-engine/src/server-scorecard";

/**
 * The TypeScript arm of the scorecard conformance contract.
 *
 * Every engine is validated against the SAME corpus, packages/shared/scorecards.spec.json,
 * generated from app/logic.py as the recorded source of truth. The Python, Kotlin and Dart arms
 * already pass it; this is the fourth.
 *
 * It exists because `test/cross-platform-parity.test.ts` — despite its name — only ever compared
 * this engine against numbers a human typed, never against Python. Ten separate divergences
 * survived in it, including bin bounds treated as [min, max) instead of [min, max], half-up
 * rounding where Python rounds half-to-even, no clamping to max_score, and base_score omitted
 * from points scoring. A corpus generated FROM the source of truth cannot drift that way.
 */
const specPath = fileURLToPath(new URL("../packages/shared/scorecards.spec.json", import.meta.url));
const spec = JSON.parse(readFileSync(specPath, "utf8")) as {
  meta?: Record<string, unknown>;
  cases: Array<{
    name: string;
    group: string;
    scorecard: Record<string, unknown>;
    variables: Record<string, unknown>;
    expected: Record<string, unknown>;
  }>;
};

describe("Scorecard conformance — TypeScript engine, server shape", () => {
  it("the corpus is present and substantial", () => {
    expect(spec.cases.length).toBeGreaterThan(100);
  });

  it("covers every scoring method, so a pass cannot come from one code path", () => {
    const methods = new Set(spec.cases.map((c) => String(c.scorecard.scoring_method ?? "points")));
    for (const required of ["points", "woe", "formula"]) {
      expect(methods.has(required), `no case for scoring_method=${required}`).toBe(true);
    }
  });

  for (const testCase of spec.cases) {
    it(`${testCase.group}: ${testCase.name}`, () => {
      const result = evaluateServerScorecard(
        testCase.scorecard as never,
        testCase.variables as never
      );
      expect(result.score, "score").toBe(testCase.expected.score);
      if (testCase.expected.scoring_method !== undefined) {
        expect(result.scoring_method).toBe(testCase.expected.scoring_method);
      }
      if (testCase.expected.base_score !== undefined) {
        expect(result.base_score).toBe(testCase.expected.base_score);
      }
      if (testCase.expected.max_score !== undefined) {
        expect(result.max_score).toBe(testCase.expected.max_score);
      }
      if (typeof testCase.expected.information_value === "number") {
        expect(result.information_value).toBeCloseTo(testCase.expected.information_value as number, 9);
      }
    });
  }
});
