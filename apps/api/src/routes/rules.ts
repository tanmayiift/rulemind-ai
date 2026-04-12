import { randomUUID } from "node:crypto";
import type { FastifyInstance, FastifyReply, FastifyRequest } from "fastify";
import { analyzeMECE, compileRule, evaluateCompiledRule, generateExpression, validateRuleDefinition } from "@rulemind/rule-engine";
import {
  batchEvaluationRequestSchema,
  evaluationRequestSchema,
  promoteSchema,
  rollbackSchema,
  ruleCreateSchema,
  ruleDefinitionSchema,
  ruleListFiltersSchema,
  ruleUpdateSchema
} from "@rulemind/shared";
import type { EnvironmentName, RuleDefinition, RuleRecord } from "@rulemind/shared";
import type { MECERuleInput } from "@rulemind/rule-engine";
import { requirePermission } from "../auth/middleware";
import { createRuleVersionSnapshot } from "../db/adapter";

function normalizeRulePayload(body: unknown) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return body;
  }

  const payload = body as Record<string, unknown>;

  if ("definition" in payload) {
    return payload;
  }

  return {
    ...payload,
    definition: {
      nodes: payload.nodes,
      connections: payload.connections,
      metadata: {
        name: payload.name,
        environment: payload.environment,
        tags: payload.tags
      }
    }
  };
}

function normalizeEvaluationPayload(body: unknown) {
  if (body && typeof body === "object" && !Array.isArray(body) && "input" in (body as Record<string, unknown>)) {
    return evaluationRequestSchema.parse(body);
  }

  const payload = body && typeof body === "object" && !Array.isArray(body) ? (body as Record<string, unknown>) : {};
  const { evaluatedBy, variables, ...input } = payload;

  return evaluationRequestSchema.parse({
    input,
    evaluatedBy,
    variables
  });
}

function normalizeBatchPayload(body: unknown) {
  if (Array.isArray(body)) {
    return batchEvaluationRequestSchema.parse({ items: body });
  }

  return batchEvaluationRequestSchema.parse(body);
}

function normalizeRollbackPayload(body: unknown) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return rollbackSchema.parse(body);
  }

  const payload = body as Record<string, unknown>;
  return rollbackSchema.parse({
    version: payload.version ?? payload.targetVersion,
    reason: payload.reason
  });
}

function normalizePromotePayload(body: unknown) {
  if (!body || typeof body !== "object" || Array.isArray(body)) {
    return promoteSchema.parse(body);
  }

  const payload = body as Record<string, unknown>;
  return promoteSchema.parse({
    toEnvironment: payload.toEnvironment ?? payload.toEnv,
    promotedBy: payload.promotedBy
  });
}

function assertEnvironmentAccess(request: FastifyRequest, reply: FastifyReply, environment?: EnvironmentName) {
  if (request.auth.authMode !== "apikey") {
    return;
  }

  const scopedEnvironment = request.auth.environment;

  if (scopedEnvironment && environment && scopedEnvironment !== environment) {
    reply.code(403);
    throw request.server.httpErrors.forbidden(`Environment scope mismatch: ${scopedEnvironment} cannot access ${environment}.`);
  }
}

function toPublicRuleRecord(rule: RuleRecord) {
  return {
    ...rule,
    nodes: rule.definition.nodes,
    connections: rule.definition.connections,
    metadata: {
      ...rule.definition.metadata,
      name: rule.name,
      environment: rule.environment,
      tags: rule.tags,
      version: rule.currentVersion,
      updatedAt: rule.updatedAt,
      createdAt: rule.createdAt
    }
  };
}

