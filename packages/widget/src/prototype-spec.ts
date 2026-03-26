import type {
  EnvironmentName,
  EvaluationResult,
  FieldType,
  OutcomeType,
  RuleConnection,
  RuleDefinition,
  RuleNode,
  RuleRecord,
  RuleVersionRecord,
  ValidationIssue
} from "@rulemind/shared";

export type ThemeMode = "dark" | "light";

export const PROTOTYPE_THEME = {
  dark: {
    mode: "dark",
    bg: "#0b0d13",
    bg2: "#11141d",
    bg3: "#171b27",
    bgEl: "#1c2030",
    border: "#272d40",
    borderHi: "#3d4560",
    tx: "#dee0ea",
    tx2: "#8a8fa6",
    tx3: "#555a70",
    acc: "#6366f1",
    accH: "#818cf8",
    canvas: "#0d0f17",
    dot: "#1c2030",
    code: "#090b10",
    ok: "#4ade80",
    okBg: "#05261a",
    err: "#f87171",
    errBg: "#2a0a0a",
    warn: "#fbbf24",
    warnBg: "#2a1c04",
    sh: "0 4px 24px rgba(0,0,0,.5)"
  },
  light: {
    mode: "light",
    bg: "#f7f8fb",
    bg2: "#ffffff",
    bg3: "#eef0f6",
    bgEl: "#ffffff",
    border: "#d8dce8",
    borderHi: "#a0a6c0",
    tx: "#181b28",
    tx2: "#5c6178",
    tx3: "#8a8fa6",
    acc: "#4f46e5",
    accH: "#6366f1",
    canvas: "#f2f4f8",
    dot: "#d8dce8",
    code: "#eef0f6",
    ok: "#16a34a",
    okBg: "#ecfdf5",
    err: "#dc2626",
    errBg: "#fef2f2",
    warn: "#d97706",
    warnBg: "#fffbeb",
    sh: "0 4px 24px rgba(0,0,0,.07)"
  }
} as const;

export type PrototypeThemeColors = (typeof PROTOTYPE_THEME)[ThemeMode];

export const NODE_COLORS = {
  trigger: { dk: { bg: "#78350f", tx: "#fbbf24", bd: "#92400e" }, lt: { bg: "#fef3c7", tx: "#92400e", bd: "#f59e0b" } },
  condition: { dk: { bg: "#1e3a5f", tx: "#60a5fa", bd: "#1e40af" }, lt: { bg: "#dbeafe", tx: "#1e40af", bd: "#3b82f6" } },
  and: { dk: { bg: "#3b1f6e", tx: "#a78bfa", bd: "#5b21b6" }, lt: { bg: "#ede9fe", tx: "#5b21b6", bd: "#8b5cf6" } },
  or: { dk: { bg: "#3b1f6e", tx: "#a78bfa", bd: "#5b21b6" }, lt: { bg: "#ede9fe", tx: "#5b21b6", bd: "#8b5cf6" } },
  not: { dk: { bg: "#4c0519", tx: "#fb7185", bd: "#881337" }, lt: { bg: "#ffe4e6", tx: "#881337", bd: "#f43f5e" } },
  approve: { dk: { bg: "#14532d", tx: "#4ade80", bd: "#166534" }, lt: { bg: "#dcfce7", tx: "#166534", bd: "#22c55e" } },
  review: { dk: { bg: "#164e63", tx: "#22d3ee", bd: "#155e75" }, lt: { bg: "#cffafe", tx: "#155e75", bd: "#06b6d4" } },
  reject: { dk: { bg: "#5c1010", tx: "#f87171", bd: "#7f1d1d" }, lt: { bg: "#fee2e2", tx: "#991b1b", bd: "#ef4444" } },
  score: { dk: { bg: "#134e4a", tx: "#2dd4bf", bd: "#115e59" }, lt: { bg: "#ccfbf1", tx: "#115e59", bd: "#14b8a6" } },
  group: { dk: { bg: "#1c2030", tx: "#a78bfa", bd: "#3d4560" }, lt: { bg: "#f0f2f7", tx: "#5b21b6", bd: "#d8dce8" } }
} as const;

export const ICONS = {
  trigger: "\u26A1",
  condition: "\u25C9",
  and: "\u2227",
  or: "\u2228",
  not: "\u00AC",
  approve: "\u2713",
  review: "\u2299",
  reject: "\u2715",
  score: "#",
  group: "{ }"
} as const;

