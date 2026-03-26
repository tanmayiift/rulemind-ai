import { createRemoteJWKSet, jwtVerify, type JWTPayload } from "jose";
import type { FastifyReply, FastifyRequest } from "fastify";
import { rolePermissions, type Permission, type Role, type UserContext } from "@rulemind/shared";
import type { AppConfig } from "../config";
import type { PlatformStore } from "../db/adapter";
import { hashApiKey } from "./api-keys";

function uniquePermissions(roles: Role[]): Permission[] {
  return [...new Set(roles.flatMap((role) => rolePermissions[role] ?? []))];
}

function userFromJwtPayload(payload: JWTPayload): UserContext {
  const roles: Role[] = Array.isArray(payload.roles)
    ? payload.roles.filter((role): role is Role => typeof role === "string" && role in rolePermissions)
    : ["viewer"];

  return {
    id: String(payload.sub ?? "jwt-user"),
    name: String(payload.name ?? payload.preferred_username ?? payload.email ?? "JWT User"),
    email: typeof payload.email === "string" ? payload.email : undefined,
    roles,
    permissions: uniquePermissions(roles),
    authMode: "jwt"
  };
}

export async function authenticateRequest(
  request: FastifyRequest,
  config: AppConfig,
  store: PlatformStore
): Promise<UserContext> {
  if (config.authMode === "none") {
    return {
      id: "local-admin",
      name: "Local Admin",
      email: "local@rulemind.dev",
      roles: ["admin"],
      permissions: uniquePermissions(["admin"]),
      authMode: "none",
      environment: "dev"
    };
  }

  const headerValue = request.headers["x-api-key"] ?? request.headers.authorization;

  if (config.authMode === "apikey") {
    const rawToken = String(headerValue ?? "").replace(/^Bearer\s+/i, "");

    if (!rawToken) {
      throw request.server.httpErrors.unauthorized("API key required.");
    }

    const record = await store.getApiKeyByHash(hashApiKey(rawToken, config.apiKeySalt));

    if (!record) {
      throw request.server.httpErrors.unauthorized("Invalid API key.");
    }

    if (record.expiresAt && Date.parse(record.expiresAt) <= Date.now()) {
      throw request.server.httpErrors.unauthorized("API key expired.");
    }

    await store.updateApiKey(record.id, { lastUsedAt: new Date().toISOString() });

    return {
      id: record.id,
      name: record.name,
      roles: ["api"],
      permissions: record.permissions,
      authMode: "apikey",
      apiKeyId: record.id,
      environment: record.environment
    };
  }

  const bearerToken = String(headerValue ?? "").replace(/^Bearer\s+/i, "");

  if (!bearerToken) {
    throw request.server.httpErrors.unauthorized("Bearer token required.");
  }

  if (config.jwtSecret) {
    try {
      const { payload } = await jwtVerify(bearerToken, new TextEncoder().encode(config.jwtSecret), {
        issuer: config.jwtIssuer,
        audience: config.jwtAudience
      });
      return userFromJwtPayload(payload);
    } catch (error) {
      if (!config.jwtJwksUri && !config.jwtIssuer) {
        throw request.server.httpErrors.unauthorized(
          error instanceof Error ? error.message : "Invalid bearer token."
        );
      }
    }
  }

  if (!config.jwtJwksUri && !config.jwtIssuer) {
    throw request.server.httpErrors.unauthorized("JWT configuration is incomplete.");
  }

  try {
    const jwks = createRemoteJWKSet(new URL(config.jwtJwksUri ?? `${config.jwtIssuer}/.well-known/jwks.json`));
    const { payload } = await jwtVerify(bearerToken, jwks, {
      issuer: config.jwtIssuer,
      audience: config.jwtAudience
    });
    return userFromJwtPayload(payload);
  } catch (error) {
    throw request.server.httpErrors.unauthorized(
      error instanceof Error ? error.message : "Invalid bearer token."
    );
  }
}

export function requirePermission(request: FastifyRequest, permission: Permission, reply: FastifyReply) {
  const user = request.auth;

  if (!user.permissions.includes(permission)) {
    reply.code(403);
    throw request.server.httpErrors.forbidden(`Missing permission: ${permission}`);
  }
}
