/**
 * `@rulemind/rule-engine` exports two scorecard functions and the obvious name is the wrong one.
 *
 *   evaluateScorecard        -> the LEGACY { factors[], bands[] } shape, which nothing in this
 *                               product produces
 *   evaluateServerScorecard  -> { base_score, max_score, bins[] }, the shape the server stores
 *                               and the one Python, Rust, Kotlin and Dart all implement
 *
 * A JavaScript consumer handing a real scorecard to the obvious name used to get
 * `TypeError: Cannot read properties of undefined (reading 'reduce')`, which names neither the
 * cause nor the fix. TypeScript consumers were already protected -- `factors` is required, so
 * the compiler rejects it -- which is exactly why this went unnoticed.
 */
import { describe, it, expect } from "vitest";

import { evaluateScorecard, evaluateServerScorecard } from "../packages/rule-engine/src/index";

// The shape the API returns, trimmed to what both functions look at.
const SERVER_SCORECARD = {
  base_score: 300,
  max_score: 900,
  scoring_method: "points",
  bins: [{ variable_id: "bureau_score", ranges: [{ min: 700, max: 900, points: 250 }] }],
};

describe("evaluateScorecard on a server scorecard", () => {
  it("names the right function instead of dying inside a reduce", () => {
    expect(() => evaluateScorecard(SERVER_SCORECARD as never, { bureau_score: 780 }))
      .toThrowError(/evaluateServerScorecard/);
  });

  it("says the same for a scorecard that is neither shape", () => {
    expect(() => evaluateScorecard({} as never, {}))
      .toThrowError(/evaluateServerScorecard/);
  });

  it("still evaluates the legacy shape it is for", () => {
    const legacy = {
      factors: [{ name: "income", field: "income", operator: ">=", value: 50000, score: 40 }],
      bands: [{ min: 0, max: 100, label: "ok" }],
    };
    expect(evaluateScorecard(legacy as never, { income: 60000 }).score).toBe(40);
  });
});

describe("evaluateServerScorecard is the one that handles real data", () => {
  it("scores the same scorecard the legacy function rejects", () => {
    const result = evaluateServerScorecard(SERVER_SCORECARD, { bureau_score: 780 });
    expect(result.score).toBe(550); // base 300 + 250 points
    expect(result.base_score).toBe(300);
  });
});
