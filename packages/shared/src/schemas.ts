import { z } from "zod";
import { assetKinds, environments, fieldTypes, nodeTypes, operators } from "./types";

export const environmentSchema = z.enum(environments);
export const nodeTypeSchema = z.enum(nodeTypes);
export const operatorSchema = z.enum(operators);
export const fieldTypeSchema = z.enum(fieldTypes);

export const ruleNodeSchema = z.object({
  id: z.string().min(1),
  type: nodeTypeSchema,
  label: z.string().min(1),
  x: z.number(),
  y: z.number(),
  config: z
    .object({
      field: z.string().optional(),
      fieldType: fieldTypeSchema.optional(),
      operator: operatorSchema.optional(),
      value: z.unknown().optional(),
      value2: z.unknown().optional(),
      event: z.string().optional(),
      reason: z.string().optional(),
      groupOp: z.enum(["AND", "OR"]).optional()
    })
    .catchall(z.unknown())
});

export const ruleConnectionSchema = z.object({
  from: z.string().min(1),
  to: z.string().min(1)
});

export const ruleDefinitionSchema = z.object({
  nodes: z.array(ruleNodeSchema),
  connections: z.array(ruleConnectionSchema),
  metadata: z
    .object({
      name: z.string().optional(),
      description: z.string().optional(),
      environment: environmentSchema.optional(),
      tags: z.array(z.string()).optional(),
      version: z.number().int().positive().optional(),
      createdAt: z.string().optional(),
      updatedAt: z.string().optional()
    })
    .optional()
});

export const ruleCreateSchema = z.object({
  name: z.string().min(1),
  environment: environmentSchema.default("dev"),
  tags: z.array(z.string()).default([]),
  definition: ruleDefinitionSchema,
  createdBy: z.string().default("system")
});

export const ruleUpdateSchema = z.object({
  name: z.string().min(1).optional(),
  environment: environmentSchema.optional(),
  tags: z.array(z.string()).optional(),
  definition: ruleDefinitionSchema.optional(),
  updatedBy: z.string().default("system"),
  summary: z.string().optional()
});

export const ruleListFiltersSchema = z.object({
  environment: environmentSchema.optional(),
  tag: z.string().optional(),
  active: z
    .union([z.boolean(), z.enum(["true", "false"])])
    .optional()
    .transform((value) => {
      if (typeof value === "boolean") {
        return value;
      }

      if (value === undefined) {
        return value;
      }

      return value === "true";
    })
});

export const evaluationRequestSchema = z.object({
  input: z.record(z.string(), z.unknown()),
  evaluatedBy: z.string().default("system"),
  variables: z.record(z.string(), z.unknown()).optional()
});

export const batchEvaluationRequestSchema = z.object({
  items: z.array(z.record(z.string(), z.unknown())).min(1),
  evaluatedBy: z.string().default("system")
});

export const rollbackSchema = z.object({
  version: z.number().int().positive(),
  reason: z.string().min(1).default("rollback")
});

export const promoteSchema = z.object({
  toEnvironment: environmentSchema,
  promotedBy: z.string().default("system")
});

export const assetKindSchema = z.enum(assetKinds);

export const managedAssetSchema = z.object({
  name: z.string().min(1),
  description: z.string().optional(),
  environment: environmentSchema.default("dev"),
  tags: z.array(z.string()).default([]),
  definition: z.record(z.string(), z.unknown()),
  metadata: z.record(z.string(), z.unknown()).default({})
});

export const approvalDecisionSchema = z.object({
  comment: z.string().optional(),
  checkerId: z.string().min(1)
});

export const apiKeyCreateSchema = z.object({
  name: z.string().min(1),
  environment: environmentSchema.default("dev"),
  permissions: z.array(z.string()).min(1),
  expiresAt: z.string().optional(),
  rateLimit: z.number().int().positive().default(600)
});

export const variableTestSchema = z.object({
  payload: z.record(z.string(), z.unknown()).default({}),
  variables: z.record(z.string(), z.unknown()).default({})
});

export const outcomeImportSchema = z.object({
  source: z.string().min(1),
  records: z.array(z.record(z.string(), z.unknown())).default([])
});
