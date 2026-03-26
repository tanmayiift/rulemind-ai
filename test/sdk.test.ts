import { afterEach, describe, expect, it, vi } from "vitest";
import { RuleEngineClient } from "../packages/sdk-js/src";

describe("RuleEngineClient", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("sends decide requests to the server-mode SDK endpoint", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          outcome: "approve",
          score: 410,
          variables: { bureau_score: 760 },
          ruleResults: [{ id: "r1", passed: true }],
          experiment: { id: "exp-1", variant: "control" },
          latencyMs: 12,
          requestId: "req_1",
          serverOnlyStepsSkipped: ["a1"]
        })
      } as Response);

    const client = new RuleEngineClient({
      baseUrl: "http://localhost:8080",
      apiKey: "rm_live_devlocaltenantkey000000000000",
      sdkVersion: "4.1.0",
      timeoutMs: 5000
    });

    const result = await client.decide({
      policyId: "policy_pl_underwriting",
      payload: { score: 750 },
      userId: "user-1",
      requestId: "req_1"
    });

    expect(result.outcome).toBe("approve");
    expect(result.serverOnlyStepsSkipped).toEqual(["a1"]);

    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8080/sdk/v1/decide",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "content-type": "application/json",
          "x-api-key": "rm_live_devlocaltenantkey000000000000"
        }),
        body: JSON.stringify({
          policyId: "policy_pl_underwriting",
          payload: { score: 750 },
          userId: "user-1",
          requestId: "req_1"
        })
      })
    );
  });

  it("retries 5xx decide responses", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValueOnce({
        ok: false,
        status: 503,
        text: async () => "service unavailable"
      } as Response)
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: async () => ({
          outcome: "review",
          variables: {},
          ruleResults: [],
          latencyMs: 7,
          serverOnlyStepsSkipped: []
        })
      } as Response);

    const client = new RuleEngineClient({
      baseUrl: "http://localhost:8080",
      retries: 1
    });

    const result = await client.decide({
      policyId: "policy_pl_underwriting",
      payload: { score: 750 }
    });

    expect(result.outcome).toBe("review");
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("sends SDK bundle headers and returns null on 304", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValueOnce({
      ok: false,
      status: 304
    } as Response);

    const client = new RuleEngineClient({
      baseUrl: "http://localhost:8080",
      apiKey: "rm_live_devlocaltenantkey000000000000",
      sdkVersion: "4.1.0"
    });

    const bundle = await client.getBundle({
      bundleVersion: 12,
      clientPublicKey: "public-key"
    });

    expect(bundle).toBeNull();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8080/sdk/v1/bundle",
      expect.objectContaining({
        method: "GET",
        headers: expect.objectContaining({
          "x-api-key": "rm_live_devlocaltenantkey000000000000",
          "x-sdk-version": "4.1.0",
          "x-bundle-version": "12",
          "x-client-public-key": "public-key"
        })
      })
    );
  });
});
