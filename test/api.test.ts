import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { buildApp } from "../apps/api/src/server";

describe("api", () => {
  let app: Awaited<ReturnType<typeof buildApp>>;

  beforeAll(async () => {
    process.env.DATABASE_ADAPTER = "file";
    process.env.FILE_STORE_PATH = "./.runtime/test-data";
    process.env.AUTH_MODE = "none";
    app = await buildApp();
  });

  afterAll(async () => {
    await app.close();
  });

  it("creates, evaluates, and lists a rule", async () => {
    const createResponse = await app.inject({
      method: "POST",
      url: "/api/v1/rules",
      payload: {
        name: "API test rule",
        environment: "dev",
        definition: {
          nodes: [
            { id: "t", type: "trigger", label: "Trigger", x: 0, y: 0, config: { event: "application_submitted" } },
            { id: "c", type: "condition", label: "Score", x: 0, y: 0, config: { field: "credit_score", fieldType: "number", operator: ">=", value: 700 } },
            { id: "a", type: "approve", label: "Approve", x: 0, y: 0, config: { reason: "OK" } }
          ],
          connections: [
            { from: "t", to: "c" },
            { from: "c", to: "a" }
          ]
        }
      }
    });
    const created = createResponse.json() as { id: string };
    expect(createResponse.statusCode).toBe(201);

    const evaluateResponse = await app.inject({
      method: "POST",
      url: `/api/v1/rules/${created.id}/evaluate`,
      payload: {
        input: {
          event: "application_submitted",
          credit_score: 710
        }
      }
    });
    expect(evaluateResponse.statusCode).toBe(200);
    expect((evaluateResponse.json() as { outcome: string }).outcome).toBe("approve");

    const listResponse = await app.inject({
      method: "GET",
      url: "/api/v1/rules?environment=dev"
    });
    expect(listResponse.statusCode).toBe(200);
    expect((listResponse.json() as Array<{ id: string }>).length).toBeGreaterThan(0);
  });
});
