/**
 * Evaluate a scorecard in the shape the SERVER actually stores.
 *
 * WHY THIS EXISTS. `decisioning.ts::evaluateScorecard` implements a different data model from
 * the rest of the product:
 *
 *   this engine (legacy) : { factors: [{ name, field, operator, value, score }], bands: [...] }
 *   everything else      : { base_score, max_score, bins: [{ variable_id, ranges: [...] }] }
 *
 * Python (`app/logic.py::evaluate_scorecard`, the declared source of truth), the Rust core, and
 * the Kotlin and Dart SDKs all use the second shape. So the exported `evaluateScorecard` cannot
 * evaluate a scorecard this product persists — `definition.factors` is undefined and `.reduce`
 * throws. It has no consumer inside this repo, which is why nobody noticed, but it is public API
 * of `@rulemind/rule-engine`.
 *
 * This module is the missing arm. It is validated against `packages/shared/scorecards.spec.json`
 * — the same 166-case corpus Python generated and the Kotlin and Dart engines now pass — rather
 * than against hand-written expectations. That distinction matters: `cross-platform-parity.test.ts`
 * is named for parity but only ever compared TypeScript to numbers a human typed, which is why
 * ten separate divergences from Python survived in it.
 *
 * The subtleties below are all places a from-scratch port guesses differently. Each is here
 * because Python does it, not because it is obviously right.
 */

import { EXCEL_FUNCTIONS } from "./excel-functions";
import { evaluateFormulaSafely } from "./safe-formula";

export interface ServerScoreRange {
  min?: unknown;
  max?: unknown;
  points?: unknown;
  woe?: unknown;
  iv?: unknown;
  event_rate?: unknown;
  non_event_rate?: unknown;
  [k: string]: unknown;
}

export interface ServerScoreBin {
  variable_id?: string;
  weight?: unknown;
  coefficient?: unknown;
  ranges?: ServerScoreRange[];
  woe_values?: ServerScoreRange[];
  [k: string]: unknown;
}

export interface ServerScorecard {
  base_score?: unknown;
  max_score?: unknown;
  scoring_method?: string;
  bins?: ServerScoreBin[];
  intercept?: unknown;
  pdo?: unknown;
  target_score?: unknown;
  target_odds?: unknown;
  formula?: string;
  metrics?: Array<{ name?: string; formula?: string; unit?: string; category?: string }>;
  [k: string]: unknown;
}

export interface ServerScorecardResult {
  score: number;
  base_score: number;
  max_score: number;
  scoring_method: string;
  breakdown: Array<Record<string, unknown>>;
  woe_details?: Array<Record<string, unknown>>;
  information_value?: number;
  metrics?: Record<string, unknown>;
}

/** Python `int(x)` truncates toward zero. JS `Math.trunc` matches; `Math.floor` does NOT for
 *  negatives, and getting this wrong flips the sign of a negative weighted factor. */
function pyInt(value: number): number {
  return Math.trunc(value);
}

/** Python `round()` is banker's rounding — half to EVEN. 600.5 -> 600, 601.5 -> 602.
 *  JS `Math.round` is half-up (601, 602) and Dart's `.round()` is half-away-from-zero. This is
 *  reachable in the WoE path whenever the arithmetic lands exactly on .5. */
function pyRound(value: number): number {
  const floor = Math.floor(value);
  const diff = value - floor;
  if (diff > 0.5) return floor + 1;
  if (diff < 0.5) return floor;
  return floor % 2 === 0 ? floor : floor + 1;
}

/** Python `float(x)`: raises for None, "", "abc", lists. Returning null models the raise, and the
 *  caller then SKIPS that range — it does not treat the value as 0. That difference is load
 *  bearing: a missing variable defaults to 0 and can therefore match a [0, n] bin and earn
 *  points, while an explicit null matches nothing at all. */
