import type { RuleDefinition, ValidationIssue } from "@rulemind/shared";
import { operators } from "@rulemind/shared";

function hasDuplicateIds(definition: RuleDefinition): string[] {
  const ids = new Set<string>();
  const duplicates: string[] = [];

  definition.nodes.forEach((node) => {
    if (ids.has(node.id)) {
      duplicates.push(node.id);
      return;
    }

    ids.add(node.id);
  });

  return duplicates;
}

function detectCycles(definition: RuleDefinition): boolean {
  const children = definition.connections.reduce<Record<string, string[]>>((acc, connection) => {
    const current = acc[connection.from] ?? [];
    current.push(connection.to);
    acc[connection.from] = current;
    return acc;
  }, {});

  const visited = new Set<string>();
  const inPath = new Set<string>();

  const visit = (nodeId: string): boolean => {
    if (inPath.has(nodeId)) {
      return true;
    }

    if (visited.has(nodeId)) {
      return false;
    }

    visited.add(nodeId);
    inPath.add(nodeId);

    for (const child of children[nodeId] ?? []) {
      if (visit(child)) {
        return true;
      }
    }

    inPath.delete(nodeId);
    return false;
  };

  return definition.nodes.some((node) => visit(node.id));
}

export function validateRuleDefinition(definition: RuleDefinition): ValidationIssue[] {
  const issues: ValidationIssue[] = [];

  if (definition.nodes.length === 0) {
    return [{ level: "warn", message: "Rule has no nodes." }];
  }

  const duplicateIds = hasDuplicateIds(definition);

  duplicateIds.forEach((duplicateId) => {
    issues.push({
      level: "error",
      message: `Duplicate node id detected: ${duplicateId}`,
      nodeId: duplicateId
    });
  });

  const nodeIds = new Set(definition.nodes.map((node) => node.id));

  definition.connections.forEach((connection) => {
    if (!nodeIds.has(connection.from) || !nodeIds.has(connection.to)) {
      issues.push({
        level: "error",
        message: `Dangling connection ${connection.from} -> ${connection.to}`,
        connection
      });
    }

    if (connection.from === connection.to) {
      issues.push({
        level: "error",
        message: `Self reference detected on ${connection.from}`,
        connection
      });
    }
  });

  const outcomeCount = definition.nodes.filter((node) => node.type === "approve" || node.type === "review" || node.type === "reject").length;

  if (outcomeCount === 0) {
    issues.push({
      level: "error",
      message: "No outcome node present. Add Approve, Review, or Reject."
    });
  }

  definition.nodes.forEach((node) => {
    if (node.type === "condition" || node.type === "score") {
      if (!node.config.field) {
        issues.push({
          level: "warn",
          message: `${node.label}: missing field name`,
          nodeId: node.id
        });
      }

      if (node.config.operator && !operators.includes(node.config.operator as (typeof operators)[number])) {
        issues.push({
          level: "error",
          message: `${node.label}: invalid operator ${String(node.config.operator)}`,
          nodeId: node.id
        });
      }

      if (node.config.operator === "between" && (node.config.value === undefined || node.config.value2 === undefined)) {
        issues.push({
          level: "warn",
          message: `${node.label}: between requires value and value2`,
          nodeId: node.id
        });
      }
    }

    if (node.type === "group" && node.config.groupOp !== undefined && node.config.groupOp !== "AND" && node.config.groupOp !== "OR") {
      issues.push({
        level: "error",
        message: `${node.label}: groupOp must be AND or OR`,
        nodeId: node.id
      });
    }
  });

  if (detectCycles(definition)) {
    issues.push({
      level: "error",
      message: "Circular reference detected."
    });
  }

  const incoming = new Set(definition.connections.map((connection) => connection.to));
  const outgoing = new Set(definition.connections.map((connection) => connection.from));
  const orphans = definition.nodes.filter((node) => !incoming.has(node.id) && !outgoing.has(node.id));

  if (orphans.length > 0 && definition.nodes.length > 1) {
    issues.push({
      level: "warn",
      message: `${orphans.length} disconnected node(s) detected.`
    });
  }

  return issues;
}
