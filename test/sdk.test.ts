import { afterEach, describe, expect, it, vi } from "vitest";
import { RuleEngineClient } from "../packages/sdk-js/src";

describe("RuleEngineClient", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends suite-shaped create and evaluate requests", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        status: 201,
        json: async () => ({ id: "rule_1", version: 1 })
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({ outcome: "approve", passed: true, conditionResults: {}, executionTimeMs: 1, traceId: "tr_1", version: 1 })
      } as Response);

    const client = new RuleEngineClient({
      baseUrl: "http://localhost:8080",
      apiKey: "re_dev_valid",
      timeout: 5000
    });

    await client.createRule({
      name: "SDK Rule",
      nodes: [{ id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "score", fieldType: "number", operator: ">=", value: "700" } }],
      connections: []
    });

    await client.evaluate("rule_1", { score: 750 });

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "http://localhost:8080/api/v1/rules",
      expect.objectContaining({
        method: "POST",
        body: expect.stringContaining("\"nodes\"")
      })
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      "http://localhost:8080/api/v1/rules/rule_1/evaluate",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ score: 750 })
      })
    );
  });

  it("retries 5xx responses and returns raw batch arrays", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: false,
        status: 503,
        json: async () => ({})
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => [{ outcome: "approve" }, { outcome: "reject" }]
      } as Response);

    const client = new RuleEngineClient({
      baseUrl: "http://localhost:8080",
      retries: 1
    });

    const result = await client.evaluateBatch("rule_1", [{ score: 750 }, { score: 600 }]);
    expect(result).toHaveLength(2);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
