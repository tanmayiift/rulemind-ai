/**
 * MECE (Mutually Exclusive, Collectively Exhaustive) analysis for policy rules.
 *
 * Given a set of rules that belong to a policy, this module detects:
 *   - **Overlaps** (not mutually exclusive): two rules whose condition spaces intersect
 *   - **Gaps** (not collectively exhaustive): input regions covered by no rule
 *
 * Industry-agnostic: works with any decision domain — lending, insurance, fraud,
 * healthcare, supply-chain, compliance, HR, etc. The analysis operates purely on
 * field types and operators, never assuming domain semantics.
 *
 * The analysis works by extracting each rule's condition tree into a set of
 * field-level constraints (intervals for numerics/dates, value-sets for
 * categoricals/booleans), then checking pairwise overlap and union coverage.
 */

import type { FieldType, Operator, OutcomeType, RuleDefinition, RuleNode } from "@rulemind/shared";

// ---------------------------------------------------------------------------
// Public types
// ---------------------------------------------------------------------------

export interface Interval {
  lower: number;
  upper: number;
  lowerInclusive: boolean;
  upperInclusive: boolean;
}

export interface ValueSet {
  /** null means "all values" (universal set) */
  included: Set<string> | null;
  excluded: Set<string>;
}

export interface FieldConstraint {
  field: string;
  fieldType: FieldType;
  intervals?: Interval[];
  valueSet?: ValueSet;
  /** For != operator: specific excluded numeric points */
  excludedPoints?: number[];
  /** Whether this constraint is derived from an opaque operator (regex, etc.) */
  opaque?: boolean;
}

export interface RuleConstraintSpace {
  ruleId: string;
  ruleName: string;
  /** Each entry is one conjunctive branch (AND of FieldConstraints). Multiple
   *  entries arise from OR expansion. */
  branches: FieldConstraint[][];
  outcome?: OutcomeType;
}

export type MECEDiagnosticSeverity = "error" | "warning" | "info";

export interface MECEDiagnostic {
  type: "overlap" | "gap" | "warning";
  severity: MECEDiagnosticSeverity;
  fields: string[];
  description: string;
  involvedRules: string[];
  /** Node IDs involved — for UI highlighting */
  involvedNodeIds?: string[];
}

export interface MECEAnalysisResult {
  isMutuallyExclusive: boolean;
  isCollectivelyExhaustive: boolean;
  diagnostics: MECEDiagnostic[];
  analyzedFields: string[];
  ruleCount: number;
  /** True if any rules use opaque operators (regex, etc.) that prevent exact analysis */
  hasOpaqueConstraints: boolean;
  /** Warnings about analysis limitations */
  warnings: string[];
}

export interface MECERuleInput {
  id: string;
  name: string;
  definition: RuleDefinition;
  outcome?: OutcomeType;
}

// ---------------------------------------------------------------------------
// Interval helpers
// ---------------------------------------------------------------------------

const NEG_INF = -Infinity;
const POS_INF = Infinity;

function fullInterval(): Interval {
  return { lower: NEG_INF, upper: POS_INF, lowerInclusive: false, upperInclusive: false };
}

function emptyInterval(): Interval {
  return { lower: 0, upper: 0, lowerInclusive: false, upperInclusive: false };
}

function isEmptyInterval(iv: Interval): boolean {
  if (iv.lower > iv.upper) return true;
  if (iv.lower === iv.upper && !(iv.lowerInclusive && iv.upperInclusive)) return true;
  return false;
}

function intervalsOverlap(a: Interval, b: Interval): boolean {
  if (isEmptyInterval(a) || isEmptyInterval(b)) return false;
  if (a.upper < b.lower || b.upper < a.lower) return false;
  if (a.upper === b.lower) return a.upperInclusive && b.lowerInclusive;
  if (b.upper === a.lower) return b.upperInclusive && a.lowerInclusive;
  return true;
}

function intervalIntersection(a: Interval, b: Interval): Interval | null {
  if (isEmptyInterval(a) || isEmptyInterval(b)) return null;

  const lower = Math.max(a.lower, b.lower);
  const upper = Math.min(a.upper, b.upper);

  let lowerInclusive: boolean;
  if (a.lower === b.lower) lowerInclusive = a.lowerInclusive && b.lowerInclusive;
  else if (a.lower > b.lower) lowerInclusive = a.lowerInclusive;
  else lowerInclusive = b.lowerInclusive;

  let upperInclusive: boolean;
  if (a.upper === b.upper) upperInclusive = a.upperInclusive && b.upperInclusive;
  else if (a.upper < b.upper) upperInclusive = a.upperInclusive;
  else upperInclusive = b.upperInclusive;

  if (lower > upper) return null;
  if (lower === upper && !(lowerInclusive && upperInclusive)) return null;
  return { lower, upper, lowerInclusive, upperInclusive };
}

