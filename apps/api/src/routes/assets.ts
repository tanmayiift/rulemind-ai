import { randomUUID } from "node:crypto";
import type { FastifyInstance } from "fastify";
import { evaluateScorecard, resolvePolicy } from "@rulemind/rule-engine";
import {
  assetKindSchema,
  managedAssetSchema,
  outcomeImportSchema,
  variableTestSchema
} from "@rulemind/shared";
import { requirePermission } from "../auth/middleware";

const resources = [
  { path: "variables", kind: "variable" },
  { path: "connectors", kind: "connector" },
  { path: "decision-tables", kind: "decision_table" },
  { path: "scorecards", kind: "scorecard" },
  { path: "policies", kind: "policy" },
  { path: "segments", kind: "segment" },
  { path: "ab-tests", kind: "ab_test" },
  { path: "workflows", kind: "workflow" },
  { path: "exports", kind: "export" }
] as const;

async function callPythonExecutor(baseUrl: string, path: string, payload: Record<string, unknown>) {
  const response = await fetch(`${baseUrl}${path}`, {
    method: "POST",
    headers: {
      "content-type": "application/json"
    },
    body: JSON.stringify(payload)
  });

  if (!response.ok) {
    throw new Error(`Python executor returned ${response.status}`);
  }

  return response.json();
}

