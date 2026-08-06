"use client";


import * as React from "react";
import { useRouter } from "next/navigation";
import { flushSync } from "react-dom";
import {
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  CircleHelp,
  CircleSlash,
  Eye,
  FileSearch,
  GitBranch,
  Layers,
  Plus,
  Trash2,
  Workflow,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { apiJson, apiText, streamDecisions, type StreamedDecision } from "../../lib/api";
import { useRuleMindStore } from "../../lib/store";
import { ENVIRONMENT_ACCENT, THEMES, type ThemeTokens } from "../theme";
import { ConnectorIcon } from "../icons";
import type {
  AuditErrorRecord,
  BootstrapPayload,
  ConnectorRecord,
  DeployStatusPayload,
  ExecutionTraceStepRecord,
  PolicyExecuteResponse,
  PolicyRecord,
  PolicyStepRecord,
  PromotionRecord,
  RuleConditionResult,
  RuleNodeRecord,
  RuleRecord,
  RuleTreeNodeRecord,
  RuleTestResponse,
  ScorecardBinRecord,
  ScorecardRangeRecord,
  ScorecardRecord,
  SettingsRecord,
  DataProtection,
  SloConfig,
  SloStatus,
  VariableBatchTestResponse,
  VariableGraphResponse,
  VariableRecord,
} from "../types";



import {
  PageId,
  PAGE_META,
  NODE_TYPES,
  STATUS_ORDER,
  CATEGORY_ORDER,
  useTheme,
  useBootstrapData,
  statusColorKey,
  statusLabel,
  tone,
  toRuleNodeLabel,
  cloneNode,
  connectorLabel,
  sourceMark,
  useFilteredVariables,
  StatCard,
  Button,
  StatusBadge,
  InlineInput,
  InlineSelect,
  InlineTextarea,
  SectionHeader,
  InfoBanner,
  EmptyState,
} from "../kit";

type RuleBuilderMode = "simple" | "advanced";

function makeRuleTreeId(prefix: string): string {
  return prefix + "_" + Date.now() + "_" + Math.random().toString(16).slice(2, 8);
}

function parseDraftRuleValue(value: unknown): string | number | boolean | null {
  if (typeof value === "string") {
    const lowered = value.trim().toLowerCase();
    if (lowered === "true") {
      return true;
    }
    if (lowered === "false") {
      return false;
    }
    if (lowered === "null") {
      return null;
    }
    if (lowered !== "" && !Number.isNaN(Number(lowered))) {
      return lowered.includes(".") ? Number.parseFloat(lowered) : Number.parseInt(lowered, 10);
    }
  }
  return value as string | number | boolean | null;
}

const RULE_OPERATORS: Array<{ value: string; label: string }> = [
  { value: "==", label: "==" },
  { value: "!=", label: "≠" },
  { value: ">", label: ">" },
  { value: ">=", label: "≥" },
  { value: "<", label: "<" },
  { value: "<=", label: "≤" },
  { value: "between", label: "between" },
  { value: "in", label: "in" },
  { value: "not_in", label: "not in" },
  { value: "regex", label: "regex" },
  { value: "exists", label: "exists" },
  { value: "!exists", label: "!exists" }
];

function draftNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isNaN(value) ? null : value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

function draftOptionList(expected: unknown): string[] {
  if (Array.isArray(expected)) return expected.map((item) => String(item).trim()).filter(Boolean);
  if (expected === null || expected === undefined) return [];
  return String(expected)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function draftLooseEqual(actual: unknown, expected: unknown): boolean {
  if (actual === expected) return true;
  const a = draftNumber(actual);
  const b = draftNumber(expected);
  if (a !== null && b !== null) return a === b;
  return String(actual) === String(expected);
}

// Mirrors packages/shared/operators.spec.json — kept in parity with the server,
// Kotlin, and Dart engines so the draft preview matches production.
function compareDraftRuleValue(
  actual: unknown,
  operator: string,
  expected: unknown,
  expected2?: unknown,
  fieldType?: string
): boolean {
  if (operator === "exists") return actual !== undefined && actual !== null && actual !== "";
  if (operator === "!exists") return actual === undefined || actual === null || actual === "";

  if (operator === "in" || operator === "not_in") {
    const options = draftOptionList(expected);
    const matched = options.some((option) => draftLooseEqual(actual, option));
    return operator === "in" ? matched : !matched;
  }

  if (operator === "regex") {
    if (actual === undefined || actual === null) return false;
    try {
      return new RegExp(String(expected)).test(String(actual));
    } catch {
      return false;
    }
  }

  if ((fieldType ?? "").toLowerCase() === "boolean" && (operator === "==" || operator === "!=")) {
    const toBool = (value: unknown) =>
      typeof value === "boolean" ? value : ["true", "1", "yes"].includes(String(value).trim().toLowerCase());
    const matched = toBool(actual) === toBool(expected);
    return operator === "==" ? matched : !matched;
  }

  if ([">=", "<=", ">", "<", "between"].includes(operator)) {
    const actualValue = draftNumber(actual);
    const expectedValue = draftNumber(expected);
    if (actualValue === null || expectedValue === null) return false;
    if (operator === ">=") return actualValue >= expectedValue;
    if (operator === "<=") return actualValue <= expectedValue;
    if (operator === ">") return actualValue > expectedValue;
    if (operator === "<") return actualValue < expectedValue;
    const upper = draftNumber(expected2);
    if (upper === null) return false;
    return actualValue >= expectedValue && actualValue <= upper;
  }

  if (operator === "==") return draftLooseEqual(actual, expected);
  if (operator === "!=") return !draftLooseEqual(actual, expected);
  return false;
}

function createTreeCondition(defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  return {
    id: makeRuleTreeId("condition"),
    type: "condition",
    variable: defaultVariable?.id ?? "",
    operator: ">=",
    value: "",
  };
}

function createTreeGroup(defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  return {
    id: makeRuleTreeId("group"),
    type: "group",
    logic: "AND",
    children: [createTreeCondition(defaultVariable)],
    onPass: "approve",
    onFail: "reject",
  };
}

function createTreeNot(defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  return {
    id: makeRuleTreeId("not"),
    type: "not",
    child: createTreeCondition(defaultVariable),
  };
}

function normalizeRuleTree(node: RuleTreeNodeRecord | null | undefined, defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  if (!node) {
    return createTreeGroup(defaultVariable);
  }
  if (node.type === "condition") {
    return {
      id: node.id ?? makeRuleTreeId("condition"),
      type: "condition",
      variable: node.variable ?? defaultVariable?.id ?? "",
      operator: node.operator ?? ">=",
      value: node.value ?? "",
      ...(node.value2 !== undefined ? { value2: node.value2 } : {}),
      ...(node.fieldType !== undefined ? { fieldType: node.fieldType } : {}),
    };
  }
  if (node.type === "not") {
    return {
      id: node.id ?? makeRuleTreeId("not"),
      type: "not",
      child: normalizeRuleTree(node.child, defaultVariable),
    };
  }
  return {
    id: node.id ?? makeRuleTreeId("group"),
    type: "group",
    logic: node.logic ?? "AND",
    children:
      node.children && node.children.length
        ? node.children.map((child) => normalizeRuleTree(child, defaultVariable))
        : [createTreeCondition(defaultVariable)],
    onPass: node.onPass ?? "approve",
    onFail: node.onFail ?? "reject",
  };
}

function simpleNodesToTree(nodes: RuleNodeRecord[], defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  if (!nodes.length) {
    return createTreeGroup(defaultVariable);
  }
  let logic: "AND" | "OR" = "AND";
  let onPass: "approve" | "review" | "reject" = "approve";
  const children: RuleTreeNodeRecord[] = [];
  nodes.forEach((node) => {
    if (node.type === "condition") {
      children.push({
        id: node.id,
        type: "condition",
        variable: node.variable ?? defaultVariable?.id ?? "",
        operator: node.operator ?? ">=",
        value: node.value ?? "",
        ...(node.value2 !== undefined ? { value2: node.value2 } : {}),
        ...(node.fieldType !== undefined ? { fieldType: node.fieldType } : {}),
      });
    }
    if (node.type === "and") {
      logic = "AND";
    }
    if (node.type === "or") {
      logic = "OR";
    }
    if (node.type === "approve" || node.type === "review" || node.type === "reject") {
      onPass = node.type;
    }
  });
  return normalizeRuleTree(
    {
      type: "group",
      logic,
      children,
      onPass,
      onFail: "reject",
    },
    defaultVariable
  );
}

function flattenTreeToNodes(tree: RuleTreeNodeRecord | null | undefined): RuleNodeRecord[] {
  if (!tree) {
    return [];
  }
  const nodes: RuleNodeRecord[] = [];
  let counter = 1;
  const nextId = (prefix: string) => prefix + "_" + counter++;
  const walk = (node: RuleTreeNodeRecord, inheritedLogic: "AND" | "OR" = "AND") => {
    if (node.type === "condition") {
      if (nodes.length && nodes[nodes.length - 1]?.type === "condition") {
        nodes.push({
          id: nextId("logic"),
          type: inheritedLogic.toLowerCase() as "and" | "or",
          label: inheritedLogic,
        });
      }
      nodes.push({
        id: node.id ?? nextId("condition"),
        type: "condition",
        variable: node.variable,
        operator: node.operator,
        value: String(node.value ?? ""),
        label: "Condition",
      });
      return;
    }
    if (node.type === "not") {
      const child = node.child ? normalizeRuleTree(node.child) : createTreeCondition();
      if (child.type === "condition") {
        walk(
          {
            ...child,
            operator: child.operator === "!=" ? "==" : "!=",
          },
          inheritedLogic
        );
      } else {
        walk(child, inheritedLogic);
      }
      return;
    }
    const childLogic = node.logic ?? inheritedLogic;
    (node.children ?? []).forEach((child) => walk(child, childLogic));
  };
  walk(tree, tree.type === "group" ? tree.logic ?? "AND" : "AND");
  const onPass = tree.type === "group" ? tree.onPass ?? "approve" : "approve";
  nodes.push({
    id: nextId("outcome"),
    type: onPass,
    label: onPass[0].toUpperCase() + onPass.slice(1),
  });
  return nodes;
}

function countRuleTreeNodes(node: RuleTreeNodeRecord | null | undefined): number {
  if (!node) {
    return 0;
  }
  if (node.type === "condition") {
    return 1;
  }
  if (node.type === "not") {
    return 1 + countRuleTreeNodes(node.child);
  }
  return 1 + (node.children ?? []).reduce((total, child) => total + countRuleTreeNodes(child), 0);
}

function collectTreeVariableIds(node: RuleTreeNodeRecord | null | undefined): string[] {
  if (!node) {
    return [];
  }
  if (node.type === "condition") {
    return node.variable ? [node.variable] : [];
  }
  if (node.type === "not") {
    return collectTreeVariableIds(node.child);
  }
  return (node.children ?? []).flatMap((child) => collectTreeVariableIds(child));
}

function ruleVariableIds(rule: RuleRecord): string[] {
  if (rule.tree) {
    return collectTreeVariableIds(rule.tree);
  }
  return rule.nodes.filter((node) => node.type === "condition" && node.variable).map((node) => node.variable as string);
}

function treeExpression(node: RuleTreeNodeRecord, variables: VariableRecord[], depth = 0): string {
  if (node.type === "condition") {
    const variable = variables.find((item) => item.id === node.variable);
    return (variable?.name ?? node.variable ?? "Variable") + " " + (node.operator ?? "==") + " " + String(node.value ?? "");
  }
  if (node.type === "not") {
    return "NOT (" + treeExpression(normalizeRuleTree(node.child), variables, depth + 1) + ")";
  }
  const children = node.children ?? [];
  const joined = children.length
    ? children.map((child) => treeExpression(child, variables, depth + 1)).join(" " + (node.logic ?? "AND") + " ")
    : "?";
  const wrapped = "(" + joined + ")";
  if (depth > 0) {
    return wrapped;
  }
  return "IF " + wrapped + " → " + String(node.onPass ?? "approve").toUpperCase() + " ELSE → " + String(node.onFail ?? "reject").toUpperCase();
}

function updateRuleTreeNode(node: RuleTreeNodeRecord, targetId: string, updater: (current: RuleTreeNodeRecord) => RuleTreeNodeRecord): RuleTreeNodeRecord {
  if (node.id === targetId) {
    return normalizeRuleTree(updater(node));
  }
  if (node.type === "not" && node.child) {
    return { ...node, child: updateRuleTreeNode(node.child, targetId, updater) };
  }
  if (node.type === "group") {
    return { ...node, children: (node.children ?? []).map((child) => updateRuleTreeNode(child, targetId, updater)) };
  }
  return node;
}

function removeRuleTreeNode(node: RuleTreeNodeRecord, targetId: string, defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  if (node.type === "not") {
    if (node.child?.id === targetId) {
      return { ...node, child: createTreeCondition(defaultVariable) };
    }
    return { ...node, child: node.child ? removeRuleTreeNode(node.child, targetId, defaultVariable) : createTreeCondition(defaultVariable) };
  }
  if (node.type === "group") {
    const nextChildren = (node.children ?? [])
      .filter((child) => child.id !== targetId)
      .map((child) => removeRuleTreeNode(child, targetId, defaultVariable));
    return {
      ...node,
      children: nextChildren.length ? nextChildren : [createTreeCondition(defaultVariable)],
    };
  }
  return node;
}

function moveRuleTreeNode(node: RuleTreeNodeRecord, targetId: string, direction: -1 | 1): RuleTreeNodeRecord {
  if (node.type === "not" && node.child) {
    return { ...node, child: moveRuleTreeNode(node.child, targetId, direction) };
  }
  if (node.type === "group") {
    const children = [...(node.children ?? [])];
    const index = children.findIndex((child) => child.id === targetId);
    if (index >= 0) {
      const nextIndex = index + direction;
      if (nextIndex >= 0 && nextIndex < children.length) {
        [children[index], children[nextIndex]] = [children[nextIndex], children[index]];
      }
      return { ...node, children };
    }
    return { ...node, children: children.map((child) => moveRuleTreeNode(child, targetId, direction)) };
  }
  return node;
}

function simulateRuleTreeDraft(
  tree: RuleTreeNodeRecord,
  variables: VariableRecord[]
): { passed: boolean; outcome: string; conditions: RuleConditionResult[]; groupResults: Array<{ id?: string; logic: string; passed: boolean; childCount: number }> } {
  const variableMap = Object.fromEntries(variables.map((variable) => [variable.id, variable]));
  const conditions: RuleConditionResult[] = [];
  const groupResults: Array<{ id?: string; logic: string; passed: boolean; childCount: number }> = [];

  const evaluateNode = (node: RuleTreeNodeRecord, inheritedLogic: string = "AND"): boolean => {
    if (node.type === "condition") {
      const variable = node.variable ? variableMap[node.variable] : undefined;
      const actual = variable?.last_test_result?.value;
      const expected = parseDraftRuleValue(node.value ?? "");
      const expected2 = node.value2 === undefined ? undefined : parseDraftRuleValue(node.value2);
      const passed = compareDraftRuleValue(actual, node.operator ?? "==", expected, expected2, node.fieldType);
      conditions.push({
        variable_id: node.variable ?? "",
        variable_name: variable?.name ?? node.variable ?? "",
        source_id: variable?.source_id,
        operator: node.operator ?? "==",
        threshold: expected,
        value: actual,
        passed,
        group: inheritedLogic,
      });
      return passed;
    }
    if (node.type === "not") {
      const result = !evaluateNode(normalizeRuleTree(node.child), "NOT");
      groupResults.push({ id: node.id, logic: "NOT", passed: result, childCount: 1 });
      return result;
    }
    const logic = node.logic ?? "AND";
    const childResults = (node.children ?? []).map((child) => evaluateNode(child, logic));
    const passed = logic === "AND" ? childResults.every(Boolean) : childResults.some(Boolean);
    groupResults.push({ id: node.id, logic, passed, childCount: childResults.length });
    return passed;
  };

  const passed = evaluateNode(tree, tree.logic ?? "AND");
  return {
    passed,
    outcome: passed ? String(tree.onPass ?? "approve") : String(tree.onFail ?? "reject"),
    conditions,
    groupResults,
  };
}

function generateExpression(nodes: RuleNodeRecord[], variables: VariableRecord[]): string {
  if (!nodes.length) {
    return "IF (?) → ?";
  }
  let logic = "AND";
  let outcome = "?";
  const conditions = nodes
    .filter((node) => node.type === "condition")
    .map((node) => {
      const variable = variables.find((item) => item.id === node.variable);
      return (variable?.name ?? node.variable ?? "Variable") + " " + (node.operator ?? "==") + " " + (node.value ?? "");
    });
  nodes.forEach((node) => {
    if (node.type === "or") {
      logic = "OR";
    }
    if (node.type === "and") {
      logic = "AND";
    }
    if (node.type === "approve" || node.type === "review" || node.type === "reject") {
      outcome = node.type.toUpperCase();
    }
  });
  return "IF (" + conditions.join(" " + logic + " ") + ") → " + outcome;
}

function simulateRuleDraft(nodes: RuleNodeRecord[], variables: VariableRecord[]): { passed: boolean; outcome: string; conditions: RuleConditionResult[] } {
  const conditions: RuleConditionResult[] = [];
  let useOr = false;
  let outcome = "reject";
  nodes.forEach((node) => {
    if (node.type === "or") {
      useOr = true;
    }
    if (node.type === "and") {
      useOr = false;
    }
    if (node.type === "approve" || node.type === "review" || node.type === "reject") {
      outcome = node.type;
    }
  });

  nodes
    .filter((node) => node.type === "condition")
    .forEach((node) => {
      const variable = variables.find((item) => item.id === node.variable);
      const actualValue = variable?.last_test_result?.value ?? 0;
      const expectedValue = Number(node.value ?? "0");
      let passed = false;
      if (node.operator === ">=") passed = Number(actualValue) >= expectedValue;
      if (node.operator === "<=") passed = Number(actualValue) <= expectedValue;
      if (node.operator === "==") passed = String(actualValue) === String(node.value ?? "");
      if (node.operator === ">") passed = Number(actualValue) > expectedValue;
      if (node.operator === "<") passed = Number(actualValue) < expectedValue;
      if (node.operator === "!=") passed = String(actualValue) !== String(node.value ?? "");
      conditions.push({
        variable_id: node.variable ?? "",
        variable_name: variable?.name ?? node.variable ?? "",
        source_id: variable?.source_id,
        operator: node.operator ?? "==",
        threshold: node.value ?? "",
        value: actualValue,
        passed,
      });
    });

  const passed = useOr ? conditions.some((condition) => condition.passed) : conditions.every((condition) => condition.passed);
  return { passed, outcome: passed ? outcome : "reject", conditions };
}

function AdvancedRuleTreeEditor(props: {
  node: RuleTreeNodeRecord;
  depth: number;
  isRoot?: boolean;
  variables: VariableRecord[];
  connectors: Record<string, ConnectorRecord>;
  onChange: (node: RuleTreeNodeRecord) => void;
  onRemove?: () => void;
  onMoveUp?: () => void;
  onMoveDown?: () => void;
}) {
  const theme = useTheme();
  const canAddChildren = props.depth < 3;

  const actionButtonStyle: React.CSSProperties = {
    border: "1px solid " + theme.border,
    background: theme.card,
    color: theme.muted,
    borderRadius: 8,
    width: 28,
    height: 28,
    display: "grid",
    placeItems: "center",
    cursor: "pointer",
  };

  if (props.node.type === "condition") {
    const variable = props.variables.find((item) => item.id === props.node.variable);
    const connector = variable ? props.connectors[variable.source_id] : undefined;
    return (
      <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 12, display: "grid", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <div style={{ fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.accent, letterSpacing: 0.7 }}>CONDITION</div>
          <div style={{ display: "flex", gap: 6 }}>
            {props.onMoveUp ? (
              <button type="button" onClick={props.onMoveUp} style={actionButtonStyle}>
                <ArrowUp size={14} />
              </button>
            ) : null}
            {props.onMoveDown ? (
              <button type="button" onClick={props.onMoveDown} style={actionButtonStyle}>
                <ArrowDown size={14} />
              </button>
            ) : null}
            {props.onRemove ? (
              <button type="button" onClick={props.onRemove} style={actionButtonStyle}>
                <Trash2 size={14} />
              </button>
            ) : null}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <InlineSelect
            value={props.node.variable ?? ""}
            onChange={(event) => props.onChange({ ...props.node, variable: event.target.value })}
            style={{ minWidth: 220 }}
          >
            {props.variables.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </InlineSelect>
          <InlineSelect
            value={props.node.operator ?? ">="}
            onChange={(event) => props.onChange({ ...props.node, operator: event.target.value as RuleNodeRecord["operator"] })}
            style={{ width: 104 }}
          >
            {RULE_OPERATORS.map((operator) => (
              <option key={operator.value} value={operator.value}>
                {operator.label}
              </option>
            ))}
          </InlineSelect>
          {props.node.operator !== "exists" && props.node.operator !== "!exists" ? (
            <InlineInput
              value={String(props.node.value ?? "")}
              onChange={(event) => props.onChange({ ...props.node, value: event.target.value })}
              placeholder={props.node.operator === "in" || props.node.operator === "not_in" ? "a, b, c" : props.node.operator === "regex" ? "pattern" : "Value"}
              style={{ width: 140 }}
            />
          ) : null}
          {props.node.operator === "between" ? (
            <InlineInput
              value={String(props.node.value2 ?? "")}
              onChange={(event) => props.onChange({ ...props.node, value2: event.target.value })}
              placeholder="Upper"
              style={{ width: 100 }}
            />
          ) : null}
        </div>
        <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted, display: "inline-flex", alignItems: "center", gap: 6 }}>
          <ConnectorIcon connectorId={connector?.id ?? "custom"} color={connector?.color} size={13} />
          <span>{connector?.name ?? "No source selected"}</span>
        </div>
      </div>
    );
  }

  if (props.node.type === "not") {
    return (
      <div style={{ background: theme.cardAlt, border: "1px solid " + theme.border, borderRadius: 12, padding: 12, display: "grid", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.warning }}>
            <CircleSlash size={14} />
            <span>NOT</span>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            {props.onMoveUp ? (
              <button type="button" onClick={props.onMoveUp} style={actionButtonStyle}>
                <ArrowUp size={14} />
              </button>
            ) : null}
            {props.onMoveDown ? (
              <button type="button" onClick={props.onMoveDown} style={actionButtonStyle}>
                <ArrowDown size={14} />
              </button>
            ) : null}
            {props.onRemove ? (
              <button type="button" onClick={props.onRemove} style={actionButtonStyle}>
                <Trash2 size={14} />
              </button>
            ) : null}
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <Button small onClick={() => props.onChange({ ...props.node, child: createTreeCondition(props.variables[0]) })}>
            Condition
          </Button>
          <Button small onClick={() => props.onChange({ ...props.node, child: createTreeGroup(props.variables[0]) })} disabled={!canAddChildren}>
            Group
          </Button>
        </div>
        <div style={{ paddingLeft: 12, borderLeft: "2px solid " + theme.border }}>
          <AdvancedRuleTreeEditor
            node={normalizeRuleTree(props.node.child, props.variables[0])}
            depth={props.depth + 1}
            variables={props.variables}
            connectors={props.connectors}
            onChange={(child) => props.onChange({ ...props.node, child })}
          />
        </div>
      </div>
    );
  }

  const children = props.node.children ?? [];
  return (
    <div style={{ background: props.isRoot ? theme.card : theme.cardAlt, border: "1px solid " + theme.border, borderRadius: 12, padding: 12, display: "grid", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.success }}>
            <GitBranch size={14} />
            <span>{props.isRoot ? "ROOT GROUP" : "GROUP"}</span>
          </div>
          <InlineSelect
            value={props.node.logic ?? "AND"}
            onChange={(event) => props.onChange({ ...props.node, logic: event.target.value as "AND" | "OR" })}
            style={{ width: 88 }}
          >
            <option value="AND">AND</option>
            <option value="OR">OR</option>
          </InlineSelect>
          {props.isRoot ? (
            <>
              <InlineSelect
                value={props.node.onPass ?? "approve"}
                onChange={(event) => props.onChange({ ...props.node, onPass: event.target.value as "approve" | "review" | "reject" })}
                style={{ width: 112 }}
              >
                <option value="approve">onPass: approve</option>
                <option value="review">onPass: review</option>
                <option value="reject">onPass: reject</option>
              </InlineSelect>
              <InlineSelect
                value={props.node.onFail ?? "reject"}
                onChange={(event) => props.onChange({ ...props.node, onFail: event.target.value as "approve" | "review" | "reject" })}
                style={{ width: 110 }}
              >
                <option value="approve">onFail: approve</option>
                <option value="review">onFail: review</option>
                <option value="reject">onFail: reject</option>
              </InlineSelect>
            </>
          ) : null}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          {props.onMoveUp ? (
            <button type="button" onClick={props.onMoveUp} style={actionButtonStyle}>
              <ArrowUp size={14} />
            </button>
          ) : null}
          {props.onMoveDown ? (
            <button type="button" onClick={props.onMoveDown} style={actionButtonStyle}>
              <ArrowDown size={14} />
            </button>
          ) : null}
          {props.onRemove ? (
            <button type="button" onClick={props.onRemove} style={actionButtonStyle}>
              <Trash2 size={14} />
            </button>
          ) : null}
        </div>
      </div>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <Button small onClick={() => props.onChange({ ...props.node, children: [...children, createTreeCondition(props.variables[0])] })} disabled={!canAddChildren}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Plus size={12} />
            <span>Condition</span>
          </span>
        </Button>
        <Button small onClick={() => props.onChange({ ...props.node, children: [...children, createTreeGroup(props.variables[0])] })} disabled={!canAddChildren}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Plus size={12} />
            <span>Group</span>
          </span>
        </Button>
        <Button small onClick={() => props.onChange({ ...props.node, children: [...children, createTreeNot(props.variables[0])] })} disabled={!canAddChildren}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Plus size={12} />
            <span>NOT</span>
          </span>
        </Button>
      </div>

      <div style={{ display: "grid", gap: 10, paddingLeft: props.isRoot ? 0 : 12, borderLeft: props.isRoot ? "none" : "2px solid " + theme.border }}>
        {children.map((child, index) => (
          <AdvancedRuleTreeEditor
            key={child.id ?? "child_" + index}
            node={child}
            depth={props.depth + 1}
            variables={props.variables}
            connectors={props.connectors}
            onChange={(updatedChild) =>
              props.onChange({
                ...props.node,
                children: children.map((item, itemIndex) => (itemIndex === index ? updatedChild : item)),
              })
            }
            onRemove={() => props.onChange({ ...props.node, children: children.filter((_, itemIndex) => itemIndex !== index).length ? children.filter((_, itemIndex) => itemIndex !== index) : [createTreeCondition(props.variables[0])] })}
            onMoveUp={index > 0 ? () => props.onChange({ ...props.node, children: children.map((item, itemIndex) => itemIndex === index ? children[index - 1] : itemIndex === index - 1 ? children[index] : item) }) : undefined}
            onMoveDown={index < children.length - 1 ? () => props.onChange({ ...props.node, children: children.map((item, itemIndex) => itemIndex === index ? children[index + 1] : itemIndex === index + 1 ? children[index] : item) }) : undefined}
          />
        ))}
      </div>
    </div>
  );
}

export function RulesPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, environment, isMobile } = useRuleMindStore();
  const connectorMap = React.useMemo(
    () => Object.fromEntries(props.data.connectors.map((connector) => [connector.id, connector])),
    [props.data.connectors]
  );
  const environmentRules = React.useMemo(
    () => props.data.rules.filter((rule) => rule.status === environment),
    [environment, props.data.rules]
  );
  const activeVariables = React.useMemo(
    () => props.data.variables.filter((variable) => connectorMap[variable.source_id]?.is_active),
    [connectorMap, props.data.variables]
  );
  const [tab, setTab] = React.useState<"builder" | "saved" | "test">("builder");
  const [selectedRuleId, setSelectedRuleId] = React.useState<string | null>(environmentRules[0]?.id ?? null);
  const [ruleName, setRuleName] = React.useState("");
  const [builderMode, setBuilderMode] = React.useState<RuleBuilderMode>("simple");
  const [nodes, setNodes] = React.useState<RuleNodeRecord[]>([]);
  const [ruleTree, setRuleTree] = React.useState<RuleTreeNodeRecord>(() => createTreeGroup(activeVariables[0]));
  const [inlineTest, setInlineTest] = React.useState<RuleTestResponse["result"] | null>(null);
  const [ruleTestResult, setRuleTestResult] = React.useState<RuleTestResponse | null>(null);
  const [savedSearch, setSavedSearch] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    const current = selectedRuleId ? props.data.rules.find((rule) => rule.id === selectedRuleId) : null;
    if (current) {
      const nextMode: RuleBuilderMode = current.rule_format === "v2" || Boolean(current.tree) ? "advanced" : "simple";
      setBuilderMode(nextMode);
      setRuleName(current.name);
      setNodes(current.nodes ?? []);
      setRuleTree(normalizeRuleTree(current.tree ?? simpleNodesToTree(current.nodes ?? [], activeVariables[0]), activeVariables[0]));
      setInlineTest(current.last_test_result ?? null);
      return;
    }
    setRuleName("New Rule");
    setNodes([]);
    setRuleTree(createTreeGroup(activeVariables[0]));
    setInlineTest(null);
  }, [activeVariables, props.data.rules, selectedRuleId]);

  const activeNodeCount = builderMode === "advanced" ? countRuleTreeNodes(ruleTree) : nodes.length;

  const switchBuilderMode = React.useCallback(
    (nextMode: RuleBuilderMode) => {
      if (nextMode === builderMode) {
        return;
      }
      if (nextMode === "advanced") {
        setRuleTree(normalizeRuleTree(simpleNodesToTree(nodes, activeVariables[0]), activeVariables[0]));
        setBuilderMode("advanced");
        setInlineTest(null);
        return;
      }
      if (!window.confirm("Switching to simple mode will flatten nested groups into a compatibility rule and may change NOT / nested logic structure. Continue?")) {
        return;
      }
      setNodes(flattenTreeToNodes(ruleTree));
      setBuilderMode("simple");
      setInlineTest(null);
    },
    [activeVariables, builderMode, nodes, ruleTree]
  );

  const saveRule = React.useCallback(async () => {
    setBusy(true);
    try {
      const selectedRule = selectedRuleId ? props.data.rules.find((rule) => rule.id === selectedRuleId) : null;
      const payload =
        builderMode === "advanced"
          ? {
              name: ruleName || "Untitled Rule",
              nodes: flattenTreeToNodes(ruleTree),
              tree: ruleTree,
              ruleFormat: "v2",
              status: selectedRule?.status ?? environment,
            }
          : {
              name: ruleName || "Untitled Rule",
              nodes,
              ruleFormat: "v1",
              status: selectedRule?.status ?? environment,
            };
      const response = selectedRuleId
        ? await apiJson<RuleRecord>(apiBaseUrl, "/api/v1/rules/" + selectedRuleId, { method: "PUT", body: JSON.stringify(payload) }, apiKey)
        : await apiJson<RuleRecord>(apiBaseUrl, "/api/v1/rules", { method: "POST", body: JSON.stringify(payload) }, apiKey);
      setSelectedRuleId(response.id);
      props.refresh();
      props.onNotify("Rule saved.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Rule save failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, builderMode, environment, nodes, props, ruleName, ruleTree, selectedRuleId]);

  const testDraft = React.useCallback(() => {
    if (builderMode === "advanced") {
      const result = simulateRuleTreeDraft(ruleTree, props.data.variables);
      setInlineTest({
        passed: result.passed,
        outcome: result.outcome,
        conditions: result.conditions,
        groupResults: result.groupResults,
        latency_ms: 0,
        tested_at: new Date().toISOString(),
      });
      return;
    }
    const result = simulateRuleDraft(nodes, props.data.variables);
    setInlineTest({
      passed: result.passed,
      outcome: result.outcome,
      conditions: result.conditions,
      groupResults: [],
      latency_ms: 0,
      tested_at: new Date().toISOString(),
    });
  }, [builderMode, nodes, props.data.variables, ruleTree]);

  const runSavedRuleTest = React.useCallback(async (ruleId: string) => {
    setBusy(true);
    try {
      const response = await apiJson<RuleTestResponse>(apiBaseUrl, "/api/v1/test/rule/" + ruleId, { method: "POST", body: JSON.stringify({ payload: {} }) }, apiKey);
      setRuleTestResult(response);
      props.refresh();
      props.onNotify("Rule executed.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Rule test failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props]);

  const promoteRule = React.useCallback(async (ruleId: string) => {
    if (!window.confirm("Promote this rule to the next environment?")) {
      return;
    }
    setBusy(true);
    try {
      await apiJson<RuleRecord>(apiBaseUrl, "/api/v1/rules/" + ruleId + "/promote", { method: "POST", body: JSON.stringify({ promoted_by: "web", reason: "Manual promotion from Rules page" }) }, apiKey);
      props.refresh();
      props.onNotify("Rule promoted.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Rule promotion failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props]);

  const filteredSavedRules = React.useMemo(() => {
    const query = savedSearch.trim().toLowerCase();
    if (!query) {
      return environmentRules;
    }
    return environmentRules.filter((rule) => {
      const sourceNames = ruleVariableIds(rule).map(
        (variableId) => connectorMap[props.data.variables.find((variable) => variable.id === variableId)?.source_id ?? ""]?.name ?? ""
      );
      return rule.name.toLowerCase().includes(query) || sourceNames.some((source) => source.toLowerCase().includes(query));
    });
  }, [connectorMap, environmentRules, props.data.variables, savedSearch]);

  const expressionPreview = builderMode === "advanced" ? treeExpression(ruleTree, props.data.variables) : generateExpression(nodes, props.data.variables);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 120px)" }}>
      <div style={{ display: "flex", gap: 18, padding: "0 20px", borderBottom: "1px solid " + theme.border }}>
        {[
          { id: "builder", label: "Builder" },
          { id: "saved", label: "Saved Rules" },
          { id: "test", label: "Test Console" },
        ].map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id as "builder" | "saved" | "test")}
            style={{
              background: "none",
              border: "none",
              borderBottom: tab === item.id ? "2px solid " + theme.accent : "2px solid transparent",
              color: tab === item.id ? theme.accent : theme.muted,
              padding: "12px 0",
              fontSize: "var(--rm-fs-body)",
              fontWeight: "var(--rm-fw-bold)" as unknown as number,
              cursor: "pointer",
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "builder" ? (
        <div style={{ display: "grid", gridTemplateRows: "auto auto 1fr auto auto", flex: 1 }}>
          <div style={{ padding: 16, borderBottom: "1px solid " + theme.border, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <InlineInput value={ruleName} onChange={(event) => setRuleName(event.target.value)} style={{ minWidth: 220 }} data-testid="rule-name" />
              <span style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{activeNodeCount} nodes</span>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <div style={{ display: "inline-flex", gap: 6, background: theme.hover, padding: 4, borderRadius: 10 }}>
                <button
                  type="button"
                  onClick={() => switchBuilderMode("simple")}
                  style={{
                    border: "none",
                    background: builderMode === "simple" ? theme.card : "transparent",
                    color: builderMode === "simple" ? theme.text : theme.muted,
                    padding: "6px 10px",
                    borderRadius: 8,
                    cursor: "pointer",
                    fontSize: "var(--rm-fs-small)",
                    fontWeight: "var(--rm-fw-bold)" as unknown as number,
                  }}
                >
                  Simple
                </button>
                <button
                  type="button"
                  onClick={() => switchBuilderMode("advanced")}
                  style={{
                    border: "none",
                    background: builderMode === "advanced" ? theme.card : "transparent",
                    color: builderMode === "advanced" ? theme.text : theme.muted,
                    padding: "6px 10px",
                    borderRadius: 8,
                    cursor: "pointer",
                    fontSize: "var(--rm-fs-small)",
                    fontWeight: "var(--rm-fw-bold)" as unknown as number,
                  }}
                >
                  Advanced
                </button>
              </div>
              <Button
                small
                variant="ghost"
                onClick={() => {
                  setSelectedRuleId(null);
                  setRuleName("New Rule");
                  setNodes([]);
                  setRuleTree(createTreeGroup(activeVariables[0]));
                  setInlineTest(null);
                }}
                testId="rule-clear"
              >
                Clear
              </Button>
              <Button small onClick={testDraft} disabled={activeNodeCount === 0} testId="rule-test">
                Test
              </Button>
              <Button small variant="primary" onClick={saveRule} disabled={activeNodeCount === 0 || busy} testId="rule-save">
                Save
              </Button>
            </div>
          </div>

          <div style={{ padding: "12px 16px", borderBottom: "1px solid " + theme.border, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {builderMode === "simple" ? (
              NODE_TYPES.map((nodeType) => {
                const palette = tone(theme, nodeType.colorKey);
                return (
                  <button
                    key={nodeType.type}
                    type="button"
                    data-testid={"rule-add-" + nodeType.type}
                    onClick={() => setNodes((items) => [...items, cloneNode(nodeType.type, activeVariables[0])])}
                    style={{
                      border: "1px solid " + palette.fg + "30",
                      background: palette.bg,
                      color: palette.fg,
                      borderRadius: 10,
                      padding: "6px 10px",
                      fontSize: "var(--rm-fs-small)",
                      fontWeight: "var(--rm-fw-bold)" as unknown as number,
                      cursor: "pointer",
                    }}
                  >
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <nodeType.icon size={14} color={palette.fg} strokeWidth={2} />
                      <span>{nodeType.label}</span>
                    </span>
                  </button>
                );
              })
            ) : (
              <>
                <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>
                  Nested groups support <strong style={{ color: theme.text }}>AND</strong>, <strong style={{ color: theme.text }}>OR</strong>, and <strong style={{ color: theme.text }}>NOT</strong> with a max depth of 3.
                </div>
                <Button small variant="ghost" onClick={() => setRuleTree(createTreeGroup(activeVariables[0]))}>
                  Reset tree
                </Button>
              </>
            )}
          </div>

          <div style={{ padding: 16, overflow: "auto" }}>
            {builderMode === "simple" && !nodes.length ? (
              <EmptyState
                icon={<Workflow size={28} />}
                title="Click to add rule nodes"
                description="Use conditions, logic, and outcomes to build a decision without drag-and-drop."
              />
            ) : (
              <>
                {builderMode === "simple" ? (
                  <div style={{ display: "grid", gap: 8 }}>
                    {nodes.map((node, index) => {
                      const spec = NODE_TYPES.find((item) => item.type === node.type)!;
                      const palette = tone(theme, spec.colorKey);
                      return (
                        <div key={node.id} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          {index > 0 ? <div style={{ width: 18, height: 2, background: theme.border }} /> : null}
                          <div style={{ flex: 1, background: theme.card, border: "1px solid " + palette.fg + "30", borderRadius: 12, padding: 10, display: "flex", alignItems: "center", gap: 10 }}>
                            <div style={{ width: 28, height: 28, borderRadius: 8, background: palette.bg, color: palette.fg, display: "grid", placeItems: "center", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>
                              <spec.icon size={15} color={palette.fg} strokeWidth={2} />
                            </div>
                            {node.type === "condition" ? (
                              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", flex: 1 }}>
                                <InlineSelect
                                  value={node.variable ?? ""}
                                  onChange={(event) => setNodes((items) => items.map((item) => (item.id === node.id ? { ...item, variable: event.target.value } : item)))}
                                  style={{ minWidth: 180 }}
                                  testId={"rule-condition-variable-" + node.id}
                                >
                                  {activeVariables.map((variable) => (
                                    <option key={variable.id} value={variable.id}>
                                      {variable.name}
                                    </option>
                                  ))}
                                </InlineSelect>
                                <InlineSelect
                                  value={node.operator ?? ">="}
                                  onChange={(event) => setNodes((items) => items.map((item) => (item.id === node.id ? { ...item, operator: event.target.value as RuleNodeRecord["operator"] } : item)))}
                                  style={{ width: 96 }}
                                >
                                  {RULE_OPERATORS.map((operator) => (
                                    <option key={operator.value} value={operator.value}>
                                      {operator.label}
                                    </option>
                                  ))}
                                </InlineSelect>
                                {node.operator !== "exists" && node.operator !== "!exists" ? (
                                  <InlineInput
                                    value={node.value ?? ""}
                                    onChange={(event) => setNodes((items) => items.map((item) => (item.id === node.id ? { ...item, value: event.target.value } : item)))}
                                    placeholder={node.operator === "in" || node.operator === "not_in" ? "a, b, c" : node.operator === "regex" ? "pattern" : "Value"}
                                    style={{ width: 110 }}
                                  />
                                ) : null}
                                {node.operator === "between" ? (
                                  <InlineInput
                                    value={node.value2 ?? ""}
                                    onChange={(event) => setNodes((items) => items.map((item) => (item.id === node.id ? { ...item, value2: event.target.value } : item)))}
                                    placeholder="Upper"
                                    style={{ width: 90 }}
                                  />
                                ) : null}
                              </div>
                            ) : (
                              <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: palette.fg, flex: 1 }}>{spec.label}</div>
                            )}
                            <button
                              type="button"
                              onClick={() => setNodes((items) => items.filter((item) => item.id !== node.id))}
                              style={{ border: "none", background: "transparent", color: theme.dim, cursor: "pointer", fontSize: "var(--rm-fs-heading)" }}
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <AdvancedRuleTreeEditor
                    node={ruleTree}
                    depth={0}
                    isRoot
                    variables={activeVariables}
                    connectors={connectorMap}
                    onChange={(nextTree) => setRuleTree(normalizeRuleTree(nextTree, activeVariables[0]))}
                  />
                )}
              </>
            )}
          </div>

          <div style={{ padding: "12px 16px", borderTop: "1px solid " + theme.border }}>
            <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.dim, marginBottom: 6, letterSpacing: 1.2, textTransform: "uppercase" }}>Expression</div>
            <div data-testid="rule-expression" style={{ background: theme.hover, borderRadius: 10, padding: "10px 12px", fontFamily: "var(--font-mono)", color: theme.accent, fontSize: "var(--rm-fs-body)" }}>
              {expressionPreview}
            </div>
          </div>

          {inlineTest ? (
            <div style={{ padding: "12px 16px", borderTop: "1px solid " + theme.border, background: inlineTest.passed ? theme.successBg : theme.dangerBg }}>
              <div data-testid="rule-inline-outcome" style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: inlineTest.passed ? theme.success : theme.danger, textTransform: "uppercase", marginBottom: 8 }}>
                {inlineTest.outcome}
              </div>
              <div style={{ display: "grid", gap: 4 }}>
                {inlineTest.conditions.map((condition) => (
                  <div key={condition.variable_id + condition.threshold} style={{ fontSize: "var(--rm-fs-small)", color: condition.passed ? theme.success : theme.danger }}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      {condition.passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                      <span>
                        {condition.variable_name}: {String(condition.value)} {condition.operator} {condition.threshold}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
              {inlineTest.groupResults?.length ? (
                <div style={{ marginTop: 10, display: "grid", gap: 4 }}>
                  {inlineTest.groupResults.map((group) => (
                    <div key={String(group.id) + group.logic} style={{ fontSize: "var(--rm-fs-small)", color: group.passed ? theme.success : theme.danger }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                        {group.passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                        <span>{group.logic} group · {group.childCount} child node(s)</span>
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "saved" ? (
        <div style={{ padding: 20, overflow: "auto", display: "grid", gap: 10 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <InlineInput value={savedSearch} onChange={(event) => setSavedSearch(event.target.value)} placeholder="Search rules or source names" style={{ maxWidth: 320 }} />
            <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{filteredSavedRules.length} shown</div>
          </div>
          {filteredSavedRules.length === 0 ? (
            <EmptyState icon={<Layers size={28} />} title="No saved rules" description={"Create and save a rule in " + environment.toUpperCase() + " to manage it here."} />
          ) : (
            filteredSavedRules.map((rule) => {
              const usedSourceIds = Array.from(
                new Set(ruleVariableIds(rule).map((variableId) => props.data.variables.find((variable) => variable.id === variableId)?.source_id).filter(Boolean))
              );
              return (
                <div key={rule.id} style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <div style={{ display: "grid", gap: 6 }}>
                    <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text, display: "inline-flex", gap: 8, alignItems: "center" }}>
                      <span>{rule.name}</span>
                      <span style={{ fontSize: "var(--rm-fs-caption)", color: theme.dim, textTransform: "uppercase" }}>{rule.rule_format ?? "v1"}</span>
                    </div>
                    <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{rule.tree ? countRuleTreeNodes(rule.tree) : rule.nodes.length} nodes</div>
                    <div style={{ display: "flex", gap: 6 }}>
                      {usedSourceIds.map((sourceId) => (
                        <ConnectorIcon key={String(sourceId)} connectorId={String(sourceId)} color={connectorMap[String(sourceId)]?.color} size={14} />
                      ))}
                    </div>
                    {!rule.last_test_result?.passed ? (
                      <div style={{ fontSize: "var(--rm-fs-small)", color: theme.warning }}>Latest test must pass before promotion.</div>
                    ) : null}
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <StatusBadge status={rule.status} />
                    {rule.status !== "prod" ? (
                      <Button small variant="success" onClick={() => promoteRule(rule.id)} disabled={!rule.last_test_result?.passed}>
                        Promote
                      </Button>
                    ) : null}
                    <Button
                      small
                      onClick={() => {
                        setSelectedRuleId(rule.id);
                        setTab("builder");
                      }}
                    >
                      Edit
                    </Button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      ) : null}

      {tab === "test" ? (
        <div style={{ padding: 20, overflow: "auto", display: "grid", gap: 12 }}>
          <SectionHeader title="Saved Rule Test" subtitle="Execute a persisted rule against the active connector samples." />
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <InlineSelect value={selectedRuleId ?? ""} onChange={(event) => setSelectedRuleId(event.target.value || null)} style={{ minWidth: 280 }} data-testid="rules-test-select">
              <option value="">Select a rule</option>
              {environmentRules.map((rule) => (
                <option key={rule.id} value={rule.id}>
                  {rule.name}
                </option>
              ))}
            </InlineSelect>
            <Button small variant="primary" disabled={!selectedRuleId || busy} onClick={() => (selectedRuleId ? runSavedRuleTest(selectedRuleId) : undefined)} testId="rules-test-run">
              Execute
            </Button>
          </div>
          {ruleTestResult ? (
            <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
              <div style={{ padding: 14, background: ruleTestResult.result.passed ? theme.successBg : theme.dangerBg }}>
                <div data-testid="rule-saved-outcome" style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: ruleTestResult.result.passed ? theme.success : theme.danger, textTransform: "uppercase" }}>
                  {ruleTestResult.result.outcome}
                </div>
              </div>
              <div style={{ padding: 14, display: "grid", gap: 8 }}>
                {ruleTestResult.result.conditions.map((condition) => (
                  <div key={condition.variable_id + condition.threshold} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "var(--rm-fs-body)", color: theme.text }}>
                    <span style={{ color: condition.passed ? theme.success : theme.danger }}>{condition.passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}</span>
                    <ConnectorIcon connectorId={condition.source_id ?? "custom"} color={connectorMap[condition.source_id ?? ""]?.color} size={12} />
                    <span>
                      {condition.variable_name}: {String(condition.value)} {condition.operator} {condition.threshold}
                    </span>
                  </div>
                ))}
                {ruleTestResult.result.groupResults?.length ? (
                  <div style={{ marginTop: 6, display: "grid", gap: 4 }}>
                    {ruleTestResult.result.groupResults.map((group) => (
                      <div key={String(group.id) + group.logic} style={{ fontSize: "var(--rm-fs-small)", color: group.passed ? theme.success : theme.danger }}>
                        {group.logic} group · {group.passed ? "PASS" : "FAIL"} · {group.childCount} child node(s)
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          ) : (
            <EmptyState icon={<FileSearch size={28} />} title="No test result yet" description="Select a rule and execute it to inspect the per-condition breakdown." />
          )}
        </div>
      ) : null}
    </div>
  );
}