export async function registerRuleRoutes(app: FastifyInstance) {
  app.post("/api/v1/rules/validate", async (request, reply) => {
    requirePermission(request, "rule:read", reply);
    const definition = ruleDefinitionSchema.parse((request.body as { definition?: never })?.definition ?? request.body) as RuleDefinition;
    const issues = validateRuleDefinition(definition);
    const expression = generateExpression(definition);
    const compilation = issues.some((issue) => issue.level === "error") ? null : compileRule(definition);

    return {
      issues,
      expression,
      graph: compilation
        ? {
            roots: compilation.roots,
            nodeCount: compilation.nodeMap.size,
            edgeCount: [...compilation.childMap.values()].reduce((count, children) => count + children.length, 0)
          }
        : null
    };
  });

  app.post("/api/v1/rules/analyze-mece", async (request, reply) => {
    requirePermission(request, "rule:read", reply);
    const body = request.body as { rules?: unknown };

    if (!body || !Array.isArray(body.rules) || body.rules.length === 0) {
      reply.code(400);
      return { message: "Request body must contain a non-empty 'rules' array." };
    }

    const meceInput: MECERuleInput[] = (body.rules as Array<{ id: string; name: string; definition: unknown; outcome?: string }>).map((r) => ({
      id: r.id,
      name: r.name,
      definition: ruleDefinitionSchema.parse(r.definition) as RuleDefinition,
      outcome: r.outcome as MECERuleInput["outcome"],
    }));

    const result = analyzeMECE(meceInput);
    return result;
  });

  app.post("/api/v1/rules", async (request, reply) => {
    requirePermission(request, "rule:create", reply);
    const body = ruleCreateSchema.parse(normalizeRulePayload(request.body)) as {
      name: string;
      environment: EnvironmentName;
      tags: string[];
      definition: RuleDefinition;
      createdBy: string;
    };
    assertEnvironmentAccess(request, reply, body.environment);
    const issues = validateRuleDefinition(body.definition);
    const blocking = issues.filter((issue) => issue.level === "error");

    if (blocking.length > 0) {
      reply.code(400);
      return { issues };
    }

    const expression = generateExpression(body.definition);
    const autoApproved = app.services.config.authMode === "none";
    const record = await app.services.store.createRule({
      name: body.name,
      environment: body.environment,
      tags: body.tags,
      isActive: true,
      currentVersion: 1,
      createdBy: request.auth.id,
      expression,
      status: autoApproved ? "approved" : "pending_approval",
      definition: body.definition
    });

    await app.services.store.createVersion(
      createRuleVersionSnapshot(record.id, body.definition, expression, 1, body.environment, request.auth.id, "initial version")
    );

    if (!autoApproved) {
      const now = new Date().toISOString();
      await app.services.store.createApproval({
        id: randomUUID(),
        entityKind: "rule",
        entityId: record.id,
        environment: record.environment,
        status: "pending",
        makerId: request.auth.id,
        afterState: record,
        createdAt: now,
        updatedAt: now,
        slaDueAt: new Date(Date.now() + 4 * 60 * 60 * 1000).toISOString()
      });
    }

    app.services.metrics.setActiveRules(record.environment, (await app.services.store.listRules({ environment: record.environment, active: true })).length);
    app.services.logger.info({
      event: "rule.created",
      ruleId: record.id,
      createdBy: request.auth.id,
      nodeCount: body.definition.nodes.length,
      version: 1,
      traceId: request.traceId
    });

    reply.code(201);
    return {
      id: record.id,
      version: record.currentVersion,
      issues
    };
  });

  app.get("/api/v1/rules", async (request, reply) => {
    requirePermission(request, "rule:read", reply);
    const filters = ruleListFiltersSchema.parse(request.query ?? {}) as {
      environment?: EnvironmentName;
      tag?: string;
      active?: boolean;
    };
    const scopedFilters = {
      ...filters,
      environment: request.auth.authMode === "apikey" ? request.auth.environment ?? filters.environment : filters.environment
    };
    const rules = await app.services.store.listRules(scopedFilters);
    const environments = new Set(rules.map((rule) => rule.environment));
    environments.forEach((environment) => {
      app.services.metrics.setActiveRules(
        environment,
        rules.filter((rule) => rule.environment === environment && rule.isActive).length
      );
    });
    return rules.map(toPublicRuleRecord);
  });

  app.get("/api/v1/rules/:id", async (request, reply) => {
    requirePermission(request, "rule:read", reply);
    const rule = await app.services.store.getRule(String((request.params as { id: string }).id));

    if (!rule) {
      reply.code(404);
      return { message: "Rule not found." };
    }

    assertEnvironmentAccess(request, reply, rule.environment);
    return toPublicRuleRecord(rule);
  });

  app.put("/api/v1/rules/:id", async (request, reply) => {
    requirePermission(request, "rule:update", reply);
    const body = ruleUpdateSchema.parse(normalizeRulePayload(request.body)) as {
      name?: string;
      environment?: EnvironmentName;
      tags?: string[];
      definition?: RuleDefinition;
      updatedBy: string;
      summary?: string;
    };
    const rule = await app.services.store.getRule(String((request.params as { id: string }).id));

    if (!rule) {
      reply.code(404);
      return { message: "Rule not found." };
    }

    assertEnvironmentAccess(request, reply, body.environment ?? rule.environment);
    const nextDefinition = body.definition ?? rule.definition;
    const issues = validateRuleDefinition(nextDefinition);
    const blocking = issues.filter((issue) => issue.level === "error");

    if (blocking.length > 0) {
      reply.code(400);
      return { issues };
    }

    const nextVersion = rule.currentVersion + 1;
    const expression = generateExpression(nextDefinition);
    const autoApproved = app.services.config.authMode === "none";
    const updated = await app.services.store.updateRule(rule.id, {
      name: body.name ?? rule.name,
      environment: body.environment ?? rule.environment,
      tags: body.tags ?? rule.tags,
      definition: nextDefinition,
      expression,
      currentVersion: nextVersion,
      status: autoApproved ? "approved" : "pending_approval"
    });

    await app.services.store.createVersion(
      createRuleVersionSnapshot(
        rule.id,
        nextDefinition,
        expression,
        nextVersion,
        updated?.environment ?? rule.environment,
        request.auth.id,
        body.summary
      )
    );
    await app.services.cache.invalidateRule(rule.id);

    if (!autoApproved) {
      const now = new Date().toISOString();
      await app.services.store.createApproval({
        id: randomUUID(),
        entityKind: "rule",
        entityId: rule.id,
        environment: updated?.environment ?? rule.environment,
        status: "pending",
        makerId: request.auth.id,
        beforeState: rule,
        afterState: updated,
        createdAt: now,
        updatedAt: now,
        slaDueAt: new Date(Date.now() + 4 * 60 * 60 * 1000).toISOString()
      });
    }

    const targetEnvironment = updated?.environment ?? rule.environment;
    app.services.metrics.setActiveRules(
      targetEnvironment,
      (await app.services.store.listRules({ environment: targetEnvironment, active: true })).length
    );
    app.services.logger.info({
      event: "rule.updated",
      ruleId: rule.id,
      newVersion: nextVersion,
      traceId: request.traceId
    });

    return {
      id: rule.id,
      version: nextVersion,
      issues
    };
  });

  app.delete("/api/v1/rules/:id", async (request, reply) => {
    requirePermission(request, "rule:delete", reply);
    const removed = await app.services.store.deleteRule(String((request.params as { id: string }).id));

    if (!removed) {
      reply.code(404);
      return { message: "Rule not found." };
    }

    assertEnvironmentAccess(request, reply, removed.environment);
    await app.services.cache.invalidateRule(removed.id);
    app.services.metrics.setActiveRules(
      removed.environment,
      (await app.services.store.listRules({ environment: removed.environment, active: true })).length
    );
    return toPublicRuleRecord(removed);
  });

  app.post("/api/v1/rules/:id/evaluate", async (request, reply) => {
    requirePermission(request, "rule:evaluate", reply);
    const body = normalizeEvaluationPayload(request.body);
    const rule = await app.services.store.getRule(String((request.params as { id: string }).id));

    if (!rule) {
      reply.code(404);
      return { message: "Rule not found." };
    }

    assertEnvironmentAccess(request, reply, rule.environment);
    let compiled = await app.services.cache.getCompiled(rule.id, rule.currentVersion);

    if (compiled) {
      app.services.metrics.markCacheHit(rule.id);
    } else {
      app.services.metrics.markCacheMiss(rule.id);
      compiled = compileRule(rule.definition);
      await app.services.cache.setCompiled(rule.id, rule.currentVersion, compiled);
    }

    const traceId = request.traceId;
    const result = evaluateCompiledRule(compiled, body.input, {
      version: rule.currentVersion,
      traceId,
      variableResults: body.variables,
      event: typeof body.input.event === "string" ? body.input.event : undefined
    });

    app.services.metrics.recordEvaluation(rule.id, rule.environment, result.outcome, result.executionTimeMs);
    await app.services.store.logEvaluation({
      id: randomUUID(),
      ruleId: rule.id,
      ruleVersion: rule.currentVersion,
      inputData: body.input,
      outcome: result.outcome,
      passed: result.passed,
      conditionResults: result.conditionResults,
      variableResults: result.variableResults,
      executionMs: result.executionTimeMs,
      traceId,
      evaluatedAt: new Date().toISOString(),
      evaluatedBy: body.evaluatedBy
    });

    app.services.logger.info({
      event: "rule.evaluated",
      ruleId: rule.id,
      version: rule.currentVersion,
      outcome: result.outcome,
      passed: result.passed,
      durationMs: result.executionTimeMs,
      traceId,
      conditionsChecked: Object.keys(result.conditionResults).length,
      conditionsPassed: Object.values(result.conditionResults).filter((entry) => entry.pass).length,
      inputFields: Object.keys(body.input)
    });

    return result;
  });

  app.post("/api/v1/rules/:id/evaluate/batch", async (request, reply) => {
    requirePermission(request, "rule:evaluate", reply);
    const body = normalizeBatchPayload(request.body);

    if (body.items.length > app.services.config.maxBatchSize) {
      reply.code(400);
      return { message: `Batch size exceeds ${app.services.config.maxBatchSize}` };
    }

    const results = [];

    for (const item of body.items) {
      results.push(
        await app.inject({
          method: "POST",
          url: `/api/v1/rules/${String((request.params as { id: string }).id)}/evaluate`,
          headers: request.headers as Record<string, string>,
          payload: {
            ...item,
            evaluatedBy: body.evaluatedBy
          }
        }).then((response) => response.json())
      );
    }

    await app.services.store.createBatchJob({
      id: randomUUID(),
      kind: "batch-evaluate",
      status: "completed",
      environment: request.auth.environment ?? "dev",
      createdBy: request.auth.id,
      createdAt: new Date().toISOString(),
      updatedAt: new Date().toISOString(),
      payload: {
        ruleId: String((request.params as { id: string }).id),
        size: body.items.length
      },
      result: {
        completed: results.length
      }
    });

    return results;
  });

  app.get("/api/v1/rules/:id/versions", async (request, reply) => {
    requirePermission(request, "rule:read", reply);
    const rule = await app.services.store.getRule(String((request.params as { id: string }).id));

    if (!rule) {
      reply.code(404);
      return { message: "Rule not found." };
    }

    assertEnvironmentAccess(request, reply, rule.environment);
    return app.services.store.listVersions(rule.id);
  });

  app.post("/api/v1/rules/:id/promote", async (request, reply) => {
    requirePermission(request, "rule:promote", reply);
    const body = normalizePromotePayload(request.body);
    const rule = await app.services.store.getRule(String((request.params as { id: string }).id));

    if (!rule) {
      reply.code(404);
      return { message: "Rule not found." };
    }

    assertEnvironmentAccess(request, reply, rule.environment);
    assertEnvironmentAccess(request, reply, body.toEnvironment);

    // MECE enforcement is done at the policy level, not individual rule promotion.
    // Use POST /api/v1/rules/analyze-mece with the full set of rules in a policy
    // to validate MECE before promoting the policy. This avoids false positives
    // from unrelated rules in the same environment.

    const updated = await app.services.store.updateRule(rule.id, {
      environment: body.toEnvironment
    });

    return {
      id: updated?.id,
      environment: updated?.environment
    };
  });

  app.post("/api/v1/rules/:id/rollback", async (request, reply) => {
    requirePermission(request, "rule:update", reply);
    const body = normalizeRollbackPayload(request.body);
    const rule = await app.services.store.getRule(String((request.params as { id: string }).id));

    if (!rule) {
      reply.code(404);
      return { message: "Rule not found." };
    }

    assertEnvironmentAccess(request, reply, rule.environment);
    const version = await app.services.store.getVersion(rule.id, body.version);

    if (!version) {
      reply.code(404);
      return { message: "Version not found." };
    }

    const nextVersion = rule.currentVersion + 1;
    await app.services.store.updateRule(rule.id, {
      definition: version.definition,
      expression: version.expression,
      currentVersion: nextVersion
    });
    await app.services.store.createVersion(
      createRuleVersionSnapshot(
        rule.id,
        version.definition,
        version.expression,
        nextVersion,
        rule.environment,
        request.auth.id,
        `rollback to v${body.version}: ${body.reason}`
      )
    );
    await app.services.cache.invalidateRule(rule.id);

    return {
      id: rule.id,
      version: nextVersion,
      rolledBackFrom: body.version
    };
  });

  app.get("/api/v1/rules/:id/audit", async (request, reply) => {
    requirePermission(request, "audit:read", reply);
    const rule = await app.services.store.getRule(String((request.params as { id: string }).id));

    if (!rule) {
      reply.code(404);
      return { message: "Rule not found." };
    }

    assertEnvironmentAccess(request, reply, rule.environment);
    return app.services.store.getAuditLog(rule.id);
  });
}