export async function registerAssetRoutes(app: FastifyInstance) {
  resources.forEach((resource) => {
    app.get(`/api/v1/${resource.path}`, async (request, reply) => {
      requirePermission(request, "asset:read", reply);
      const query = request.query as { environment?: never; tag?: never; status?: never };
      return app.services.store.listAssets(resource.kind, query);
    });

    app.post(`/api/v1/${resource.path}`, async (request, reply) => {
      requirePermission(request, "asset:create", reply);
      const body = managedAssetSchema.parse(request.body);
      const now = new Date().toISOString();
      const autoApproved = app.services.config.authMode === "none";
      const asset = await app.services.store.createAsset(resource.kind, {
        name: body.name,
        description: body.description,
        environment: body.environment,
        tags: body.tags,
        status: autoApproved ? "approved" : "pending_approval",
        version: 1,
        createdBy: request.auth.id,
        metadata: body.metadata,
        definition: body.definition,
        kind: resource.kind
      });

      if (!autoApproved) {
        await app.services.store.createApproval({
          id: randomUUID(),
          entityKind: resource.kind,
          entityId: asset.id,
          environment: asset.environment,
          status: "pending",
          makerId: request.auth.id,
          afterState: asset,
          createdAt: now,
          updatedAt: now,
          slaDueAt: new Date(Date.now() + 4 * 60 * 60 * 1000).toISOString()
        });
      }

      reply.code(201);
      return asset;
    });

    app.get(`/api/v1/${resource.path}/:id`, async (request, reply) => {
      requirePermission(request, "asset:read", reply);
      const asset = await app.services.store.getAsset(resource.kind, String((request.params as { id: string }).id));

      if (!asset) {
        reply.code(404);
        return { message: "Asset not found." };
      }

      return asset;
    });

    app.put(`/api/v1/${resource.path}/:id`, async (request, reply) => {
      requirePermission(request, "asset:update", reply);
      const body = managedAssetSchema.partial().parse(request.body);
      const existing = await app.services.store.getAsset(resource.kind, String((request.params as { id: string }).id));

      if (!existing) {
        reply.code(404);
        return { message: "Asset not found." };
      }

      const nextVersion = existing.version + 1;
      const autoApproved = app.services.config.authMode === "none";
      const updated = await app.services.store.updateAsset(resource.kind, existing.id, {
        ...body,
        version: nextVersion,
        status: autoApproved ? "approved" : "pending_approval"
      });

      if (!autoApproved) {
        const now = new Date().toISOString();
        await app.services.store.createApproval({
          id: randomUUID(),
          entityKind: resource.kind,
          entityId: existing.id,
          environment: existing.environment,
          status: "pending",
          makerId: request.auth.id,
          beforeState: existing,
          afterState: updated,
          createdAt: now,
          updatedAt: now,
          slaDueAt: new Date(Date.now() + 4 * 60 * 60 * 1000).toISOString()
        });
      }

      return updated;
    });

    app.delete(`/api/v1/${resource.path}/:id`, async (request, reply) => {
      requirePermission(request, "asset:delete", reply);
      const removed = await app.services.store.deleteAsset(resource.kind, String((request.params as { id: string }).id));

      if (!removed) {
        reply.code(404);
        return { message: "Asset not found." };
      }

      return removed;
    });
  });

  app.post("/api/v1/variables/:id/test", async (request, reply) => {
    requirePermission(request, "asset:test", reply);
    const asset = await app.services.store.getAsset("variable", String((request.params as { id: string }).id));

    if (!asset) {
      reply.code(404);
      return { message: "Variable not found." };
    }

    const body = variableTestSchema.parse(request.body);
    return callPythonExecutor(
      app.services.config.pythonExecutorUrl,
      `/api/v1/variables/${asset.id}/test`,
      {
        payload: body.payload
      }
    );
  });

  app.post("/api/v1/connectors/:id/test", async (request, reply) => {
    requirePermission(request, "asset:test", reply);
    const asset = await app.services.store.getAsset("connector", String((request.params as { id: string }).id));

    if (!asset) {
      reply.code(404);
      return { message: "Connector not found." };
    }

    const payload = (request.body as { response?: Record<string, unknown> })?.response ?? {};
    const mappings = asset.definition.mappings as Record<string, string> | undefined;

    return {
      connectorId: asset.id,
      published: Boolean(asset.definition.published),
      mappings,
      previewResponse: payload
    };
  });

  app.post("/api/v1/scorecards/:id/evaluate", async (request, reply) => {
    requirePermission(request, "asset:read", reply);
    const scorecard = await app.services.store.getAsset("scorecard", String((request.params as { id: string }).id));

    if (!scorecard) {
      reply.code(404);
      return { message: "Scorecard not found." };
    }

    return evaluateScorecard(scorecard.definition as never, (request.body as { input?: Record<string, unknown> })?.input ?? {});
  });

  app.post("/api/v1/policies/:id/evaluate", async (request, reply) => {
    requirePermission(request, "rule:evaluate", reply);
    const policy = await app.services.store.getAsset("policy", String((request.params as { id: string }).id));

    if (!policy) {
      reply.code(404);
      return { message: "Policy not found." };
    }

    const ruleMap = new Map((await app.services.store.listRules()).map((rule) => [rule.id, rule]));
    return resolvePolicy(policy.definition as never, ruleMap);
  });

  app.post("/api/v1/ab-tests/:id/promote", async (request, reply) => {
    requirePermission(request, "asset:update", reply);
    const abTest = await app.services.store.getAsset("ab_test", String((request.params as { id: string }).id));

    if (!abTest) {
      reply.code(404);
      return { message: "Experiment not found." };
    }

    return app.services.store.updateAsset("ab_test", abTest.id, {
      status: "active",
      metadata: {
        ...(abTest.metadata ?? {}),
        promotedAt: new Date().toISOString(),
        promotedBy: request.auth.id
      }
    });
  });

  app.post("/api/v1/workflows/:id/start", async (request, reply) => {
    requirePermission(request, "asset:read", reply);
    const workflow = await app.services.store.getAsset("workflow", String((request.params as { id: string }).id));

    if (!workflow) {
      reply.code(404);
      return { message: "Workflow not found." };
    }

    return {
      workflowId: workflow.id,
      instanceId: randomUUID(),
      startedAt: new Date().toISOString(),
      payload: request.body ?? {}
    };
  });

  app.get("/api/v1/outcome-imports", async (request, reply) => {
    requirePermission(request, "asset:read", reply);
    return app.services.store.listOutcomeImports();
  });

  app.post("/api/v1/outcome-imports", async (request, reply) => {
    requirePermission(request, "asset:create", reply);
    const body = outcomeImportSchema.parse(request.body);
    const record = await app.services.store.createOutcomeImport({
      id: randomUUID(),
      source: body.source,
      status: "processed",
      recordsImported: body.records.length,
      createdAt: new Date().toISOString(),
      createdBy: request.auth.id,
      payload: {
        records: body.records
      }
    });

    reply.code(201);
    return record;
  });

  app.get("/api/v1/batch-jobs", async (request, reply) => {
    requirePermission(request, "batch:manage", reply);
    return app.services.store.listBatchJobs();
  });

  app.get("/api/v1/assets/:kind", async (request, reply) => {
    requirePermission(request, "asset:read", reply);
    const kind = assetKindSchema.parse((request.params as { kind: string }).kind);
    return app.services.store.listAssets(kind);
  });
}