function pyFloat(value: unknown): number | null {
  if (typeof value === "boolean") return value ? 1 : 0;
  if (typeof value === "number") return Number.isFinite(value) ? value : null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (trimmed === "") return null;
    const n = Number(trimmed);
    return Number.isFinite(n) ? n : null;
  }
  return null;
}

/**
 * Match a value against a range list, first match wins.
 *
 * Two Python behaviours that a port reliably gets wrong:
 *  - bounds are CLOSED on both ends (`min <= v <= max`), so with bins [0,500] and [500,700] the
 *    value 500 takes the FIRST. The legacy TS WoE scan used `v >= min && v < max` and took the
 *    second.
 *  - an ABSENT bound defaults to 0, not to -/+ infinity: `float(candidate.get("min", 0))`. So a
 *    range `{ "points": 50 }` matches only the value 0. Kotlin and Dart originally substituted
 *    infinities and matched everything.
 *  A range whose bound is present but unparseable (null, "abc") raises inside Python's try and
 *  is SKIPPED — not treated as 0.
 */
function findRange(ranges: ServerScoreRange[] | undefined, value: unknown): ServerScoreRange | null {
  const v = pyFloat(value);
  if (v === null) return null; // float(value) itself raised; no range can match
  for (const candidate of ranges ?? []) {
    const lo = "min" in candidate ? pyFloat(candidate.min) : 0;
    const hi = "max" in candidate ? pyFloat(candidate.max) : 0;
    if (lo === null || hi === null) continue; // present-but-unparseable: Python's except: continue
    if (lo <= v && v <= hi) return candidate;
  }
  return null;
}

