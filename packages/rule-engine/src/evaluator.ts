import type {
  ConditionResult,
  EvaluationResult,
  ExplanationNode,
  OutcomeDecision,
  OutcomeType,
  RuleNode
} from "@rulemind/shared";
import { compileRule, type CompiledRule } from "./compiler";

function createTraceId() {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }

  return `trace_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
}

interface EvaluationContext {
  version?: number;
  traceId?: string;
  variableResults?: Record<string, unknown>;
  event?: string;
}

interface NodeEvaluation {
  pass: boolean;
  outcomes: OutcomeDecision[];
  explanation: ExplanationNode;
}

const outcomeSeverity: OutcomeType[] = ["reject", "review", "approve"];

function toNumber(value: unknown): number {
  if (typeof value === "number") {
    return value;
  }

  return Number(value);
}

/**
 * Numeric coercion that returns null for anything non-numeric — the exact
 * mirror of Python `_coerce_number`, Kotlin/Dart `numericOrNull`, and the Rust
 * core. Ordered operators use this so a non-numeric operand yields `false`
 * consistently across every engine.
 */
function numericOrNull(value: unknown): number | null {
  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }
  if (typeof value === "boolean" || value === null || value === undefined) {
    return null;
  }
  const text = String(value).trim();
  if (text === "") {
    return null;
  }
  const parsed = Number(text);
  return Number.isFinite(parsed) ? parsed : null;
}

function toBoolean(value: unknown): boolean {
  if (typeof value === "boolean") {
    return value;
  }

  return String(value).toLowerCase() === "true";
}

/**
 * Loose / numeric-aware equality — a numeric string equals its number ("750" == 750).
 * Mirrors Python `_loose_equal`, Rust `loose_equal`, Kotlin/Dart `looseEqual`.
 */
function looseEqual(a: unknown, b: unknown): boolean {
  if (a === b) {
    return true;
  }
  const an = numericOrNull(a);
  const bn = numericOrNull(b);
  if (an !== null && bn !== null) {
    return an === bn;
  }
  return String(a) === String(b);
}

// First-class date type — normalize ISO date/date-time (UTC) to an epoch via a
// strict regex + integer civil-days math (Howard Hinnant), NOT Date.parse (which
// is lenient and timezone-sensitive). Every engine reimplements this identically
// so dates order and compare equal byte-for-byte across server, Rust, and SDKs.
const ISO_DATE_RE = /^(\d{4})-(\d{1,2})-(\d{1,2})(?:[T ](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?(?:\.\d+)?Z?)?$/;
const DAYS_IN_MONTH = [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];

function isLeapYear(year: number): boolean {
  return (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
}

function daysFromCivil(year: number, month: number, day: number): number {
  const y = year - (month <= 2 ? 1 : 0);
  const era = Math.floor((y >= 0 ? y : y - 399) / 400);
  const yoe = y - era * 400;
  const doy = Math.floor((153 * (month + (month > 2 ? -3 : 9)) + 2) / 5) + (day - 1);
  const doe = yoe * 365 + Math.floor(yoe / 4) - Math.floor(yoe / 100) + doy;
  return era * 146097 + doe - 719468;
}

function dateToEpoch(value: unknown): number | null {
  if (value === null || value === undefined || typeof value === "boolean") {
    return null;
  }
  const match = ISO_DATE_RE.exec(String(value).trim());
  if (!match) {
    return null;
  }
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const hour = match[4] ? Number(match[4]) : 0;
  const minute = match[5] ? Number(match[5]) : 0;
  const second = match[6] ? Number(match[6]) : 0;
  if (month < 1 || month > 12) {
    return null;
  }
  const daysInMonth = month === 2 && isLeapYear(year) ? 29 : DAYS_IN_MONTH[month - 1];
  if (day < 1 || day > daysInMonth) {
    return null;
  }
  if (hour > 23 || minute > 59 || second > 59) {
    return null;
  }
  return daysFromCivil(year, month, day) * 86400 + hour * 3600 + minute * 60 + second;
}

function compareValues(node: RuleNode, actual: unknown): { pass: boolean; reason?: string; expected?: unknown } {
  const config = node.config ?? {};
  const operator = config.operator ?? "==";
  const fieldType = config.fieldType ?? "string";

  if (operator !== "exists" && operator !== "!exists" && actual === undefined) {
    return {
      pass: false,
      reason: `"${String(config.field ?? node.label)}" not provided`,
      expected: config.value
    };
  }

  if (operator === "exists") {
    return {
      pass: actual !== undefined && actual !== null && actual !== "",
      expected: "exists"
    };
  }

  if (operator === "!exists") {
    return {
      pass: actual === undefined || actual === null || actual === "",
      expected: "!exists"
    };
  }

  if (operator === "regex") {
    try {
      const expression = new RegExp(String(config.value ?? ""));
      return {
        pass: expression.test(String(actual ?? "")),
        expected: config.value
      };
    } catch {
      return {
        pass: false,
        reason: "Invalid regex pattern.",
        expected: config.value
      };
    }
  }

  if (operator === "in" || operator === "not_in") {
    const values = String(config.value ?? "")
      .split(",")
      .map((item) => item.trim())
      .filter(Boolean);
    const match = values.includes(String(actual));

    return {
      pass: operator === "in" ? match : !match,
      expected: values
    };
  }

  // Boolean-typed equality (declared boolean fields only).
  if (fieldType === "boolean" && (operator === "==" || operator === "!=")) {
    const match = toBoolean(actual) === toBoolean(config.value);
    return { pass: operator === "==" ? match : !match, expected: toBoolean(config.value) };
  }

  // First-class date type: both sides normalize to a UTC epoch (shared civil-days
  // routine — NOT Date.parse), so dates are truly ORDERED and equality is
  // spelling-insensitive, identical to the server, Rust core, and SDKs. A
  // non-ISO/out-of-range value is false for every operator.
  if (fieldType === "date") {
    const actualEpoch = dateToEpoch(actual);
    const expectedEpoch = dateToEpoch(config.value);
    if (actualEpoch === null || expectedEpoch === null) {
      return { pass: false, expected: operator === "between" ? [config.value, config.value2] : config.value };
    }
    switch (operator) {
      case "==":
        return { pass: actualEpoch === expectedEpoch, expected: config.value };
      case "!=":
        return { pass: actualEpoch !== expectedEpoch, expected: config.value };
      case ">=":
        return { pass: actualEpoch >= expectedEpoch, expected: config.value };
      case "<=":
        return { pass: actualEpoch <= expectedEpoch, expected: config.value };
      case ">":
        return { pass: actualEpoch > expectedEpoch, expected: config.value };
      case "<":
        return { pass: actualEpoch < expectedEpoch, expected: config.value };
      case "between": {
        const upperEpoch = dateToEpoch(config.value2);
        return { pass: upperEpoch !== null && actualEpoch >= expectedEpoch && actualEpoch <= upperEpoch, expected: [config.value, config.value2] };
      }
      default:
        return { pass: false, expected: config.value };
    }
  }

  // Ordered comparisons + inclusive range are NUMERIC-ONLY (non-date) in every
  // engine (Python `_coerce_number`, Kotlin/Dart `numericOrNull`, Rust core). A
  // non-numeric operand — a plain string / label — makes the condition `false`;
  // no lexical ordering. Dates are handled above.
  if (operator === ">=" || operator === "<=" || operator === ">" || operator === "<" || operator === "between") {
    const actualNumber = numericOrNull(actual);
    const expectedNumber = numericOrNull(config.value);
    if (operator === "between") {
      const upperNumber = numericOrNull(config.value2);
      const pass =
        actualNumber !== null && expectedNumber !== null && upperNumber !== null &&
        actualNumber >= expectedNumber && actualNumber <= upperNumber;
      return { pass, expected: [config.value, config.value2] };
    }
    if (actualNumber === null || expectedNumber === null) {
      return { pass: false, expected: config.value };
    }
    switch (operator) {
      case ">=":
        return { pass: actualNumber >= expectedNumber, expected: config.value };
      case "<=":
        return { pass: actualNumber <= expectedNumber, expected: config.value };
      case ">":
        return { pass: actualNumber > expectedNumber, expected: config.value };
      case "<":
        return { pass: actualNumber < expectedNumber, expected: config.value };
    }
  }

  // Equality is loose / numeric-aware (so "750" == 750) across every engine.
  if (operator === "==") {
    return { pass: looseEqual(actual, config.value), expected: config.value };
  }
  if (operator === "!=") {
    return { pass: !looseEqual(actual, config.value), expected: config.value };
  }
  return { pass: false, expected: config.value };
}

function mergeOutcomes(items: NodeEvaluation[]): OutcomeDecision[] {
  return items.flatMap((item) => item.outcomes);
}

function pickOutcome(candidates: OutcomeDecision[], allNodes: Iterable<RuleNode>, passed: boolean): OutcomeDecision {
  if (candidates.length > 0) {
    return [...candidates].sort(
      (left, right) => outcomeSeverity.indexOf(left.type) - outcomeSeverity.indexOf(right.type)
    )[0];
  }

  const outcomeNodes = [...allNodes].filter(
    (node) => node.type === "approve" || node.type === "review" || node.type === "reject"
  );
  const fallbackType = passed
    ? ((outcomeNodes.find((node) => node.type === "approve")?.type as OutcomeType | undefined) ??
      (outcomeNodes.find((node) => node.type === "review")?.type as OutcomeType | undefined) ??
      "approve")
    : ((outcomeNodes.find((node) => node.type === "reject")?.type as OutcomeType | undefined) ??
      (outcomeNodes.find((node) => node.type === "review")?.type as OutcomeType | undefined) ??
      "reject");
  const fallbackNode = outcomeNodes.find((node) => node.type === fallbackType);

  return {
    nodeId: fallbackNode?.id ?? "fallback",
    type: fallbackType,
    reason: fallbackNode?.config.reason ? String(fallbackNode.config.reason) : undefined
  };
}

function evaluateCompiledRule(
  compiled: CompiledRule,
  input: Record<string, unknown>,
  context: EvaluationContext
): EvaluationResult {
  const startedAt = performance.now();
  const conditionResults: Record<string, ConditionResult> = {};
  const memo = new Map<string, NodeEvaluation>();

  const evaluateNode = (nodeId: string): NodeEvaluation => {
    const cached = memo.get(nodeId);

    if (cached) {
      return cached;
    }

    const node = compiled.nodeMap.get(nodeId);

    if (!node) {
      throw new Error(`Missing node ${nodeId}`);
    }

    const childIds = compiled.childMap.get(nodeId) ?? [];
    const childResults = childIds.map((childId) => evaluateNode(childId));
    let result: NodeEvaluation;

    if (node.type === "approve" || node.type === "review" || node.type === "reject") {
      result = {
        pass: true,
        outcomes: [
          {
            type: node.type,
            reason: node.config.reason ? String(node.config.reason) : undefined,
            nodeId: node.id
          }
        ],
        explanation: {
          nodeId: node.id,
          nodeType: node.type,
          label: node.label,
          pass: true,
          detail: node.config.reason ? String(node.config.reason) : undefined,
          children: [],
          reachedOutcomes: [
            {
              type: node.type,
              reason: node.config.reason ? String(node.config.reason) : undefined,
              nodeId: node.id
            }
          ]
        }
      };
      memo.set(nodeId, result);
      return result;
    }

    if (node.type === "trigger") {
      const expectedEvent = node.config.event ? String(node.config.event) : "";
      const actualEvent = context.event ? String(context.event) : String(input.event ?? "");
      const eventPass = !expectedEvent || !actualEvent || expectedEvent === actualEvent;
      const childPass = childResults.length === 0 ? true : childResults.every((child) => child.pass);

      result = {
        pass: eventPass && childPass,
        outcomes: eventPass && childPass ? mergeOutcomes(childResults) : [],
        explanation: {
          nodeId: node.id,
          nodeType: node.type,
          label: node.label,
          pass: eventPass && childPass,
          detail: expectedEvent ? `event=${expectedEvent}` : "event=any",
          children: childResults.map((child) => child.explanation),
          reachedOutcomes: eventPass && childPass ? mergeOutcomes(childResults) : []
        }
      };
      memo.set(nodeId, result);
      return result;
    }

    if (node.type === "condition" || node.type === "score") {
      const field = String(node.config.field ?? "");
      const actual = field ? input[field] : undefined;
      const comparison = compareValues(node, actual);
      const pass = comparison.pass && (childResults.length === 0 ? true : childResults.every((child) => child.pass));
      const condition: ConditionResult = {
        nodeId: node.id,
        nodeLabel: node.label,
        type: node.type,
        pass,
        field,
        operator: node.config.operator ? String(node.config.operator) : undefined,
        expected: comparison.expected,
        actual,
        reason: comparison.reason
      };
      conditionResults[node.id] = condition;
      result = {
        pass,
        outcomes: pass ? mergeOutcomes(childResults) : [],
        explanation: {
          nodeId: node.id,
          nodeType: node.type,
          label: node.label,
          pass,
          detail: comparison.reason ?? `${field || node.label} ${String(node.config.operator ?? "==")}`,
          children: childResults.map((child) => child.explanation),
          reachedOutcomes: pass ? mergeOutcomes(childResults) : []
        }
      };
      memo.set(nodeId, result);
      return result;
    }

    if (node.type === "and" || (node.type === "group" && node.config.groupOp !== "OR")) {
      const pass = childResults.length > 0 && childResults.every((child) => child.pass);
      result = {
        pass,
        outcomes: pass ? mergeOutcomes(childResults) : [],
        explanation: {
          nodeId: node.id,
          nodeType: node.type,
          label: node.label,
          pass,
          detail: node.type === "group" ? "group=AND" : "all children must pass",
          children: childResults.map((child) => child.explanation),
          reachedOutcomes: pass ? mergeOutcomes(childResults) : []
        }
      };
      memo.set(nodeId, result);
      return result;
    }

    if (node.type === "or" || (node.type === "group" && node.config.groupOp === "OR")) {
      const passingChildren = childResults.filter((child) => child.pass);
      result = {
        pass: passingChildren.length > 0,
        outcomes: mergeOutcomes(passingChildren),
        explanation: {
          nodeId: node.id,
          nodeType: node.type,
          label: node.label,
          pass: passingChildren.length > 0,
          detail: node.type === "group" ? "group=OR" : "any child may pass",
          children: childResults.map((child) => child.explanation),
          reachedOutcomes: mergeOutcomes(passingChildren)
        }
      };
      memo.set(nodeId, result);
      return result;
    }

    if (node.type === "not") {
      const target = childResults[0];
      const pass = target ? !target.pass : false;
      result = {
        pass,
        outcomes: [],
        explanation: {
          nodeId: node.id,
          nodeType: node.type,
          label: node.label,
          pass,
          detail: "negates the first child",
          children: target ? [target.explanation] : []
        }
      };
      memo.set(nodeId, result);
      return result;
    }

    result = {
      pass: false,
      outcomes: [],
      explanation: {
        nodeId: node.id,
        nodeType: node.type,
        label: node.label,
        pass: false,
        children: []
      }
    };
    memo.set(nodeId, result);
    return result;
  };

  const hasAnyConnections = [...compiled.childMap.values()].some((children) => children.length > 0);
  const rootIds = compiled.roots.filter((rootId) => {
    const node = compiled.nodeMap.get(rootId);

    if (!node || node.type === "approve" || node.type === "review" || node.type === "reject") {
      return false;
    }

    if (!hasAnyConnections) {
      return true;
    }

    const hasIncoming = (compiled.parentMap.get(rootId) ?? []).length > 0;
    const hasOutgoing = (compiled.childMap.get(rootId) ?? []).length > 0;
    return hasIncoming || hasOutgoing;
  });
  const roots = rootIds.length > 0 ? rootIds : [...compiled.nodeMap.keys()];
  const rootResults = roots.map((rootId) => evaluateNode(rootId));
  const passed = rootResults.length === 0 ? true : rootResults.every((result) => result.pass);
  const reachedOutcomes = mergeOutcomes(rootResults);
  const outcome = pickOutcome(reachedOutcomes, compiled.nodeMap.values(), passed);

  return {
    outcome: outcome.type,
    passed,
    conditionResults,
    variableResults: context.variableResults ?? {},
    explanationTree: rootResults.map((result) => result.explanation),
    executionTimeMs: Number((performance.now() - startedAt).toFixed(3)),
    traceId: context.traceId || createTraceId(),
    version: context.version,
    reachedOutcomes
  };
}

export function evaluateRule(
  definition: { nodes: RuleNode[]; connections: { from: string; to: string }[] },
  input: Record<string, unknown>,
  context: EvaluationContext = {}
): EvaluationResult {
  const compiled = compileRule({
    nodes: definition.nodes,
    connections: definition.connections,
    metadata: undefined
  });
  return evaluateCompiledRule(compiled, input, context);
}

export { evaluateCompiledRule };

/**
 * Evaluate a single condition against an actual value. Thin, test-friendly
 * wrapper over the internal operator logic used to assert cross-engine
 * conformance against packages/shared/operators.spec.json.
 */
export function evaluateCondition(
  config: {
    operator?: RuleNode["config"]["operator"];
    value?: unknown;
    value2?: unknown;
    fieldType?: RuleNode["config"]["fieldType"];
    field?: string;
  },
  actual: unknown
): boolean {
  const node: RuleNode = {
    id: "cond",
    type: "condition",
    label: config.field ?? "cond",
    x: 0,
    y: 0,
    config: {
      field: config.field,
      fieldType: config.fieldType,
      operator: config.operator,
      value: config.value,
      value2: config.value2
    }
  };
  return compareValues(node, actual).pass;
}