export const NODE_DEFS = [
  { type: "trigger", label: "Trigger", cat: "flow" },
  { type: "condition", label: "Condition", cat: "logic" },
  { type: "score", label: "Score", cat: "numeric" },
  { type: "and", label: "AND", cat: "operator" },
  { type: "or", label: "OR", cat: "operator" },
  { type: "not", label: "NOT", cat: "operator" },
  { type: "group", label: "Group", cat: "operator" },
  { type: "approve", label: "Approve", cat: "outcome" },
  { type: "review", label: "Review", cat: "outcome" },
  { type: "reject", label: "Reject", cat: "outcome" }
] as const;

export const OPS = [
  { v: "==", l: "equals" },
  { v: "!=", l: "not equals" },
  { v: ">", l: "greater than" },
  { v: ">=", l: "\u2265 (gte)" },
  { v: "<", l: "less than" },
  { v: "<=", l: "\u2264 (lte)" },
  { v: "between", l: "between" },
  { v: "in", l: "in list" },
  { v: "not_in", l: "not in list" },
  { v: "regex", l: "matches regex" },
  { v: "exists", l: "exists" },
  { v: "!exists", l: "not exists" }
] as const;

export const FTYPES = [
  { v: "number", l: "Number" },
  { v: "string", l: "Text" },
  { v: "boolean", l: "Boolean" },
  { v: "date", l: "Date" },
  { v: "enum", l: "Enum / List" }
] as const;

export function nodeColors(type: RuleNode["type"], dark: boolean) {
  return (NODE_COLORS[type] || NODE_COLORS.condition)[dark ? "dk" : "lt"];
}

export function nodeWidth(node: RuleNode) {
  const base = (node.label || "").length * 7.5 + 44;
  return Math.max(90, base);
}

export function nodeHeight(node: RuleNode) {
  if ((node.type === "condition" || node.type === "score") && node.config?.field) {
    return 42;
  }

  if (node.type === "group") {
    return 38;
  }

  return 32;
}

export function cloneDefinition(definition: RuleDefinition): RuleDefinition {
  return JSON.parse(JSON.stringify(definition)) as RuleDefinition;
}

export function generatePrototypeExpression(nodes: RuleNode[], connections: RuleConnection[]) {
  if (!nodes.length) {
    return "// Empty \u2014 drag nodes onto the canvas to begin";
  }

  const childMap: Record<string, string[]> = {};
  connections.forEach((connection) => {
    (childMap[connection.from] ||= []).push(connection.to);
  });
  const incoming = new Set(connections.map((connection) => connection.to));
  const roots = nodes.filter((node) => !incoming.has(node.id));

  function format(id: string, depth = 0): string | null {
    const node = nodes.find((item) => item.id === id);

    if (!node) {
      return null;
    }

    const pad = "  ".repeat(depth);
    const config = node.config || {};

    if (node.type === "condition" || node.type === "score") {
      const field = String(config.field || "unknown_field");
      const operator = String(config.operator || "==");

      if (operator === "exists" || operator === "!exists") {
        return `${pad}${field} ${operator.toUpperCase()}`;
      }

      if (operator === "between") {
        return `${pad}${field} BETWEEN ${config.value ?? "?"} AND ${config.value2 ?? "?"}`;
      }

      if (operator === "in" || operator === "not_in") {
        return `${pad}${field} ${operator === "in" ? "IN" : "NOT IN"} (${config.value || "?"})`;
      }

      if (operator === "regex") {
        return `${pad}${field} MATCHES /${config.value || "?"}/`;
      }

      const value =
        config.fieldType === "string"
          ? `"${config.value ?? ""}"`
          : config.fieldType === "boolean"
            ? String(config.value || "true")
            : String(config.value ?? "?");
      return `${pad}${field} ${operator} ${value}`;
    }

    if (node.type === "and" || node.type === "or" || node.type === "group") {
      const children = (childMap[id] || []).map((childId) => format(childId, depth + 1)).filter(Boolean);
      const operator = node.type === "group" ? String(config.groupOp || "AND") : node.type.toUpperCase();

      if (!children.length) {
        return `${pad}${operator} ( )`;
      }

      return `${pad}${operator} (\n${children.join(",\n")}\n${pad})`;
    }

    if (node.type === "not") {
      const children = (childMap[id] || []).map((childId) => format(childId, depth + 1)).filter(Boolean);
      return `${pad}NOT (\n${children.join(",\n")}\n${pad})`;
    }

    if (node.type === "trigger") {
      const event = String(config.event || "on_request");
      const children = (childMap[id] || []).map((childId) => format(childId, depth + 1)).filter(Boolean);
      return children.length ? `${pad}WHEN ${event} {\n${children.join("\n")}\n${pad}}` : `${pad}WHEN ${event}`;
    }

    if (node.type === "approve" || node.type === "review" || node.type === "reject") {
      const reason = config.reason ? ` "${String(config.reason)}"` : "";
      return `${pad}=> ${node.type.toUpperCase()}${reason}`;
    }

    return null;
  }

  const lines = ["// Auto-generated rule expression"];
  const sourceNodes = roots.length ? roots : nodes;
  sourceNodes.forEach((node) => {
    const line = format(node.id);
    if (line) {
      lines.push(line);
    }
  });
  return lines.join("\n");
}