export function evaluateServerScorecard(
  scorecard: ServerScorecard,
  variableValues: Record<string, unknown>,
  variableLookup: Record<string, { name?: string; source_id?: string }> = {}
): ServerScorecardResult {
  const baseScore = pyInt(pyFloat(scorecard.base_score) ?? 300);
  const maxScore = pyInt(pyFloat(scorecard.max_score) ?? 900);
  const scoringMethod = scorecard.scoring_method ?? "points";

  const breakdown: Array<Record<string, unknown>> = [];
  const woeDetails: Array<Record<string, unknown>> = [];
  let totalPoints = 0;
  let totalWoeScore = 0;
  let totalIv = 0;

  for (const bin of scorecard.bins ?? []) {
    const variableId = bin.variable_id;
    // Python: `variable_values.get(variable_id, 0)` — a MISSING variable is 0, which can match a
    // [0, n] range and earn points. An explicit null is a different thing entirely (see pyFloat).
    const value = variableId !== undefined && variableId in variableValues ? variableValues[variableId] : 0;
    const weight = pyFloat(bin.weight) ?? 1.0;
    const coefficient = pyFloat(bin.coefficient) ?? 0.0;

    const activeRange = findRange(bin.ranges, value);
    const activeWoeRange = findRange(bin.woe_values, value);

    const points = activeRange ? pyInt(pyFloat(activeRange.points) ?? 0) : 0;
    // Truncated PER FACTOR, then summed. Summing in float and truncating once at the end gives a
    // different answer: three factors at 0.5 x 3 are 3, not 4.
    const weightedPoints = pyInt(points * weight);
    totalPoints += weightedPoints;

    const woeValue = activeWoeRange ? pyFloat(activeWoeRange.woe) ?? 0 : 0;
    const ivContribution = activeWoeRange ? pyFloat(activeWoeRange.iv) ?? 0 : 0;
    totalIv += ivContribution;
    totalWoeScore += coefficient * woeValue;

    if (activeWoeRange) {
      // Note this runs for EVERY scoring method, not just "woe" — a points scorecard carrying
      // woe_values still reports woe_details and information_value. Conversely a woe scorecard
      // where nothing matched reports neither key.
      woeDetails.push({
        variable_id: variableId,
        variable_name: variableLookup[variableId ?? ""]?.name ?? variableId,
        value,
        woe: woeValue,
        iv: ivContribution,
        coefficient,
        contribution: coefficient * woeValue,
        event_rate: activeWoeRange.event_rate,
        non_event_rate: activeWoeRange.non_event_rate,
      });
    }

    breakdown.push({
      variable_id: variableId,
      variable_name: variableLookup[variableId ?? ""]?.name ?? variableId,
      source_id: variableLookup[variableId ?? ""]?.source_id,
      value,
      active_range: activeRange,
      points,
      weighted_points: weightedPoints,
      weight,
      woe: woeValue,
      coefficient,
    });
  }

  let score: number;
  if (scoringMethod === "woe") {
    const intercept = pyFloat(scorecard.intercept) ?? 0;
    const pdo = pyFloat(scorecard.pdo) ?? 20;
    const targetScore = pyFloat(scorecard.target_score) ?? 600;
    const targetOdds = pyFloat(scorecard.target_odds) ?? 50;
    // pdo <= 0 makes the factor exactly 1 rather than pdo/ln(2), and target_odds <= 0 drops the
    // log term entirely. The legacy TS path computed Math.log(0) = -Infinity here.
    const factorVal = pdo > 0 ? pdo / Math.log(2) : 1;
    const offset = targetOdds > 0 ? targetScore - factorVal * Math.log(targetOdds) : targetScore;
    score = Math.max(0, Math.min(maxScore, pyRound(offset + factorVal * (intercept + totalWoeScore))));
  } else if (scoringMethod === "formula") {
    const formula = scorecard.formula ?? "base_score + total_points";
    const context: Record<string, unknown> = {
      base_score: baseScore,
      max_score: maxScore,
      total_points: totalPoints,
      total_woe_score: totalWoeScore,
      variables: variableValues,
    };
    for (const entry of breakdown) context[String(entry.variable_id)] = entry.value;
    let raw: unknown;
    try {
      raw = evaluateFormulaSafely(formula, context, EXCEL_FUNCTIONS as never);
    } catch {
      raw = null; // a bad formula falls back to base_score, it does not throw and does not give 0
    }
    // `bool` is numeric in Python: `total_points > 0` scores 1, not a fallback.
    const numeric = typeof raw === "boolean" ? (raw ? 1 : 0) : typeof raw === "number" ? raw : null;
    score = Math.max(0, Math.min(maxScore, numeric === null ? baseScore : pyInt(numeric)));
  } else {
    score = Math.max(0, Math.min(maxScore, baseScore + totalPoints));
  }

  const result: ServerScorecardResult = {
    score,
    base_score: baseScore,
    max_score: maxScore,
    scoring_method: scoringMethod,
    breakdown,
  };

  if (woeDetails.length) {
    result.woe_details = woeDetails;
    result.information_value = totalIv;
  }

  const metrics = scorecard.metrics ?? [];
  if (metrics.length) {
    const out: Record<string, unknown> = {};
    for (const metric of metrics) {
      // The metric context binds `score` and `total_iv`; the FORMULA context above binds neither.
      // They are deliberately different scopes in Python, so a formula that works as a metric may
      // not work as the score formula.
      const context: Record<string, unknown> = {
        score,
        base_score: baseScore,
        max_score: maxScore,
        total_points: totalPoints,
        total_woe_score: totalWoeScore,
        total_iv: totalIv,
        variables: variableValues,
      };
      for (const entry of breakdown) context[String(entry.variable_id)] = entry.value;
      let value: unknown;
      try {
        value = evaluateFormulaSafely(metric.formula ?? "0", context, EXCEL_FUNCTIONS as never);
      } catch (err) {
        value = { error: String(err instanceof Error ? err.message : err) };
      }
      out[metric.name ?? "unnamed"] = {
        value,
        unit: metric.unit ?? "",
        category: metric.category ?? "financial",
      };
    }
    result.metrics = out;
  }

  return result;
}
