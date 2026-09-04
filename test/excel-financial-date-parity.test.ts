/**
 * CUMIPMT and DAYS360 were Python-only with ZERO tests anywhere.
 *
 * `docs/parity/parity.json` carries them as an `engineSemantics: true` row -- "Excel functions
 * (CUMIPMT, DAYS360, EDATE, EOMONTH)" -- meaning the five engines must agree on them. Two of
 * the four existed only in `app/excel_functions.py`, had no TypeScript twin, and had no test in
 * apps/python-executor/tests, test/, or any SDK conformance suite. A row inside the semantics
 * contract with no coverage on either side of it.
 *
 * The corpus is GENERATED from the Python implementation rather than hand-written, for the same
 * reason the scorecard corpus is: `cross-platform-parity.test.ts` is named for parity but only
 * ever compared TypeScript against numbers a human typed, which is how ten divergences from
 * Python survived in it. Numbers a human types agree with whatever the human believed.
 *
 * Regenerate with the script in the W6.4 commit if the Python changes.
 */
import { describe, it, expect } from "vitest";

import corpus from "../packages/shared/excel-financial-date.spec.json";
import { EXCEL_FUNCTIONS } from "../packages/rule-engine/src/excel-functions";

interface Case {
  fn: string;
  args: unknown[];
  expected: number;
}

const CASES = (corpus as { cases: Case[] }).cases;

// Financial results are floats built by iterated multiplication, so the two languages can
// differ in the last bits. 1e-9 relative is far tighter than any decision would notice and far looser
// than IEEE noise; DAYS360 returns integers and is compared exactly.
function closeEnough(actual: number, expected: number): boolean {
  if (Number.isInteger(expected) && Number.isInteger(actual)) return actual === expected;
  const scale = Math.max(1, Math.abs(expected));
  return Math.abs(actual - expected) / scale < 1e-9;
}

describe("Excel financial + date functions match the Python engine", () => {
  it("has a corpus covering both functions the parity matrix names", () => {
    const covered = new Set(CASES.map((c) => c.fn));
    expect(covered.has("CUMIPMT")).toBe(true);
    expect(covered.has("DAYS360")).toBe(true);
    expect(CASES.length).toBeGreaterThan(40);
  });

  for (const [index, testCase] of CASES.entries()) {
    it(`${testCase.fn}(${testCase.args.join(", ")}) [#${index}]`, () => {
      const fn = EXCEL_FUNCTIONS[testCase.fn];
      expect(fn, `${testCase.fn} is not registered in the TypeScript engine`).toBeTypeOf("function");
      const actual = fn(...testCase.args);
      expect(closeEnough(actual, testCase.expected),
        `${testCase.fn} -> ${actual}, Python -> ${testCase.expected}`).toBe(true);
    });
  }
});

describe("the DAYS360 asymmetry a from-scratch port gets wrong", () => {
  it("US convention only pulls d2 back from 31 when d1 is already >= 30", () => {
    // d1 is 30 here, so d2 IS pulled back from 31 to 30 and the answer is 60. Move the start
    // one day earlier and d1 is 29, so d2 stays 31 and the answer jumps to 62 -- not 61.
    // Clamping both ends unconditionally (the obvious reading) gives 61 and is wrong.
    expect(EXCEL_FUNCTIONS.DAYS360("2024-01-30", "2024-03-31")).toBe(60);
    expect(EXCEL_FUNCTIONS.DAYS360("2024-01-29", "2024-03-31")).toBe(62);
    // European clamps both ends unconditionally, so the same pair is 61.
    expect(EXCEL_FUNCTIONS.DAYS360("2024-01-29", "2024-03-31", true)).toBe(61);
  });

  it("does not shift a date across a timezone boundary", () => {
    // `new Date("2024-01-30")` is UTC midnight but `getDate()` reads local time, so west of
    // Greenwich the naive version reports the 29th -- and this function is arithmetic on that
    // number. Same day in, same day out, wherever the test runs.
    expect(EXCEL_FUNCTIONS.DAYS360("2024-01-15", "2024-01-15")).toBe(0);
    expect(EXCEL_FUNCTIONS.DAYS360("2024-03-01", "2024-03-02")).toBe(1);
  });
});