export function validatePrototypeRule(nodes: RuleNode[], connections: RuleConnection[]): ValidationIssue[] {
  const issues: ValidationIssue[] = [];

  if (!nodes.length) {
    return issues;
  }

  if (!nodes.some((node) => node.type === "approve" || node.type === "review" || node.type === "reject")) {
    issues.push({ level: "error", message: "No outcome node \u2014 add Approve, Review, or Reject" });
  }

  nodes
    .filter((node) => node.type === "condition" || node.type === "score")
    .forEach((node) => {
      const config = node.config || {};

      if (!config.field) {
        issues.push({ level: "warn", message: `"${node.label}": missing field name` });
      }

      if (config.operator === "between" && (!config.value || !config.value2)) {
        issues.push({ level: "warn", message: `"${node.label}": 'between' needs two values` });
      }
    });

  const hasIncoming = new Set(connections.map((connection) => connection.to));
  const hasOutgoing = new Set(connections.map((connection) => connection.from));
  const orphans = nodes.filter((node) => !hasIncoming.has(node.id) && !hasOutgoing.has(node.id));

  if (orphans.length > 0 && nodes.length > 1) {
    issues.push({ level: "warn", message: `${orphans.length} disconnected node(s)` });
  }

  const childMap: Record<string, string[]> = {};
  connections.forEach((connection) => {
    (childMap[connection.from] ||= []).push(connection.to);
  });
  const visited = new Set<string>();

  function hasCycle(id: string, path = new Set<string>()): boolean {
    if (path.has(id)) {
      return true;
    }

    if (visited.has(id)) {
      return false;
    }

    visited.add(id);
    path.add(id);

    for (const child of childMap[id] || []) {
      if (hasCycle(child, new Set(path))) {
        return true;
      }
    }

    return false;
  }

  let cycleFound = false;
  nodes.forEach((node) => {
    if (!cycleFound && hasCycle(node.id)) {
      issues.push({ level: "error", message: "Circular reference detected" });
      cycleFound = true;
    }
  });

  return issues;
}

interface PrototypeConditionResult {
  pass: boolean;
  actual?: unknown;
  expected?: unknown;
  op?: string;
  reason?: string;
}

interface PrototypeEvaluationResult {
  cResults: Record<string, PrototypeConditionResult>;
  allPass: boolean;
  outcome: OutcomeType;
}

