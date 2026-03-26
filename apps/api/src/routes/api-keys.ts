import { randomUUID } from "node:crypto";
import type { FastifyInstance } from "fastify";
import { apiKeyCreateSchema, type Permission } from "@rulemind/shared";
import { requirePermission } from "../auth/middleware";
import { generateApiKey, hashApiKey } from "../auth/api-keys";

export async function registerApiKeyRoutes(app: FastifyInstance) {
  app.get("/api/v1/api-keys", async (request, reply) => {
    requirePermission(request, "api-key:manage", reply);
    return app.services.store.listApiKeys(request.auth.environment);
  });

  app.post("/api/v1/api-keys", async (request, reply) => {
    requirePermission(request, "api-key:manage", reply);
    const body = apiKeyCreateSchema.parse(request.body);
    const rawKey = generateApiKey(body.environment);
    const record = await app.services.store.createApiKey({
      id: randomUUID(),
      name: body.name,
      keyHash: hashApiKey(rawKey, app.services.config.apiKeySalt),
      environment: body.environment,
      permissions: body.permissions as Permission[],
      createdBy: request.auth.id,
      createdAt: new Date().toISOString(),
      expiresAt: body.expiresAt,
      rateLimit: body.rateLimit
    });

    reply.code(201);
    return {
      ...record,
      key: rawKey
    };
  });
}