function operatorToInterval(op: Operator, value: number, value2?: number): Interval {
  // Guard against NaN values — treat as empty interval (no match)
  if (Number.isNaN(value)) return emptyInterval();
  if (value2 !== undefined && Number.isNaN(value2)) return emptyInterval();

  switch (op) {
    case "==":
      return { lower: value, upper: value, lowerInclusive: true, upperInclusive: true };
    case "!=":
      // != is handled via excludedPoints, but we still return full interval
      // as the "covered" range, with the point removed during overlap checks
      return fullInterval();
    case ">":
      return { lower: value, upper: POS_INF, lowerInclusive: false, upperInclusive: false };
    case ">=":
      return { lower: value, upper: POS_INF, lowerInclusive: true, upperInclusive: false };
    case "<":
      return { lower: NEG_INF, upper: value, lowerInclusive: false, upperInclusive: false };
    case "<=":
      return { lower: NEG_INF, upper: value, lowerInclusive: false, upperInclusive: true };
    case "between": {
      const lo = Math.min(value, value2 ?? value);
      const hi = Math.max(value, value2 ?? value);
      return { lower: lo, upper: hi, lowerInclusive: true, upperInclusive: true };
    }
    default:
      return fullInterval();
  }
}

function invertInterval(iv: Interval): Interval[] {
  if (isEmptyInterval(iv)) return [fullInterval()];

  const result: Interval[] = [];
  if (iv.lower > NEG_INF || (iv.lower === NEG_INF && iv.lowerInclusive)) {
    if (iv.lower > NEG_INF) {
      result.push({ lower: NEG_INF, upper: iv.lower, lowerInclusive: false, upperInclusive: !iv.lowerInclusive });
    }
  }
  if (iv.upper < POS_INF || (iv.upper === POS_INF && iv.upperInclusive)) {
    if (iv.upper < POS_INF) {
      result.push({ lower: iv.upper, upper: POS_INF, lowerInclusive: !iv.upperInclusive, upperInclusive: false });
    }
  }
  if (result.length === 0 && iv.lower === NEG_INF && iv.upper === POS_INF) {
    return [];
  }
  return result;
}

function formatInterval(iv: Interval): string {
  const lo = iv.lower === NEG_INF ? "-∞" : String(iv.lower);
  const hi = iv.upper === POS_INF ? "+∞" : String(iv.upper);
  const lb = iv.lowerInclusive ? "[" : "(";
  const rb = iv.upperInclusive ? "]" : ")";
  return `${lb}${lo}, ${hi}${rb}`;
}

// ---------------------------------------------------------------------------
// ValueSet helpers
// ---------------------------------------------------------------------------

function universalValueSet(): ValueSet {
  return { included: null, excluded: new Set() };
}

function emptyValueSet(): ValueSet {
  return { included: new Set(), excluded: new Set() };
}

function isEmptyValueSet(vs: ValueSet): boolean {
  return vs.included !== null && vs.included.size === 0;
}

function valueSetsOverlap(a: ValueSet, b: ValueSet): boolean {
  if (isEmptyValueSet(a) || isEmptyValueSet(b)) return false;

  // Both universal with exclusions
  if (a.included === null && b.included === null) {
    // Only non-overlapping if exclusion sets form complement — which we can't determine
    // for open domains, so conservatively return true
    return true;
  }
  if (a.included === null) {
    // a is universal minus excluded; b has finite inclusion
    for (const v of b.included!) {
      if (!a.excluded.has(v)) return true;
    }
    return false;
  }
  if (b.included === null) {
    for (const v of a.included) {
      if (!b.excluded.has(v)) return true;
    }
    return false;
  }
  // Both finite — check intersection
  for (const v of a.included) {
    if (b.included.has(v)) return true;
  }
  return false;
}

function valueSetsIntersection(a: ValueSet, b: ValueSet): ValueSet {
  if (isEmptyValueSet(a) || isEmptyValueSet(b)) return emptyValueSet();

  if (a.included === null && b.included === null) {
    return { included: null, excluded: new Set([...a.excluded, ...b.excluded]) };
  }
  if (a.included === null) {
    const inc = new Set<string>();
    for (const v of b.included!) {
      if (!a.excluded.has(v)) inc.add(v);
    }
    return { included: inc, excluded: new Set() };
  }
  if (b.included === null) {
    const inc = new Set<string>();
    for (const v of a.included) {
      if (!b.excluded.has(v)) inc.add(v);
    }
    return { included: inc, excluded: new Set() };
  }
  const inc = new Set<string>();
  for (const v of a.included) {
    if (b.included.has(v)) inc.add(v);
  }
  return { included: inc, excluded: new Set() };
}