export function evaluatePrototypeRule(nodes: RuleNode[], connections: RuleConnection[], data: Record<string, unknown>): PrototypeEvaluationResult {
  const conditionResults: Record<string, PrototypeConditionResult> = {};

  nodes
    .filter((node) => node.type === "condition" || node.type === "score")
    .forEach((node) => {
      const config = node.config || {};
      const field = String(config.field || "");
      const operator = String(config.operator || "==");
      const actualValue = data[field];

      if (actualValue === undefined && operator !== "exists" && operator !== "!exists") {
        conditionResults[node.id] = { pass: false, reason: `"${field}" not provided` };
        return;
      }

      let pass = false;
      const actualNumber = Number(actualValue);
      const expectedNumber = Number(config.value);

      switch (operator) {
        case "==":
          pass = String(actualValue) === String(config.value);
          break;
        case "!=":
          pass = String(actualValue) !== String(config.value);
          break;
        case ">":
          pass = actualNumber > expectedNumber;
          break;
        case ">=":
          pass = actualNumber >= expectedNumber;
          break;
        case "<":
          pass = actualNumber < expectedNumber;
          break;
        case "<=":
          pass = actualNumber <= expectedNumber;
          break;
        case "between":
          pass = actualNumber >= expectedNumber && actualNumber <= Number(config.value2);
          break;
        case "in":
          pass = String(config.value || "")
            .split(",")
            .map((item) => item.trim())
            .includes(String(actualValue));
          break;
        case "not_in":
          pass = !String(config.value || "")
            .split(",")
            .map((item) => item.trim())
            .includes(String(actualValue));
          break;
        case "regex":
          try {
            pass = new RegExp(String(config.value || "")).test(String(actualValue));
          } catch {
            pass = false;
          }
          break;
        case "exists":
          pass = actualValue !== undefined && actualValue !== null && actualValue !== "";
          break;
        case "!exists":
          pass = actualValue === undefined || actualValue === null || actualValue === "";
          break;
        default:
          pass = false;
      }

      conditionResults[node.id] = {
        pass,
        actual: actualValue,
        expected: config.value,
        op: operator
      };
    });

  const childMap: Record<string, string[]> = {};
  connections.forEach((connection) => {
    (childMap[connection.from] ||= []).push(connection.to);
  });

  function evaluateNode(id: string): boolean {
    const node = nodes.find((item) => item.id === id);

    if (!node) {
      return false;
    }

    if (conditionResults[id] !== undefined) {
      return conditionResults[id].pass;
    }

    if (node.type === "and" || node.type === "group") {
      const children = childMap[id] || [];
      return children.length > 0 && children.every(evaluateNode);
    }

    if (node.type === "or") {
      return (childMap[id] || []).some(evaluateNode);
    }

    if (node.type === "not") {
      const children = childMap[id] || [];
      return children.length > 0 ? !evaluateNode(children[0]) : false;
    }

    if (node.type === "trigger") {
      return (childMap[id] || []).every(evaluateNode);
    }

    return true;
  }

  const incoming = new Set(connections.map((connection) => connection.to));
  const roots = nodes.filter((node) => !incoming.has(node.id));
  const conditionRoots = roots.filter((node) => node.type !== "approve" && node.type !== "review" && node.type !== "reject");
  const allPass = conditionRoots.length ? conditionRoots.every((node) => evaluateNode(node.id)) : Object.values(conditionResults).every((result) => result.pass);
  const outcomes = nodes.filter(
    (node): node is RuleNode & { type: OutcomeType } =>
      node.type === "approve" || node.type === "review" || node.type === "reject"
  );
  const outcome: OutcomeType = allPass
    ? outcomes.find((node) => node.type === "approve")?.type || "approve"
    : outcomes.find((node) => node.type === "reject")?.type || "reject";

  return { cResults: conditionResults, allPass, outcome };
}

export function prototypeResultToEvaluation(
  result: PrototypeEvaluationResult,
  nodes: RuleNode[]
): EvaluationResult {
  const conditionResults = Object.fromEntries(
    Object.entries(result.cResults).map(([id, entry]) => {
      const node = nodes.find((item) => item.id === id);
      return [
        id,
        {
          nodeId: id,
          nodeLabel: node?.label || id,
          type: (node?.type || "condition") as RuleNode["type"],
          pass: entry.pass,
          field: node?.config?.field ? String(node.config.field) : undefined,
          operator: entry.op,
          expected: entry.expected,
          actual: entry.actual,
          reason: entry.reason
        }
      ];
    })
  );

  return {
    outcome: result.outcome,
    passed: result.allPass,
    conditionResults,
    variableResults: {},
    explanationTree: [],
    executionTimeMs: 0,
    traceId: "tr_prototype_local",
    reachedOutcomes: []
  };
}

export function createLocalRuleRecord(input: {
  id?: string;
  name: string;
  environment: EnvironmentName;
  expression: string;
  definition: RuleDefinition;
  currentVersion: number;
  createdAt?: string;
}): RuleRecord {
  const now = input.createdAt ?? new Date().toISOString();
  return {
    id: input.id ?? `local_rule_${Date.now().toString(36)}`,
    name: input.name,
    environment: input.environment,
    tags: [],
    isActive: true,
    currentVersion: input.currentVersion,
    createdBy: "prototype-harness",
    createdAt: now,
    updatedAt: now,
    expression: input.expression,
    status: "approved",
    definition: cloneDefinition(input.definition)
  };
}

export function createLocalVersionRecord(input: {
  ruleId: string;
  version: number;
  environment: EnvironmentName;
  expression: string;
  definition: RuleDefinition;
}): RuleVersionRecord {
  return {
    id: `local_version_${input.ruleId}_${input.version}`,
    ruleId: input.ruleId,
    version: input.version,
    definition: cloneDefinition(input.definition),
    expression: input.expression,
    createdAt: new Date().toISOString(),
    createdBy: "prototype-harness",
    environment: input.environment,
    summary: `${input.definition.nodes.length} nodes \u00B7 ${input.definition.connections.length} connections`
  };
}

export function emptyDefinition(environment: EnvironmentName): RuleDefinition {
  return {
    nodes: [],
    connections: [],
    metadata: {
      environment
    }
  };
}

export function fieldInputType(fieldType?: FieldType) {
  if (fieldType === "number") {
    return "number";
  }

  if (fieldType === "date") {
    return "date";
  }

  return "text";
}
