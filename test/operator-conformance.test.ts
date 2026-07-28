import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { evaluateCondition } from "../packages/rule-engine/src/evaluator";

/**
 * Cross-engine operator conformance. Every RuleMind evaluation engine (this TS
 * engine, the Python runtime, the Kotlin and Dart SDKs) is validated against
 * the SAME fixture file: packages/shared/operators.spec.json. This test is the
 * TypeScript arm; the Python arm lives at
 * apps/python-executor/tests/test_operators.py and the Dart arm at
 * packages/sdk-flutter/test/operator_conformance_test.dart.
 */

interface OperatorCase {
  name: string;
  actual: unknown;
  operator: string;
  value: unknown;
  value2?: unknown;
  fieldType?: string;
  expected: boolean;
}

const specPath = fileURLToPath(new URL("../packages/shared/operators.spec.json", import.meta.url));
const spec = JSON.parse(readFileSync(specPath, "utf8")) as {
  operators: Array<{ op: string }>;
  cases: OperatorCase[];
};

describe("Operator conformance — TypeScript engine", () => {
  it("covers all 12 operators in the fixture matrix", () => {
    const covered = new Set(spec.cases.map((testCase) => testCase.operator));
    for (const { op } of spec.operators) {
      expect(covered.has(op), `no fixture for operator ${op}`).toBe(true);
    }
  });

  for (const testCase of spec.cases) {
    it(`${testCase.operator}: ${testCase.name}`, () => {
      const result = evaluateCondition(
        {
          operator: testCase.operator as never,
          value: testCase.value,
          value2: testCase.value2,
          fieldType: testCase.fieldType as never,
          field: "x"
        },
        testCase.actual
      );
      expect(result).toBe(testCase.expected);
    });
  }
});
