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

  // Ordered comparisons + inclusive range are NUMERIC-ONLY in every engine
  // (Python `_coerce_number`, Kotlin/Dart `numericOrNull`, Rust core). A
  // non-numeric operand — a label, or a date/ISO string — makes the condition
  // `false` so the web preview matches the server and the on-device SDKs
  // exactly. Do NOT reintroduce lexical string ordering or Date.parse ordering
  // here: that is the silent preview-vs-production divergence this block fixes.
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

  if (fieldType === "boolean") {
    const actualValue = toBoolean(actual);
    const expectedValue = toBoolean(config.value);
    const match = operator === "==" ? actualValue === expectedValue : actualValue !== expectedValue;

    return {
      pass: match,
      expected: expectedValue
    };
  }

  // Date equality compares the parsed instant so "2024-01-01" == "2024-1-1".
  // Ordered date comparisons are handled by the numeric-only block above (a
  // date string is non-numeric -> false), matching the server and SDKs, which
  // do not order dates via the numeric operators.
  if (fieldType === "date") {
    const actualDate = Date.parse(String(actual));
    const expectedDate = Date.parse(String(config.value));

    switch (operator) {
      case "==":
        return { pass: actualDate === expectedDate, expected: config.value };
      case "!=":
        return { pass: actualDate !== expectedDate, expected: config.value };
      default:
        return { pass: false, expected: config.value };
    }
  }

  if (fieldType === "number" || node.type === "score") {
    const actualNumber = toNumber(actual);
    const expectedNumber = toNumber(config.value);

    switch (operator) {
      case "==":
        return { pass: actualNumber === expectedNumber, expected: config.value };
      case "!=":
        return { pass: actualNumber !== expectedNumber, expected: config.value };
      default:
        return { pass: false, expected: config.value };
    }
  }

  const actualString = String(actual ?? "");
  const expectedString = String(config.value ?? "");

  switch (operator) {
    case "==":
      return { pass: actualString === expectedString, expected: config.value };
    case "!=":
      return { pass: actualString !== expectedString, expected: config.value };
    default:
      return { pass: false, expected: config.value };
  }
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
