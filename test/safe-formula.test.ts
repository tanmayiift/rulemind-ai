import { describe, expect, it } from "vitest";
import { evaluateFormulaSafely, FormulaError } from "../packages/rule-engine/src/safe-formula";
import { EXCEL_FUNCTIONS } from "../packages/rule-engine/src/excel-functions";

/**
 * The TypeScript engine evaluated author-supplied scorecard formulas with
 * `new Function(...)` — real JavaScript eval. It was verified exploitable: a formula of
 * `((globalThis.__PWNED__ = 'arbitrary JS ran'), 1)` executed. This engine runs in the
 * dashboard, so that is arbitrary JS in the browser of anyone who opens or tests that
 * scorecard: stored XSS in a product whose policy authors are explicitly untrusted.
 *
 * The escapes below are not filtered by a denylist — the grammar simply has no production for
 * member access, subscripting, calling a call's result, `new`, or assignment, so they cannot be
 * spelled. That matters because a denylist loses: `(()=>{}).constructor` alone rebuilds Function.
 */
const fns = EXCEL_FUNCTIONS as never;

describe("no code execution", () => {
  const escapes = [
    "((globalThis.__PWNED__ = 'x'), 1)",  // the verified original exploit
    "globalThis",
    "constructor",
    "this.constructor.constructor('return 1')()",
    "(() => 1)()",
    "[].constructor",
    "process.exit(1)",
    "require('fs')",
    "score.__proto__",
    "MIN(1,2)(3)",           // calling the result of a call
    "x[0]",                  // subscripting
    "a = 1",                 // assignment
    "score ? 1 : 2",         // JS ternary; Python uses `a if c else b`
  ];
  for (const src of escapes) {
    it(`refuses ${src}`, () => {
      expect(() => evaluateFormulaSafely(src, { score: 1, x: [1], a: 1 }, fns)).toThrow(FormulaError);
    });
  }

  it("the original exploit does not reach globalThis", () => {
    const g = globalThis as unknown as Record<string, unknown>;
    delete g.__PWNED_TS__;
    try {
      evaluateFormulaSafely("((globalThis.__PWNED_TS__ = 'ran'), 1)", {}, fns);
    } catch { /* expected */ }
    expect(g.__PWNED_TS__).toBeUndefined();
  });
});

describe("real formulas still evaluate, with Python semantics", () => {
  const ctx = { base_score: 300, total_points: 42, score: 720, total_woe_score: 1.5 };
  const cases: Array<[string, unknown]> = [
    ["base_score + total_points", 342],
    ["base_score + total_points * 2", 384],
    ["100 if score >= 700 else 0", 100],       // Python conditional, not the JS ternary
    ["0 if score >= 900 else 55", 55],
    ["2 ** 10", 1024],
    ["7 / 2", 3.5],                            // true division
    ["7 // 2", 3],                             // floors
    ["-7 // 2", -4],                           // floors toward -inf, unlike JS trunc
    ["7 % 3", 1],
    ["-7 % 3", 2],                             // sign of the DIVISOR, unlike JS -1
    ["True + 1", 2],                           // bools are numeric in Python
    ["0 < score < 1000", true],                // chained comparison
    ["1000 < score < 2000", false],
    ["MIN(3, 9)", 3],
    ["MAX(MIN(score, 100), 0)", 100],
  ];
  for (const [src, want] of cases) {
    it(`${src} -> ${want}`, () => {
      expect(evaluateFormulaSafely(src, ctx, fns)).toBe(want);
    });
  }

  it("and/or short-circuit and return an operand, as in Python", () => {
    expect(evaluateFormulaSafely("a and b", { a: 5, b: 0 }, fns)).toBe(0);
    expect(evaluateFormulaSafely("a or b", { a: 0, b: 7 }, fns)).toBe(7);
  });
});

describe("bounded", () => {
  it("refuses an exponent bomb", () => {
    expect(() => evaluateFormulaSafely("10 ** 10 ** 10", {}, fns)).toThrow(FormulaError);
  });
  it("refuses a string-repetition bomb", () => {
    expect(() => evaluateFormulaSafely("'a' * 999999999", {}, fns)).toThrow(FormulaError);
  });
  it("division by zero is an error, not a crash", () => {
    for (const src of ["1 / 0", "1 % 0", "1 // 0"]) {
      expect(() => evaluateFormulaSafely(src, {}, fns)).toThrow(FormulaError);
    }
  });
});
