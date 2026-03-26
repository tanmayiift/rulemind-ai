import type { RuleConnection, RuleDefinition, RuleNode } from "@rulemind/shared";
import { validateRuleDefinition } from "./validator";

export interface CompiledRule {
  nodeMap: Map<string, RuleNode>;
  childMap: Map<string, string[]>;
  parentMap: Map<string, string[]>;
  roots: string[];
}

function buildMap<T extends RuleNode | RuleConnection>(items: T[], key: keyof T): Map<string, T> {
  return new Map(items.map((item) => [String(item[key]), item]));
}

export function compileRule(definition: RuleDefinition): CompiledRule {
  const issues = validateRuleDefinition(definition).filter((issue) => issue.level === "error");

  if (issues.length > 0) {
    throw new Error(`Rule validation failed: ${issues.map((issue) => issue.message).join("; ")}`);
  }

  const nodeMap = buildMap(definition.nodes, "id");
  const childMap = new Map<string, string[]>();
  const parentMap = new Map<string, string[]>();

  definition.connections.forEach((connection) => {
    const children = childMap.get(connection.from) ?? [];
    children.push(connection.to);
    childMap.set(connection.from, children);

    const parents = parentMap.get(connection.to) ?? [];
    parents.push(connection.from);
    parentMap.set(connection.to, parents);
  });

  const roots = definition.nodes
    .filter((node) => !parentMap.has(node.id))
    .map((node) => node.id);

  return {
    nodeMap,
    childMap,
    parentMap,
    roots
  };
}