function invertValueSet(vs: ValueSet): ValueSet {
  if (vs.included === null) {
    // Universal minus excluded → return just the excluded as included
    return { included: new Set(vs.excluded), excluded: new Set() };
  }
  // Finite set → universal minus those values
  return { included: null, excluded: new Set(vs.included) };
}

// ---------------------------------------------------------------------------
// Constraint extraction from rule definition
// ---------------------------------------------------------------------------

const BRANCH_CAP = 200;

/**
 * Try to parse a date-like value to a numeric timestamp for interval comparison.
 */
function parseDateValue(val: unknown): number {
  if (typeof val === "number") return val;
  const s = String(val ?? "");
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? NaN : d.getTime();
}

function extractConditionConstraint(node: RuleNode): FieldConstraint | null {
  const config = node.config ?? {};
  const field = config.field as string | undefined;
  const fieldType = (config.fieldType as FieldType) ?? "string";
  const operator = (config.operator as Operator) ?? "==";

  if (!field) return null;

  // Numeric → intervals
  if (fieldType === "number" || node.type === "score") {
    const value = Number(config.value ?? 0);
    const value2 = config.value2 !== undefined ? Number(config.value2) : undefined;

    if (operator === "!=") {
      // != on numeric: full interval with excluded point
      return { field, fieldType, intervals: [fullInterval()], excludedPoints: [value] };
    }
    const interval = operatorToInterval(operator, value, value2);
    return { field, fieldType, intervals: [interval] };
  }

  // Date → convert to numeric timestamps for interval comparison
  if (fieldType === "date") {
    const value = parseDateValue(config.value);
    const value2 = config.value2 !== undefined ? parseDateValue(config.value2) : undefined;

    if (operator === "!=") {
      return { field, fieldType, intervals: [fullInterval()], excludedPoints: Number.isNaN(value) ? [] : [value] };
    }
    if (Number.isNaN(value)) {
      return { field, fieldType, intervals: [fullInterval()], opaque: true };
    }
    const interval = operatorToInterval(operator, value, value2);
    return { field, fieldType, intervals: [interval] };
  }

  // Boolean → value set (domain is exactly {true, false})
  if (fieldType === "boolean") {
    const boolVal = String(config.value ?? "true").toLowerCase() === "true";
    if (operator === "==") {
      return { field, fieldType, valueSet: { included: new Set([String(boolVal)]), excluded: new Set() } };
    }
    if (operator === "!=") {
      return { field, fieldType, valueSet: { included: new Set([String(!boolVal)]), excluded: new Set() } };
    }
    if (operator === "exists") {
      return { field, fieldType, valueSet: { included: new Set(["true", "false"]), excluded: new Set() } };
    }
    if (operator === "!exists") {
      return { field, fieldType, valueSet: emptyValueSet() };
    }
    return { field, fieldType, valueSet: { included: new Set(["true", "false"]), excluded: new Set() } };
  }

  // String / enum
  if (operator === "in") {
    const values = String(config.value ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    return { field, fieldType, valueSet: { included: new Set(values), excluded: new Set() } };
  }
  if (operator === "not_in") {
    const values = String(config.value ?? "").split(",").map((s) => s.trim()).filter(Boolean);
    return { field, fieldType, valueSet: { included: null, excluded: new Set(values) } };
  }
  if (operator === "==" && config.value !== undefined) {
    return { field, fieldType, valueSet: { included: new Set([String(config.value)]), excluded: new Set() } };
  }
  if (operator === "!=" && config.value !== undefined) {
    return { field, fieldType, valueSet: { included: null, excluded: new Set([String(config.value)]) } };
  }
  if (operator === "exists") {
    return { field, fieldType, valueSet: universalValueSet() };
  }
  if (operator === "!exists") {
    return { field, fieldType, valueSet: emptyValueSet() };
  }
  if (operator === "regex") {
    // Regex constraints are opaque — we can't decompose them into intervals/value sets
    return { field, fieldType, valueSet: universalValueSet(), opaque: true };
  }

  // Fallback: unknown operator — mark as opaque
  return { field, fieldType, valueSet: universalValueSet(), opaque: true };
}

/**
 * Walk a rule's node tree and return an array of conjunctive branches.
 * Each branch is an AND-combined list of field constraints.
 * OR nodes expand into multiple branches (cartesian product capped at BRANCH_CAP).
 */
export function extractRuleConstraintSpace(definition: RuleDefinition): FieldConstraint[][] {
  if (!definition.nodes || definition.nodes.length === 0) return [[]];

  const nodeMap = new Map<string, RuleNode>();
  for (const n of definition.nodes) nodeMap.set(n.id, n);

  // Build adjacency (parent → children)
  const children = new Map<string, string[]>();
  for (const c of (definition.connections ?? [])) {
    const list = children.get(c.from) ?? [];
    list.push(c.to);
    children.set(c.from, list);
  }

  const outcomeTypes = new Set(["approve", "reject", "review"]);

  function isOutcomeNode(id: string): boolean {
    const n = nodeMap.get(id);
    return n ? outcomeTypes.has(n.type) : false;
  }

  // Find root nodes (no incoming connections, not outcome nodes)
  const hasIncoming = new Set((definition.connections ?? []).map((c) => c.to));
  const roots = definition.nodes.filter(
    (n) => !hasIncoming.has(n.id) && !outcomeTypes.has(n.type)
  );

  // Cycle detection to prevent infinite recursion
  const visiting = new Set<string>();

  function walk(nodeId: string): FieldConstraint[][] {
    if (visiting.has(nodeId)) return [[]]; // Break cycle
    visiting.add(nodeId);

    try {
      const node = nodeMap.get(nodeId);
      if (!node) return [[]];

      const childIds = (children.get(nodeId) ?? []).filter((id) => !isOutcomeNode(id));

      if (node.type === "condition" || node.type === "score") {
        const constraint = extractConditionConstraint(node);
        if (!constraint) return [[]];
        const childBranches = childIds.length > 0
          ? childIds.flatMap((id) => walk(id))
          : [[]];
        if (childBranches.length === 0) return [[constraint]];
        return childBranches.map((branch) => [constraint, ...branch]);
      }

      if (node.type === "and" || (node.type === "group" && (node.config?.groupOp ?? "AND") === "AND")) {
        return expandAndBranches(childIds);
      }

      if (node.type === "or" || (node.type === "group" && node.config?.groupOp === "OR")) {
        return expandOrBranches(childIds);
      }

      if (node.type === "not") {
        if (childIds.length === 0) return [[]];
        const childBranches = walk(childIds[0]);
        return invertBranches(childBranches);
      }

      if (node.type === "trigger") {
        if (childIds.length === 0) return [[]];
        return childIds.flatMap((id) => walk(id));
      }

      // Unknown node type — pass through
      return [[]];
    } finally {
      visiting.delete(nodeId);
    }
  }

  function expandAndBranches(childIds: string[]): FieldConstraint[][] {
    if (childIds.length === 0) return [[]];
    let result: FieldConstraint[][] = [[]];
    for (const cid of childIds) {
      const childBranches = walk(cid);
      if (childBranches.length === 0) continue;
      const newResult: FieldConstraint[][] = [];
      for (const existing of result) {
        for (const cb of childBranches) {
          if (newResult.length >= BRANCH_CAP) break;
          newResult.push([...existing, ...cb]);
        }
        if (newResult.length >= BRANCH_CAP) break;
      }
      result = newResult;
      if (result.length === 0) return [[]];
    }
    return result;
  }

  function expandOrBranches(childIds: string[]): FieldConstraint[][] {
    if (childIds.length === 0) return [[]];
    const branches: FieldConstraint[][] = [];
    for (const cid of childIds) {
      if (branches.length >= BRANCH_CAP) break;
      branches.push(...walk(cid));
    }
    return branches.length > 0 ? branches.slice(0, BRANCH_CAP) : [[]];
  }

  function invertBranches(branches: FieldConstraint[][]): FieldConstraint[][] {
    const inverted: FieldConstraint[][] = [];
    for (const branch of branches) {
      if (branch.length === 0) {
        // Empty branch = universal → NOT(universal) = empty (impossible)
        continue;
      }
      if (branch.length === 1) {
        const c = branch[0];
        if (c.opaque) {
          // Can't invert opaque constraints — mark as opaque
          inverted.push([{ ...c, opaque: true }]);
        } else if (c.intervals && !c.excludedPoints?.length) {
          const inv = invertInterval(c.intervals[0]);
          for (const ivn of inv) {
            inverted.push([{ ...c, intervals: [ivn] }]);
          }
        } else if (c.intervals && c.excludedPoints?.length) {
          // NOT(full interval minus point) = just the point
          for (const pt of c.excludedPoints) {
            inverted.push([{ ...c, intervals: [{ lower: pt, upper: pt, lowerInclusive: true, upperInclusive: true }], excludedPoints: undefined }]);
          }
        } else if (c.valueSet) {
          inverted.push([{ ...c, valueSet: invertValueSet(c.valueSet) }]);
        }
      } else {
        // Multi-constraint branch: NOT(A AND B AND ...) = NOT(A) OR NOT(B) OR ...
        // Apply De Morgan's law
        for (const c of branch) {
          const singleInverted = invertBranches([[c]]);
          inverted.push(...singleInverted);
        }
      }
    }
    return inverted.length > 0 ? inverted : [[]];
  }

  if (roots.length === 0) return [[]];

  const allBranches: FieldConstraint[][] = [];
  for (const root of roots) {
    allBranches.push(...walk(root.id));
  }
  return allBranches.length > 0 ? allBranches.slice(0, BRANCH_CAP) : [[]];
}

// ---------------------------------------------------------------------------
// Branch normalization (merge same-field AND constraints)
// ---------------------------------------------------------------------------

/**
 * Merge multiple constraints on the same field within a branch (AND = intersection).
 * This is critical for correct overlap detection when rules have conditions like
 * `score >= 500 AND score < 700` on the same field.
 */
function normalizeBranch(branch: FieldConstraint[]): Map<string, FieldConstraint> {
  const byField = new Map<string, FieldConstraint>();
  for (const c of branch) {
    const existing = byField.get(c.field);
    if (!existing) {
      // Deep clone to avoid mutation
      byField.set(c.field, {
        ...c,
        intervals: c.intervals ? [...c.intervals] : undefined,
        valueSet: c.valueSet
          ? { included: c.valueSet.included ? new Set(c.valueSet.included) : null, excluded: new Set(c.valueSet.excluded) }
          : undefined,
        excludedPoints: c.excludedPoints ? [...c.excludedPoints] : undefined,
        opaque: c.opaque || false,
      });
      continue;
    }

    // Propagate opaque flag
    if (c.opaque) existing.opaque = true;

    // Intersect intervals (AND of numeric ranges)
    if (existing.intervals && c.intervals) {
      const merged: Interval[] = [];
      for (const ei of existing.intervals) {
        for (const ci of c.intervals) {
          const inter = intervalIntersection(ei, ci);
          if (inter) merged.push(inter);
        }
      }
      existing.intervals = merged.length > 0 ? merged : [emptyInterval()];
    }

    // Merge excluded points
    if (c.excludedPoints) {
      existing.excludedPoints = [...(existing.excludedPoints ?? []), ...c.excludedPoints];
    }

    // Intersect value sets
    if (existing.valueSet && c.valueSet) {
      existing.valueSet = valueSetsIntersection(existing.valueSet, c.valueSet);
    }
  }
  return byField;
}

// ---------------------------------------------------------------------------
// Overlap checking
// ---------------------------------------------------------------------------

function constraintsOverlapForField(a: FieldConstraint, b: FieldConstraint): boolean {
  // If either is opaque, conservatively assume overlap
  if (a.opaque || b.opaque) return true;

  // Both have intervals (numeric/date)
  if (a.intervals && b.intervals) {
    for (const ia of a.intervals) {
      for (const ib of b.intervals) {
        if (isEmptyInterval(ia) || isEmptyInterval(ib)) continue;
        if (!intervalsOverlap(ia, ib)) continue;

        // Check excluded points — if overlap is just a single point that's excluded, skip
        const intersection = intervalIntersection(ia, ib);
        if (!intersection) continue;

        // If the intersection is a single point, check if it's excluded by either rule
        if (intersection.lower === intersection.upper && intersection.lowerInclusive && intersection.upperInclusive) {
          const pt = intersection.lower;
          if (a.excludedPoints?.includes(pt) || b.excludedPoints?.includes(pt)) continue;
        }

        return true;
      }
    }
    return false;
  }

  // Both have value sets (string/boolean/enum)
  if (a.valueSet && b.valueSet) {
    return valueSetsOverlap(a.valueSet, b.valueSet);
  }

  // Mixed types or missing — assume overlap (conservative)
  return true;
}

/**
 * Check if two conjunctive branches (AND of constraints) overlap.
 * They overlap if for every shared field, the normalized constraints overlap.
 */
function branchesOverlap(branchA: FieldConstraint[], branchB: FieldConstraint[]): boolean {
  const fieldsA = normalizeBranch(branchA);
  const fieldsB = normalizeBranch(branchB);

  const sharedFields = [...fieldsA.keys()].filter((f) => fieldsB.has(f));

  // If no shared fields, rules are unconstrained relative to each other → overlap
  if (sharedFields.length === 0 && fieldsA.size > 0 && fieldsB.size > 0) return true;
  if (sharedFields.length === 0) return true;

  for (const field of sharedFields) {
    if (!constraintsOverlapForField(fieldsA.get(field)!, fieldsB.get(field)!)) {
      return false;
    }
  }
  return true;
}

function describeOverlap(a: FieldConstraint, b: FieldConstraint): string {
  if (a.opaque || b.opaque) {
    return `${a.field} uses regex/opaque operators — overlap cannot be precisely determined`;
  }
  if (a.intervals && b.intervals) {
    for (const ia of a.intervals) {
      for (const ib of b.intervals) {
        const intersection = intervalIntersection(ia, ib);
        if (intersection) {
          return `${a.field} overlaps in range ${formatInterval(intersection)}`;
        }
      }
    }
  }
  if (a.valueSet && b.valueSet) {
    const inter = valueSetsIntersection(a.valueSet, b.valueSet);
    if (inter.included && inter.included.size > 0) {
      const vals = [...inter.included].slice(0, 10);
      const suffix = inter.included.size > 10 ? ` (and ${inter.included.size - 10} more)` : "";
      return `${a.field} overlaps on values: {${vals.join(", ")}}${suffix}`;
    }
    if (inter.included === null) {
      return `${a.field} overlaps on open domain`;
    }
  }
  return `${a.field} has overlapping conditions`;
}

/**
 * Collect all condition node IDs from a rule definition.
 */
function collectConditionNodeIds(definition: RuleDefinition): string[] {
  return definition.nodes
    .filter((n) => n.type === "condition" || n.type === "score")
    .map((n) => n.id);
}

/**
 * Check two rules for pairwise overlap across all their disjunctive branches.
 * Returns ALL overlap diagnostics found (not just the first).
 */
function checkRulePairOverlap(
  ruleA: { id: string; name: string; branches: FieldConstraint[][]; definition: RuleDefinition },
  ruleB: { id: string; name: string; branches: FieldConstraint[][]; definition: RuleDefinition }
): MECEDiagnostic[] {
  const result: MECEDiagnostic[] = [];
  const foundOverlapFields = new Set<string>();

  for (const ba of ruleA.branches) {
    for (const bb of ruleB.branches) {
      if (!branchesOverlap(ba, bb)) continue;

      const fieldsA = normalizeBranch(ba);
      const fieldsB = normalizeBranch(bb);
      const shared = [...fieldsA.keys()].filter((f) => fieldsB.has(f));

      const descriptions: string[] = [];
      const overlapFields: string[] = [];
      let hasOpaqueOverlap = false;

      for (const f of shared) {
        const ca = fieldsA.get(f)!;
        const cb = fieldsB.get(f)!;
        if (constraintsOverlapForField(ca, cb)) {
          if (!foundOverlapFields.has(f)) {
            overlapFields.push(f);
            foundOverlapFields.add(f);
          }
          descriptions.push(describeOverlap(ca, cb));
          if (ca.opaque || cb.opaque) hasOpaqueOverlap = true;
        }
      }

      const nodeIdsA = collectConditionNodeIds(ruleA.definition);
      const nodeIdsB = collectConditionNodeIds(ruleB.definition);

      if (shared.length === 0) {
        result.push({
          type: "overlap",
          severity: "error",
          fields: [],
          description: `Rules "${ruleA.name}" and "${ruleB.name}" have no shared condition fields — both can fire on any input`,
          involvedRules: [ruleA.id, ruleB.id],
          involvedNodeIds: [...nodeIdsA, ...nodeIdsB],
        });
      } else if (descriptions.length > 0) {
        result.push({
          type: "overlap",
          severity: hasOpaqueOverlap ? "warning" : "error",
          fields: overlapFields,
          description: `Rules "${ruleA.name}" and "${ruleB.name}" overlap: ${descriptions.join("; ")}`,
          involvedRules: [ruleA.id, ruleB.id],
          involvedNodeIds: [...nodeIdsA, ...nodeIdsB],
        });
      }

      // Only report first overlapping branch combination per rule pair
      return result;
    }
  }

  return result;
}

// ---------------------------------------------------------------------------
// Exhaustiveness checking
// ---------------------------------------------------------------------------

function checkNumericExhaustiveness(
  field: string,
  allIntervals: Interval[]
): MECEDiagnostic[] {
  if (allIntervals.length === 0) return [];

  // Filter out empty intervals
  const valid = allIntervals.filter((iv) => !isEmptyInterval(iv));
  if (valid.length === 0) {
    return [{
      type: "gap",
      severity: "error",
      fields: [field],
      description: `All rules for ${field} have empty/invalid conditions — entire domain is uncovered`,
      involvedRules: [],
    }];
  }

  // Sort by lower bound, then by inclusiveness (inclusive first)
  const sorted = [...valid].sort((a, b) => {
    if (a.lower !== b.lower) return a.lower - b.lower;
    return (a.lowerInclusive ? 0 : 1) - (b.lowerInclusive ? 0 : 1);
  });

  const gaps: MECEDiagnostic[] = [];

  // Check gap before first interval
  if (sorted[0].lower > NEG_INF) {
    const boundary = sorted[0].lower;
    const incl = sorted[0].lowerInclusive;
    gaps.push({
      type: "gap",
      severity: "error",
      fields: [field],
      description: `No rule covers ${field} ${incl ? "<" : "<="} ${boundary}`,
      involvedRules: [],
    });
  }

  // Merge intervals and find internal gaps
  let currentUpper = sorted[0].upper;
  let currentUpperInclusive = sorted[0].upperInclusive;

  for (let i = 1; i < sorted.length; i++) {
    const next = sorted[i];

    if (currentUpper < next.lower) {
      // Clear gap
      gaps.push({
        type: "gap",
        severity: "error",
        fields: [field],
        description: `No rule covers ${field} in (${currentUpper}, ${next.lower}) — gap between rules`,
        involvedRules: [],
      });
    } else if (currentUpper === next.lower && !currentUpperInclusive && !next.lowerInclusive) {
      // Boundary gap — single point uncovered
      gaps.push({
        type: "gap",
        severity: "error",
        fields: [field],
        description: `No rule covers ${field} = ${currentUpper} — boundary point gap`,
        involvedRules: [],
      });
    }

    // Advance the "water mark"
    if (next.upper > currentUpper || (next.upper === currentUpper && next.upperInclusive && !currentUpperInclusive)) {
      currentUpper = next.upper;
      currentUpperInclusive = next.upperInclusive;
    }
  }

  // Check gap after last interval
  if (currentUpper < POS_INF) {
    const boundary = currentUpper;
    const incl = currentUpperInclusive;
    gaps.push({
      type: "gap",
      severity: "error",
      fields: [field],
      description: `No rule covers ${field} ${incl ? ">" : ">="} ${boundary}`,
      involvedRules: [],
    });
  }

  return gaps;
}

/**
 * Check boolean field exhaustiveness — domain is exactly {true, false}.
 */
function checkBooleanExhaustiveness(
  field: string,
  allValues: Set<string>
): MECEDiagnostic[] {
  const gaps: MECEDiagnostic[] = [];
  if (!allValues.has("true")) {
    gaps.push({
      type: "gap",
      severity: "error",
      fields: [field],
      description: `No rule covers ${field} = true`,
      involvedRules: [],
    });
  }
  if (!allValues.has("false")) {
    gaps.push({
      type: "gap",
      severity: "error",
      fields: [field],
      description: `No rule covers ${field} = false`,
      involvedRules: [],
    });
  }
  return gaps;
}

// ---------------------------------------------------------------------------
// Main entry point
// ---------------------------------------------------------------------------

/**
 * Analyze a set of rules for MECE properties.
 *
 * This function is industry-agnostic — it works with any decision domain
 * (lending, insurance, fraud, healthcare, supply-chain, compliance, HR, etc.)
 * by operating purely on field types and operators.
 *
 * @param rules - Array of rules to analyze (each with id, name, definition, optional outcome)
 * @returns Comprehensive analysis result with diagnostics, warnings, and field coverage info
 */
export function analyzeMECE(rules: MECERuleInput[]): MECEAnalysisResult {
  const warnings: string[] = [];

  if (rules.length === 0) {
    return {
      isMutuallyExclusive: true,
      isCollectivelyExhaustive: false,
      diagnostics: [{
        type: "gap",
        severity: "error",
        fields: [],
        description: "Policy has no rules — no inputs are covered",
        involvedRules: [],
      }],
      analyzedFields: [],
      ruleCount: 0,
      hasOpaqueConstraints: false,
      warnings: ["No rules to analyze"],
    };
  }

  if (rules.length === 1) {
    const branches = extractRuleConstraintSpace(rules[0].definition);
    const allFields = new Set<string>();
    let hasOpaque = false;
    for (const branch of branches) {
      for (const c of branch) {
        allFields.add(c.field);
        if (c.opaque) hasOpaque = true;
      }
    }
    return {
      isMutuallyExclusive: true,
      isCollectivelyExhaustive: false,
      diagnostics: [{
        type: "gap",
        severity: "info",
        fields: [...allFields],
        description: "Single rule cannot be collectively exhaustive — add complementary rules to cover all inputs",
        involvedRules: [rules[0].id],
      }],
      analyzedFields: [...allFields],
      ruleCount: 1,
      hasOpaqueConstraints: hasOpaque,
      warnings: hasOpaque ? ["Some conditions use regex or opaque operators — analysis may be imprecise"] : [],
    };
  }

  // Extract constraint spaces for all rules
  const ruleSpaces: Array<{
    id: string;
    name: string;
    branches: FieldConstraint[][];
    outcome?: OutcomeType;
    definition: RuleDefinition;
  }> = [];

  for (const rule of rules) {
    const branches = extractRuleConstraintSpace(rule.definition);
    ruleSpaces.push({ id: rule.id, name: rule.name, branches, outcome: rule.outcome, definition: rule.definition });
  }

  const diagnostics: MECEDiagnostic[] = [];
  let hasOpaqueConstraints = false;

  // Check for branch cap warnings
  for (const space of ruleSpaces) {
    if (space.branches.length >= BRANCH_CAP) {
      warnings.push(`Rule "${space.name}" has ≥${BRANCH_CAP} OR-branches — analysis is approximate`);
      diagnostics.push({
        type: "warning",
        severity: "warning",
        fields: [],
        description: `Rule "${space.name}" has complex OR structure (≥${BRANCH_CAP} branches) — analysis may miss some overlaps or gaps`,
        involvedRules: [space.id],
      });
    }
  }

  // Pairwise overlap check
  for (let i = 0; i < ruleSpaces.length; i++) {
    for (let j = i + 1; j < ruleSpaces.length; j++) {
      const overlapDiags = checkRulePairOverlap(ruleSpaces[i], ruleSpaces[j]);
      diagnostics.push(...overlapDiags);
    }
  }

  // Collect all fields and their constraints across all rules (normalized)
  const numericFieldIntervals = new Map<string, Interval[]>();
  const booleanFieldValues = new Map<string, Set<string>>();
  const categoricalFieldValues = new Map<string, { values: Set<string>; hasUniversal: boolean }>();
  const allFields = new Set<string>();

  for (const space of ruleSpaces) {
    for (const branch of space.branches) {
      const normalized = normalizeBranch(branch);
      for (const [, constraint] of normalized) {
        allFields.add(constraint.field);
        if (constraint.opaque) hasOpaqueConstraints = true;

        if (constraint.intervals) {
          const existing = numericFieldIntervals.get(constraint.field) ?? [];
          existing.push(...constraint.intervals);
          numericFieldIntervals.set(constraint.field, existing);
        }

        if (constraint.valueSet) {
          if (constraint.fieldType === "boolean") {
            const existing = booleanFieldValues.get(constraint.field) ?? new Set();
            if (constraint.valueSet.included) {
              for (const v of constraint.valueSet.included) existing.add(v);
            } else {
              // Universal boolean covers both
              existing.add("true");
              existing.add("false");
            }
            booleanFieldValues.set(constraint.field, existing);
          } else {
            const existing = categoricalFieldValues.get(constraint.field) ?? { values: new Set(), hasUniversal: false };
            if (constraint.valueSet.included === null) {
              existing.hasUniversal = true;
            } else {
              for (const v of constraint.valueSet.included) existing.values.add(v);
            }
            categoricalFieldValues.set(constraint.field, existing);
          }
        }
      }
    }
  }

  // Exhaustiveness check for numeric fields
  for (const [field, intervals] of numericFieldIntervals) {
    const gapDiags = checkNumericExhaustiveness(field, intervals);
    diagnostics.push(...gapDiags);
  }

  // Exhaustiveness check for boolean fields
  for (const [field, values] of booleanFieldValues) {
    const gapDiags = checkBooleanExhaustiveness(field, values);
    diagnostics.push(...gapDiags);
  }

  // Exhaustiveness warnings for categorical fields with only finite value sets
  for (const [field, info] of categoricalFieldValues) {
    if (!info.hasUniversal && info.values.size > 0) {
      warnings.push(
        `Field "${field}" only covers specific values {${[...info.values].slice(0, 5).join(", ")}${info.values.size > 5 ? "..." : ""}} — ` +
        `inputs with other values won't match any rule`
      );
      diagnostics.push({
        type: "gap",
        severity: "warning",
        fields: [field],
        description: `Field "${field}" only covers ${info.values.size} specific value(s) — inputs with other values won't match any rule. Consider adding a catch-all rule.`,
        involvedRules: [],
      });
    }
  }

  if (hasOpaqueConstraints) {
    warnings.push("Some conditions use regex or opaque operators — MECE analysis may be imprecise for those fields");
  }

  const isMutuallyExclusive = !diagnostics.some((d) => d.type === "overlap" && d.severity === "error");
  const isCollectivelyExhaustive = !diagnostics.some((d) => d.type === "gap" && d.severity === "error");

  return {
    isMutuallyExclusive,
    isCollectivelyExhaustive,
    diagnostics,
    analyzedFields: [...allFields],
    ruleCount: rules.length,
    hasOpaqueConstraints,
    warnings,
  };
}
