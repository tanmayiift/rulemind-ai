import { readFileSync } from "node:fs";
import { join } from "node:path";
import { evaluateRule, validateRuleDefinition } from "../packages/rule-engine/src";
import type {
  EvaluationResult,
  FieldType,
  GroupOperator,
  NodeType,
  RuleDefinition,
  RuleNode,
  ValidationIssue
} from "../packages/shared/src";

interface WorkbookRow {
  [key: string]: string;
}

interface WorkbookSheet {
  name: string;
  rows: WorkbookRow[];
}

interface WorkbookManifest {
  generatedAt: string;
  sheets: WorkbookSheet[];
}

export interface WorkbookCaseResult {
  id: string;
  sheet: string;
  expected: string;
  actual: string;
  pass: boolean;
  detail?: string;
}

export interface WorkbookAutomationSummary {
  operators: { total: number; passed: number; failed: number };
  topology: { total: number; passed: number; failed: number };
}

export interface WorkbookAutomationResult {
  generatedAt: string;
  operatorResults: WorkbookCaseResult[];
  topologyResults: WorkbookCaseResult[];
  summary: WorkbookAutomationSummary;
}

const defaultRootDir = "/Users/tanmaykumar/Downloads/RuleMind.AI";

function loadManifest(rootDir = defaultRootDir): WorkbookManifest {
  return JSON.parse(readFileSync(join(rootDir, "qa", "results", "workbook-manifest.json"), "utf8")) as WorkbookManifest;
}

function makeNode(
  id: string,
  type: NodeType,
  label: string,
  config: RuleNode["config"] = {}
): RuleNode {
  return {
    id,
    type,
    label,
    x: 0,
    y: 0,
    config
  };
}

function makeCondition(
  id: string,
  field: string,
  operator: string,
  value: unknown,
  fieldType: FieldType = "string",
  value2?: unknown
) {
  return makeNode(id, "condition", id, {
    field,
    fieldType,
    operator,
    value,
    value2
  });
}

function makeOutcome(id: string, type: "approve" | "review" | "reject", reason?: string) {
  return makeNode(id, type, id, reason ? { reason } : {});
}

function makeLogic(id: string, type: "and" | "or" | "not" | "group" | "trigger", groupOp?: GroupOperator) {
  return makeNode(id, type, id, type === "group" ? { groupOp } : type === "trigger" ? { event: "on_submit" } : {});
}

const OMIT_FIELD = Symbol("omit-field");

function parseOperatorInput(fieldType: FieldType, raw: string): unknown {
  if (raw === "(omit field)") {
    return OMIT_FIELD;
  }

  if (raw === "NaN") {
    return Number.NaN;
  }

  if (fieldType === "number") {
    return Number(raw);
  }

  if (fieldType === "boolean") {
    return raw.toLowerCase() === "true";
  }

  return raw;
}

function stringifyValue(value: unknown) {
  if (typeof value === "number" && Number.isNaN(value)) {
    return "NaN";
  }

  return JSON.stringify(value);
}

function buildOperatorDefinition(row: WorkbookRow): { definition: RuleDefinition; input: Record<string, unknown> } {
  const fieldType = (row["Field Type"] || "string") as FieldType;
  const operator = row.Operator || "==";
  const inputValue = parseOperatorInput(fieldType, row["Test Input"] ?? "");
  const input =
    inputValue === OMIT_FIELD
      ? {}
      : {
          [row["Field Name"]]: inputValue
        };

  return {
    definition: {
      nodes: [
        makeCondition("c1", row["Field Name"], operator, row["Configured Value"], fieldType, row["Value2 (if between)"] || undefined),
        makeOutcome("a1", "approve", "pass"),
        makeOutcome("r1", "reject", "fail")
      ],
      connections: [{ from: "c1", to: "a1" }]
    },
    input
  };
}

