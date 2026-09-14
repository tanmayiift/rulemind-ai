import { describe, expect, it } from "vitest";
import { evaluateCondition } from "../packages/rule-engine/src/evaluator";

/**
 * fieldType must be matched case-INSENSITIVELY, exactly as the Python source of truth does
 * (`app/logic.py:454`: `(field_type or "").lower() == "boolean"`).
 *
 * This engine used a bare `fieldType === "boolean"` / `=== "date"`. A field declared "Boolean"
 * or "Date" therefore skipped its typed branch and fell through to the generic comparison, so
 * the same policy decided differently in the browser than on the server.
 *
 * It was found by a differential fuzz of the RUST engine against Python, which had the identical
 * bug: 7,694 of 8,034 disagreements across 205,335 generated cases were this one line. The TS
 * engine had it too and nothing here caught it, because every fixture in
 * packages/shared/operators.spec.json spells fieldType in lowercase — so the corpus can only
 * ever exercise the branch that already worked.
 *
 * These cases deliberately use the capitalised spellings the corpus does not contain.
 */
describe("fieldType is case-insensitive", () => {
  const cases: Array<[string, unknown, unknown, string, boolean]> = [
    // Boolean inputs chosen so the TYPED branch and the generic branch DISAGREE. `"true" == true`
    // was a bad choice: the generic path agrees, so the case passes whether or not the typed
    // branch runs, and the mutation test proved it -- reverting the fix left those green while
    // the date cases went red. These five were found by probing for typed != generic.
    ["Boolean", "TRUE", true, "==", true],
    ["BOOLEAN", 0, false, "==", true],
    ["Boolean", "0", false, "==", true],
    ["Boolean", null, false, "==", true],
    ["Boolean", "", false, "==", true],
    ["boolean", "TRUE", true, "==", true],   // control: the spelling that always worked
    // A declared date orders by epoch, so spelling differences are equal, not merely comparable.
    ["Date", "2026-01-01", "2026-01-01T00:00:00Z", "==", true],
    ["DATE", "2026-01-01", "2026-01-01T00:00:00Z", "==", true],
    ["date", "2026-01-01", "2026-01-01T00:00:00Z", "==", true],  // control
    ["Date", "2026-06-01", "2026-01-01", ">", true],
  ];

  for (const [fieldType, actual, value, operator, expected] of cases) {
    it(`${fieldType} ${JSON.stringify(actual)} ${operator} ${JSON.stringify(value)} -> ${expected}`, () => {
      const result = evaluateCondition(
        { operator: operator as never, value, fieldType: fieldType as never, field: "x" },
        actual
      );
      expect(result).toBe(expected);
    });
  }

  it("a capitalised spelling agrees with its lowercase twin", () => {
    // The property that actually matters, stated directly: casing must not change the answer.
    const pairs: Array<[string, string, unknown, unknown]> = [
      ["Boolean", "boolean", "TRUE", true],
      ["DATE", "date", "2026-01-01", "2026-01-01T00:00:00Z"],
    ];
    for (const [upper, lower, actual, value] of pairs) {
      const call = (ft: string) =>
        evaluateCondition({ operator: "==" as never, value, fieldType: ft as never, field: "x" }, actual);
      expect(call(upper), `${upper} disagreed with ${lower}`).toBe(call(lower));
    }
  });
});
