import { mkdir } from "node:fs/promises";
import { dirname, join, resolve } from "node:path";
import { createHmac, randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { buildApp } from "../apps/api/src/server";

const envSnapshot = { ...process.env };
const runtimeRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..", ".runtime");

async function buildTestApp(overrides: Record<string, string>) {
  process.env = {
    ...envSnapshot,
    DATABASE_ADAPTER: "file",
    AUTH_MODE: "none",
    PORT: "8080",
    API_KEY_SALT: "suite-salt",
    JWT_SECRET: "suite-jwt-secret",
    JWT_ISSUER: "https://local.rulemind.test",
    JWT_AUDIENCE: "rulemind",
    ...overrides
  };
  return buildApp();
}

function signJwt(payload: Record<string, unknown>, secret: string) {
  const header = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString("base64url");
  const body = Buffer.from(
    JSON.stringify({
      ...payload,
      iss: "https://local.rulemind.test",
      aud: "rulemind",
      exp: Math.floor(Date.now() / 1000) + 3600
    })
  ).toString("base64url");
  const signature = createHmac("sha256", secret).update(`${header}.${body}`).digest("base64url");
  return `${header}.${body}.${signature}`;
}

afterEach(() => {
  process.env = { ...envSnapshot };
});

describe("auth and observability conformance", () => {
  it("supports api key auth, expiry, RBAC, and environment scoping", async () => {
    const storePath = join(runtimeRoot, `apikey-suite-${randomUUID()}`);
    await mkdir(storePath, { recursive: true });

    const seedApp = await buildTestApp({
      AUTH_MODE: "none",
      FILE_STORE_PATH: storePath
    });

    const createdRule = await seedApp.inject({
      method: "POST",
      url: "/api/v1/rules",
      payload: {
        name: "Seed Rule",
        environment: "dev",
        nodes: [
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } },
          { id: "a1", type: "approve", label: "Approve", x: 0, y: 0, config: {} }
        ],
        connections: [{ from: "c1", to: "a1" }]
      }
    });
    expect(createdRule.statusCode).toBe(201);

    const editorKey = await seedApp.inject({
      method: "POST",
      url: "/api/v1/api-keys",
      payload: {
        name: "Editor Key",
        environment: "dev",
        permissions: ["rule:create", "rule:read", "rule:evaluate"]
      }
    });
    const viewerKey = await seedApp.inject({
      method: "POST",
      url: "/api/v1/api-keys",
      payload: {
        name: "Viewer Key",
        environment: "dev",
        permissions: ["rule:read", "rule:evaluate"]
      }
    });
    const expiredKey = await seedApp.inject({
      method: "POST",
      url: "/api/v1/api-keys",
      payload: {
        name: "Expired Key",
        environment: "dev",
        permissions: ["rule:read"],
        expiresAt: new Date(Date.now() - 60_000).toISOString()
      }
    });

    await seedApp.close();

    const editorSecret = (editorKey.json() as { key: string }).key;
    const viewerSecret = (viewerKey.json() as { key: string }).key;
    const expiredSecret = (expiredKey.json() as { key: string }).key;

    const apiKeyApp = await buildTestApp({
      AUTH_MODE: "apikey",
      FILE_STORE_PATH: storePath
    });

    const listResponse = await apiKeyApp.inject({
      method: "GET",
      url: "/api/v1/rules",
      headers: {
        "x-api-key": editorSecret
      }
    });
    expect(listResponse.statusCode).toBe(200);

    const viewerCreate = await apiKeyApp.inject({
      method: "POST",
      url: "/api/v1/rules",
      headers: {
        "x-api-key": viewerSecret
      },
      payload: {
        name: "Viewer Create",
        environment: "dev",
        nodes: [],
        connections: []
      }
    });
    expect(viewerCreate.statusCode).toBe(403);

    const invalidKey = await apiKeyApp.inject({
      method: "GET",
      url: "/api/v1/rules",
      headers: {
        "x-api-key": "re_dev_invalid"
      }
    });
    expect(invalidKey.statusCode).toBe(401);

    const expiredAccess = await apiKeyApp.inject({
      method: "GET",
      url: "/api/v1/rules",
      headers: {
        "x-api-key": expiredSecret
      }
    });
    expect(expiredAccess.statusCode).toBe(401);

    const scopedCreate = await apiKeyApp.inject({
      method: "POST",
      url: "/api/v1/rules",
      headers: {
        "x-api-key": editorSecret
      },
      payload: {
        name: "Scoped Create",
        environment: "staging",
        nodes: [
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } }
        ],
        connections: []
      }
    });
    expect(scopedCreate.statusCode).toBe(403);

    await apiKeyApp.close();
  });

  it("supports local JWT_SECRET verification and exposes suite metrics names", async () => {
    const storePath = join(runtimeRoot, `jwt-suite-${randomUUID()}`);
    await mkdir(storePath, { recursive: true });

    const seedApp = await buildTestApp({
      AUTH_MODE: "none",
      FILE_STORE_PATH: storePath
    });

    const createResponse = await seedApp.inject({
      method: "POST",
      url: "/api/v1/rules",
      payload: {
        name: "Metrics Rule",
        environment: "dev",
        nodes: [
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } },
          { id: "a1", type: "approve", label: "Approve", x: 0, y: 0, config: {} },
          { id: "r1", type: "reject", label: "Reject", x: 0, y: 0, config: {} }
        ],
        connections: [{ from: "c1", to: "a1" }]
      }
    });
    const ruleId = (createResponse.json() as { id: string }).id;

    const evaluated = await seedApp.inject({
      method: "POST",
      url: `/api/v1/rules/${ruleId}/evaluate`,
      headers: {
        "x-trace-id": "tr_auth_obs"
      },
      payload: {
        score: 750
      }
    });
    expect(evaluated.statusCode).toBe(200);

    const metricsResponse = await seedApp.inject({
      method: "GET",
      url: "/metrics"
    });
    const metricsBody = metricsResponse.body;
    expect(metricsResponse.statusCode).toBe(200);
    expect(metricsBody).toContain("rule_evaluations_total");
    expect(metricsBody).toContain("rule_evaluations_duration_ms");
    expect(metricsBody).toContain("rule_evaluations_outcome");
    expect(metricsBody).toContain("rule_cache_hits");
    expect(metricsBody).toContain("rule_cache_misses");
    expect(metricsBody).toContain("rule_active_count");

    const readyResponse = await seedApp.inject({
      method: "GET",
      url: "/health/ready"
    });
    expect(readyResponse.statusCode).toBe(200);

    const auditResponse = await seedApp.inject({
      method: "GET",
      url: `/api/v1/rules/${ruleId}/audit`
    });
    expect(auditResponse.statusCode).toBe(200);
    expect((auditResponse.json() as Array<{ traceId: string }>)[0]?.traceId).toBe("tr_auth_obs");

    await seedApp.close();

    const viewerToken = signJwt(
      {
        sub: "viewer-1",
        roles: ["viewer"],
        name: "Viewer"
      },
      "suite-jwt-secret"
    );

    const adminToken = signJwt(
      {
        sub: "admin-1",
        roles: ["admin"],
        name: "Admin"
      },
      "suite-jwt-secret"
    );

    const jwtApp = await buildTestApp({
      AUTH_MODE: "jwt",
      FILE_STORE_PATH: storePath
    });

    const noHeader = await jwtApp.inject({
      method: "GET",
      url: "/api/v1/rules"
    });
    expect(noHeader.statusCode).toBe(401);

    const viewerRead = await jwtApp.inject({
      method: "GET",
      url: "/api/v1/rules",
      headers: {
        authorization: `Bearer ${viewerToken}`
      }
    });
    expect(viewerRead.statusCode).toBe(200);

    const viewerCreate = await jwtApp.inject({
      method: "POST",
      url: "/api/v1/rules",
      headers: {
        authorization: `Bearer ${viewerToken}`
      },
      payload: {
        name: "JWT Viewer Create",
        environment: "dev",
        nodes: [],
        connections: []
      }
    });
    expect(viewerCreate.statusCode).toBe(403);

    const adminCreate = await jwtApp.inject({
      method: "POST",
      url: "/api/v1/rules",
      headers: {
        authorization: `Bearer ${adminToken}`
      },
      payload: {
        name: "JWT Admin Create",
        environment: "dev",
        nodes: [
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } },
          { id: "a1", type: "approve", label: "Approve", x: 0, y: 0, config: {} }
        ],
        connections: [{ from: "c1", to: "a1" }]
      }
    });
    expect(adminCreate.statusCode).toBe(201);

    await jwtApp.close();
  });
});