function buildTopologyCase(row: WorkbookRow): {
  definition: RuleDefinition;
  input: Record<string, unknown>;
  validateOnly?: boolean;
  verify?: (result: EvaluationResult, issues: ValidationIssue[]) => { pass: boolean; actual: string; detail?: string };
} {
  switch (row["TC#"]) {
    case "LT-001":
      return {
        definition: {
          nodes: [makeCondition("c1", "score", ">=", "700", "number"), makeOutcome("a1", "approve")],
          connections: [{ from: "c1", to: "a1" }]
        },
        input: { score: 750 }
      };
    case "LT-002":
      return {
        definition: {
          nodes: [makeCondition("c1", "score", ">=", "700", "number"), makeOutcome("r1", "reject")],
          connections: []
        },
        input: { score: 600 }
      };
    case "LT-003":
    case "LT-004":
    case "LT-005":
      return {
        definition: {
          nodes: [
            makeLogic("and1", "and"),
            makeCondition("c1", "score", ">=", "700", "number"),
            makeCondition("c2", "income", ">", "50000", "number"),
            makeOutcome(row["TC#"] === "LT-003" ? "a1" : "r1", row["TC#"] === "LT-003" ? "approve" : "reject")
          ],
          connections: [
            { from: "and1", to: "c1" },
            { from: "and1", to: "c2" }
          ]
        },
        input:
          row["TC#"] === "LT-003"
            ? { score: 750, income: 80000 }
            : row["TC#"] === "LT-004"
              ? { score: 750, income: 30000 }
              : { score: 500, income: 30000 }
      };
    case "LT-006":
    case "LT-007":
    case "LT-008":
      return {
        definition: {
          nodes: [
            makeLogic("or1", "or"),
            makeCondition("c1", "score", ">=", "700", "number"),
            makeCondition("c2", "income", ">", "50000", "number"),
            makeOutcome(row["TC#"] === "LT-008" ? "r1" : "a1", row["TC#"] === "LT-008" ? "reject" : "approve")
          ],
          connections: [
            { from: "or1", to: "c1" },
            { from: "or1", to: "c2" }
          ]
        },
        input:
          row["TC#"] === "LT-006"
            ? { score: 500, income: 80000 }
            : row["TC#"] === "LT-007"
              ? { score: 750, income: 80000 }
              : { score: 500, income: 30000 }
      };
    case "LT-009":
    case "LT-010":
      return {
        definition: {
          nodes: [
            makeLogic("not1", "not"),
            makeCondition("c1", "score", ">=", "700", "number"),
            makeOutcome(row["TC#"] === "LT-009" ? "r1" : "a1", row["TC#"] === "LT-009" ? "reject" : "approve")
          ],
          connections: [{ from: "not1", to: "c1" }]
        },
        input: { score: row["TC#"] === "LT-009" ? 750 : 500 }
      };
    case "LT-011":
      return {
        definition: {
          nodes: [
            makeLogic("or1", "or"),
            makeLogic("and1", "and"),
            makeCondition("a", "a", "==", "1"),
            makeCondition("b", "b", "==", "1"),
            makeCondition("c", "c", "==", "1"),
            makeOutcome("a1", "approve")
          ],
          connections: [
            { from: "or1", to: "and1" },
            { from: "or1", to: "c" },
            { from: "and1", to: "a" },
            { from: "and1", to: "b" }
          ]
        },
        input: { a: "1", b: "1", c: "0" }
      };
    case "LT-012":
      return {
        definition: {
          nodes: [
            makeLogic("and1", "and"),
            makeLogic("or1", "or"),
            makeCondition("a", "a", "==", "1"),
            makeCondition("b", "b", "==", "1"),
            makeCondition("c", "c", "==", "1"),
            makeOutcome("a1", "approve")
          ],
          connections: [
            { from: "and1", to: "or1" },
            { from: "and1", to: "c" },
            { from: "or1", to: "a" },
            { from: "or1", to: "b" }
          ]
        },
        input: { a: "0", b: "1", c: "1" }
      };
    case "LT-013":
      return {
        definition: {
          nodes: [
            makeLogic("and1", "and"),
            makeLogic("or1", "or"),
            makeLogic("not1", "not"),
            makeCondition("a", "a", "==", "1"),
            makeCondition("b", "b", "==", "1"),
            makeCondition("c", "c", "==", "1"),
            makeOutcome("a1", "approve")
          ],
          connections: [
            { from: "and1", to: "or1" },
            { from: "and1", to: "not1" },
            { from: "or1", to: "a" },
            { from: "or1", to: "b" },
            { from: "not1", to: "c" }
          ]
        },
        input: { a: "0", b: "1", c: "0" }
      };
    case "LT-014":
    case "LT-015":
      return {
        definition: {
          nodes: [
            makeLogic("g1", "group", row["TC#"] === "LT-014" ? "AND" : "OR"),
            makeCondition("c1", "x", "==", "1"),
            makeCondition("c2", "y", "==", "1"),
            makeOutcome("a1", "approve")
          ],
          connections: [
            { from: "g1", to: "c1" },
            { from: "g1", to: "c2" }
          ]
        },
        input: row["TC#"] === "LT-014" ? { x: "1", y: "1" } : { x: "0", y: "1" }
      };
    case "LT-016":
      return {
        definition: {
          nodes: [
            makeLogic("t1", "trigger"),
            makeLogic("and1", "and"),
            makeCondition("c1", "score", ">=", "700", "number"),
            makeCondition("c2", "income", ">", "50000", "number"),
            makeOutcome("a1", "approve")
          ],
          connections: [
            { from: "t1", to: "and1" },
            { from: "and1", to: "c1" },
            { from: "and1", to: "c2" }
          ]
        },
        input: { score: 750, income: 80000 }
      };
    case "LT-017":
      return {
        definition: {
          nodes: [makeLogic("and1", "and"), makeOutcome("a1", "approve")],
          connections: []
        },
        input: {}
      };
    case "LT-018":
      return {
        definition: {
          nodes: [makeLogic("or1", "or"), makeOutcome("a1", "approve")],
          connections: []
        },
        input: {}
      };
    case "LT-019":
      return {
        definition: {
          nodes: [
            makeCondition("c1", "score", ">=", "700", "number"),
            makeOutcome("a1", "approve"),
            makeOutcome("r1", "reject")
          ],
          connections: [{ from: "c1", to: "a1" }]
        },
        input: { score: 750 }
      };
    case "LT-020":
      return {
        definition: {
          nodes: [
            makeCondition("c1", "x", "==", "1"),
            makeOutcome("a1", "approve"),
            makeCondition("c2", "y", "==", "1")
          ],
          connections: [{ from: "c1", to: "a1" }]
        },
        input: { x: "1" },
        verify: (result, issues) => {
          const hasWarning = issues.some((issue) => issue.level === "warn" && issue.message.includes("disconnected"));
          const pass = result.passed && result.outcome === "approve" && hasWarning;
          return {
            pass,
            actual: pass ? "approve (with warning)" : result.outcome,
            detail: hasWarning ? "disconnected warning present" : "missing disconnected warning"
          };
        }
      };
    case "LT-021":
      return {
        definition: {
          nodes: [makeLogic("a", "and"), makeLogic("b", "or"), makeOutcome("r1", "reject")],
          connections: [
            { from: "a", to: "b" },
            { from: "b", to: "a" }
          ]
        },
        input: {},
        validateOnly: true,
        verify: (_result, issues) => {
          const hasCircular = issues.some((issue) => issue.message.includes("Circular reference detected"));
          return {
            pass: hasCircular,
            actual: hasCircular ? "VALIDATION ERROR" : "NO VALIDATION ERROR",
            detail: issues.map((issue) => issue.message).join("; ")
          };
        }
      };
    case "LT-022": {
      const conditions = Array.from({ length: 20 }, (_, index) =>
        makeCondition(`c${index + 1}`, `f${index + 1}`, "==", "1")
      );
      const input = Object.fromEntries(Array.from({ length: 20 }, (_, index) => [`f${index + 1}`, "1"]));
      return {
        definition: {
          nodes: [makeLogic("and1", "and"), ...conditions, makeOutcome("a1", "approve")],
          connections: conditions.map((condition) => ({ from: "and1", to: condition.id }))
        },
        input
      };
    }
    case "LT-023":
      return {
        definition: {
          nodes: [makeCondition("c1", "score", ">=", "700", "number"), makeOutcome("r1", "reject")],
          connections: []
        },
        input: { income: 80000 },
        verify: (result) => {
          const reason = result.conditionResults.c1?.reason ?? "";
          const pass = !result.passed && result.outcome === "reject" && reason.includes("not provided");
          return {
            pass,
            actual: result.outcome,
            detail: reason
          };
        }
      };
    case "LT-024":
      return {
        definition: {
          nodes: [makeOutcome("a1", "approve")],
          connections: []
        },
        input: {}
      };
    default:
      throw new Error(`Unhandled topology row ${row["TC#"]}`);
  }
}

