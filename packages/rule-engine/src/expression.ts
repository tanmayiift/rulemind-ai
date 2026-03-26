import type { RuleConnection, RuleDefinition, RuleNode } from "@rulemind/shared";

function buildChildMap(connections: RuleConnection[]): Record<string, string[]> {
  return connections.reduce<Record<string, string[]>>((acc, connection) => {
    const current = acc[connection.from] ?? [];
    current.push(connection.to);
    acc[connection.from] = current;
    return acc;
  }, {});
}

function findNode(nodes: RuleNode[], id: string): RuleNode | undefined {
  return nodes.find((node) => node.id === id);
}

function formatNode(
  nodes: RuleNode[],
  childMap: Record<string, string[]>,
  nodeId: string,
  depth = 0
): string | null {
  const node = findNode(nodes, nodeId);

  if (!node) {
    return null;
  }

  const padding = "  ".repeat(depth);
  const config = node.config ?? {};

  if (node.type === "condition" || node.type === "score") {
    const field = config.field ?? "unknown_field";
    const operator = config.operator ?? "==";

    if (operator === "exists" || operator === "!exists") {
      return `${padding}${field} ${String(operator).toUpperCase()}`;
    }

    if (operator === "between") {
      return `${padding}${field} BETWEEN ${String(config.value ?? "?")} AND ${String(config.value2 ?? "?")}`;
    }

    if (operator === "in" || operator === "not_in") {
      return `${padding}${field} ${operator === "in" ? "IN" : "NOT IN"} (${String(config.value ?? "?")})`;
    }

    if (operator === "regex") {
      return `${padding}${field} MATCHES /${String(config.value ?? "?")}/`;
    }

    const right =
      config.fieldType === "string" ? `"${String(config.value ?? "")}"` : String(config.value ?? "?");

    return `${padding}${field} ${operator} ${right}`;
  }

  if (node.type === "and" || node.type === "or" || node.type === "group") {
    const children = (childMap[nodeId] ?? []).map((childId) => formatNode(nodes, childMap, childId, depth + 1)).filter(Boolean);
    const operator = node.type === "group" ? String(config.groupOp ?? "AND") : node.type.toUpperCase();

    if (children.length === 0) {
      return `${padding}${operator} ( )`;
    }

    return `${padding}${operator} (\n${children.join(",\n")}\n${padding})`;
  }

  if (node.type === "not") {
    const children = (childMap[nodeId] ?? []).map((childId) => formatNode(nodes, childMap, childId, depth + 1)).filter(Boolean);
    return `${padding}NOT (\n${children.join(",\n")}\n${padding})`;
  }

  if (node.type === "trigger") {
    const event = config.event ?? "on_request";
    const children = (childMap[nodeId] ?? []).map((childId) => formatNode(nodes, childMap, childId, depth + 1)).filter(Boolean);
    return children.length > 0 ? `${padding}WHEN ${event} {\n${children.join("\n")}\n${padding}}` : `${padding}WHEN ${event}`;
  }

  if (node.type === "approve" || node.type === "review" || node.type === "reject") {
    const reason = config.reason ? ` "${String(config.reason)}"` : "";
    return `${padding}=> ${node.type.toUpperCase()}${reason}`;
  }

  return null;
}

export function generateExpression(definition: RuleDefinition): string {
  if (definition.nodes.length === 0) {
    return "// Empty — drag nodes onto the canvas to begin";
  }

  const childMap = buildChildMap(definition.connections);
  const incoming = new Set(definition.connections.map((connection) => connection.to));
  const roots = definition.nodes.filter((node) => !incoming.has(node.id));
  const sourceNodes = roots.length > 0 ? roots : definition.nodes;
  const lines = ["// Auto-generated rule expression"];

  sourceNodes.forEach((node) => {
    const fragment = formatNode(definition.nodes, childMap, node.id);

    if (fragment) {
      lines.push(fragment);
    }
  });

  return lines.join("\n");
}