function runOperatorRows(manifest: WorkbookManifest): WorkbookCaseResult[] {
  const sheet = manifest.sheets.find((entry) => entry.name === "1. Operator Tests");

  if (!sheet) {
    throw new Error("Operator sheet not found.");
  }

  return sheet.rows.map((row) => {
    const { definition, input } = buildOperatorDefinition(row);
    const result = evaluateRule(definition, input);
    const expectedPass = row["Expected Result"] === "PASS";
    const actualPass = result.passed;

    return {
      id: row["TC#"],
      sheet: sheet.name,
      expected: row["Expected Result"],
      actual: actualPass ? "PASS" : "FAIL",
      pass: actualPass === expectedPass,
      detail: `outcome=${result.outcome}; input=${stringifyValue(Object.keys(input).length > 0 ? input : "(omitted)")}`
    };
  });
}

function runTopologyRows(manifest: WorkbookManifest): WorkbookCaseResult[] {
  const sheet = manifest.sheets.find((entry) => entry.name === "2. Logic & Topology");

  if (!sheet) {
    throw new Error("Topology sheet not found.");
  }

  return sheet.rows.map((row) => {
    const topology = buildTopologyCase(row);
    const issues = validateRuleDefinition(topology.definition);
    const result = topology.validateOnly ? null : evaluateRule(topology.definition, topology.input);

    if (topology.verify) {
      const verification = topology.verify(
        result ?? ({
          outcome: "reject",
          passed: false,
          conditionResults: {},
          variableResults: {},
          explanationTree: [],
          executionTimeMs: 0,
          traceId: "tr_validation_only",
          reachedOutcomes: []
        } as EvaluationResult),
        issues
      );
      return {
        id: row["TC#"],
        sheet: sheet.name,
        expected: `${row["Expected Outcome"]} / ${row["Expected Pass"]}`,
        actual: verification.actual,
        pass: verification.pass,
        detail: verification.detail
      };
    }

    const expectedOutcome = row["Expected Outcome"].toLowerCase();
    const expectedPass = row["Expected Pass"].toLowerCase() === "true";
    if (!result) {
      return {
        id: row["TC#"],
        sheet: sheet.name,
        expected: `${row["Expected Outcome"]} / ${row["Expected Pass"]}`,
        actual: "VALIDATION ONLY",
        pass: false,
        detail: issues.map((issue) => issue.message).join("; ") || undefined
      };
    }

    const pass = result.outcome === expectedOutcome && result.passed === expectedPass;

    return {
      id: row["TC#"],
      sheet: sheet.name,
      expected: `${row["Expected Outcome"]} / ${row["Expected Pass"]}`,
      actual: `${result.outcome} / ${String(result.passed).toUpperCase()}`,
      pass,
      detail: issues.map((issue) => issue.message).join("; ") || undefined
    };
  });
}

export function runWorkbookAutomation(rootDir = defaultRootDir): WorkbookAutomationResult {
  const manifest = loadManifest(rootDir);
  const operatorResults = runOperatorRows(manifest);
  const topologyResults = runTopologyRows(manifest);

  return {
    generatedAt: new Date().toISOString(),
    operatorResults,
    topologyResults,
    summary: {
      operators: {
        total: operatorResults.length,
        passed: operatorResults.filter((result) => result.pass).length,
        failed: operatorResults.filter((result) => !result.pass).length
      },
      topology: {
        total: topologyResults.length,
        passed: topologyResults.filter((result) => result.pass).length,
        failed: topologyResults.filter((result) => !result.pass).length
      }
    }
  };
}
