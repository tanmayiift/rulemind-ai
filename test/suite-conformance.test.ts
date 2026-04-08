import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { buildApp } from "../apps/api/src/server";
import { evaluateRule, generateExpression, validateRuleDefinition } from "../packages/rule-engine/src";
import type { RuleDefinition } from "../packages/shared/src";

describe("suite conformance", () => {
  let app: Awaited<ReturnType<typeof buildApp>>;

  beforeAll(async () => {
    process.env.DATABASE_ADAPTER = "file";
    process.env.FILE_STORE_PATH = "./.runtime/suite-data";
    process.env.AUTH_MODE = "none";
    process.env.PORT = "8080";
    app = await buildApp();
  });

  afterAll(async () => {
    await app.close();
  });

  it("matches key operator and topology semantics from the workbook", () => {
    const invalidRegex = evaluateRule(
      {
        nodes: [
          {
            id: "c1",
            type: "condition",
            label: "Regex",
            x: 0,
            y: 0,
            config: { field: "status", fieldType: "string", operator: "regex", value: "[" }
          },
          { id: "r1", type: "reject", label: "Reject", x: 0, y: 0, config: {} }
        ],
        connections: [{ from: "c1", to: "r1" }]
      },
      { status: "approved" }
    );
    expect(invalidRegex.passed).toBe(false);
    expect(invalidRegex.conditionResults.c1.reason).toContain("Invalid regex");

    const existsEmpty = evaluateRule(
      {
        nodes: [
          {
            id: "c1",
            type: "condition",
            label: "Exists",
            x: 0,
            y: 0,
            config: { field: "flag", fieldType: "string", operator: "exists", value: "" }
          },
          { id: "r1", type: "reject", label: "Reject", x: 0, y: 0, config: {} }
        ],
        connections: [{ from: "c1", to: "r1" }]
      },
      { flag: "" }
    );
    expect(existsEmpty.passed).toBe(false);

    const nanComparison = evaluateRule(
      {
        nodes: [
          {
            id: "c1",
            type: "condition",
            label: "Numeric",
            x: 0,
            y: 0,
            config: { field: "score", fieldType: "number", operator: ">", value: "700" }
          },
          { id: "r1", type: "reject", label: "Reject", x: 0, y: 0, config: {} }
        ],
        connections: [{ from: "c1", to: "r1" }]
      },
      { score: "abc" }
    );
    expect(nanComparison.passed).toBe(false);

    const negativeZero = evaluateRule(
      {
        nodes: [
          {
            id: "c1",
            type: "condition",
            label: "Negative zero",
            x: 0,
            y: 0,
            config: { field: "value", fieldType: "number", operator: ">=", value: "0" }
          },
          { id: "a1", type: "approve", label: "Approve", x: 0, y: 0, config: {} }
        ],
        connections: [{ from: "c1", to: "a1" }]
      },
      { value: -0 }
    );
    expect(negativeZero.passed).toBe(true);

    const emptyAnd = evaluateRule(
      {
        nodes: [
          { id: "and1", type: "and", label: "AND", x: 0, y: 0, config: {} },
          { id: "r1", type: "reject", label: "Reject", x: 0, y: 0, config: {} }
        ],
        connections: []
      },
      {}
    );
    expect(emptyAnd.passed).toBe(false);

    const outcomeOnly = evaluateRule(
      {
        nodes: [{ id: "a1", type: "approve", label: "Approve", x: 0, y: 0, config: { reason: "always" } }],
        connections: []
      },
      {}
    );
    expect(outcomeOnly.passed).toBe(true);
    expect(outcomeOnly.outcome).toBe("approve");
  });

  it("accepts the provided fixtures and preserves underwriting DSL expectations", () => {
    const simpleRule: RuleDefinition = {
      nodes: [
        { id: "n1", type: "condition", label: "Credit Score Check", x: 120, y: 100, config: { fieldType: "number", field: "credit_score", operator: ">=", value: "700" } },
        { id: "n2", type: "approve", label: "Approve", x: 400, y: 100, config: { reason: "Credit score meets threshold" } }
      ],
      connections: [{ from: "n1", to: "n2" }]
    };
    const complexRule: RuleDefinition = {
      nodes: [
        { id: "t1", type: "trigger", label: "On Application", x: 30, y: 150, config: { event: "on_application_submit" } },
        { id: "a1", type: "and", label: "AND", x: 220, y: 150, config: {} },
        { id: "c1", type: "score", label: "Credit Score", x: 440, y: 40, config: { fieldType: "number", field: "credit_score", operator: "between", value: "650", value2: "850" } },
        { id: "c2", type: "condition", label: "Income Check", x: 440, y: 110, config: { fieldType: "number", field: "annual_income", operator: ">", value: "50000" } },
        { id: "c3", type: "condition", label: "Employment", x: 440, y: 180, config: { fieldType: "string", field: "employment_status", operator: "in", value: "employed, self_employed" } },
        { id: "n1", type: "not", label: "NOT", x: 440, y: 260, config: {} },
        { id: "c4", type: "condition", label: "Bankruptcy Flag", x: 640, y: 260, config: { fieldType: "string", field: "has_bankruptcy", operator: "exists", value: "" } },
        { id: "c5", type: "condition", label: "DTI Ratio", x: 440, y: 340, config: { fieldType: "number", field: "debt_to_income", operator: "<=", value: "0.43" } },
        { id: "out1", type: "approve", label: "Approve", x: 720, y: 80, config: { reason: "All underwriting criteria met" } },
        { id: "out2", type: "review", label: "Manual Review", x: 720, y: 180, config: { reason: "Borderline — needs human review" } },
        { id: "out3", type: "reject", label: "Reject", x: 720, y: 280, config: { reason: "Failed underwriting" } }
      ],
      connections: [
        { from: "t1", to: "a1" }, { from: "a1", to: "c1" }, { from: "a1", to: "c2" },
        { from: "a1", to: "c3" }, { from: "a1", to: "n1" }, { from: "n1", to: "c4" }, { from: "a1", to: "c5" }
      ]
    };
    const partialRule = {
      nodes: [
        { id: "n1", type: "condition", label: "Orphan Condition", x: 200, y: 100, config: { fieldType: "number", field: "score", operator: ">=", value: "500" } }
      ]
    } as unknown as RuleDefinition;

    const simplePass = evaluateRule(simpleRule, { credit_score: 700 });
    const simpleFail = evaluateRule(simpleRule, { credit_score: 650 });
    expect(simplePass.outcome).toBe("approve");
    expect(simpleFail.passed).toBe(false);

    const complexExpression = generateExpression(complexRule);
    expect(complexExpression).toContain("WHEN on_application_submit {");
    expect(complexExpression).toContain("credit_score BETWEEN 650 AND 850");
    expect(complexExpression).toContain("employment_status IN (employed, self_employed)");
    expect(complexExpression).toContain("NOT (");
    expect(complexExpression).toContain("has_bankruptcy EXISTS");
    expect(complexExpression).toContain("debt_to_income <= 0.43");

    const partialIssues = validateRuleDefinition({
      ...partialRule,
      connections: partialRule.connections ?? []
    });
    expect(partialIssues.some((issue) => issue.message.includes("No outcome node"))).toBe(true);

    expect(() => JSON.parse("{ this is not valid json at all")).toThrow();
  });

  it("supports the suite REST contract with top-level rule bodies and raw evaluation payloads", async () => {
    const createResponse = await app.inject({
      method: "POST",
      url: "/api/v1/rules",
      payload: {
        name: "Suite contract rule",
        environment: "dev",
        nodes: [
          { id: "c1", type: "condition", label: "Score", x: 0, y: 0, config: { field: "credit_score", fieldType: "number", operator: ">=", value: "700" } },
          { id: "a1", type: "approve", label: "Approve", x: 0, y: 0, config: { reason: "pass" } },
          { id: "r1", type: "reject", label: "Reject", x: 0, y: 0, config: { reason: "fail" } }
        ],
        connections: [{ from: "c1", to: "a1" }]
      }
    });
    expect(createResponse.statusCode).toBe(201);
    const created = createResponse.json() as { id: string; version: number };
    expect(created.version).toBe(1);

    const invalidCreate = await app.inject({
      method: "POST",
      url: "/api/v1/rules",
      payload: { name: "Missing nodes" }
    });
    expect(invalidCreate.statusCode).toBe(400);

    const getResponse = await app.inject({
      method: "GET",
      url: `/api/v1/rules/${created.id}`
    });
    expect(getResponse.statusCode).toBe(200);
    const fetched = getResponse.json() as { nodes: unknown[]; connections: unknown[] };
    expect(Array.isArray(fetched.nodes)).toBe(true);
    expect(Array.isArray(fetched.connections)).toBe(true);

    const evaluateResponse = await app.inject({
      method: "POST",
      url: `/api/v1/rules/${created.id}/evaluate`,
      headers: {
        "x-trace-id": "tr_suite_contract"
      },
      payload: { credit_score: 750 }
    });
    expect(evaluateResponse.statusCode).toBe(200);
    expect(evaluateResponse.headers["x-trace-id"]).toBe("tr_suite_contract");
    const evaluated = evaluateResponse.json() as { outcome: string; traceId: string };
    expect(evaluated.outcome).toBe("approve");
    expect(evaluated.traceId).toBe("tr_suite_contract");

    const batchResponse = await app.inject({
      method: "POST",
      url: `/api/v1/rules/${created.id}/evaluate/batch`,
      payload: [{ credit_score: 750 }, { credit_score: 600 }]
    });
    expect(batchResponse.statusCode).toBe(200);
    const batch = batchResponse.json() as Array<{ outcome: string }>;
    expect(Array.isArray(batch)).toBe(true);
    expect(batch).toHaveLength(2);

    const versionsResponse = await app.inject({
      method: "GET",
      url: `/api/v1/rules/${created.id}/versions`
    });
    expect(versionsResponse.statusCode).toBe(200);

    const promoteResponse = await app.inject({
      method: "POST",
      url: `/api/v1/rules/${created.id}/promote`,
      payload: { fromEnv: "dev", toEnv: "uat" }
    });
    expect(promoteResponse.statusCode).toBe(200);

    const rollbackResponse = await app.inject({
      method: "POST",
      url: `/api/v1/rules/${created.id}/rollback`,
      payload: { targetVersion: 1 }
    });
    expect(rollbackResponse.statusCode).toBe(200);

    const auditResponse = await app.inject({
      method: "GET",
      url: `/api/v1/rules/${created.id}/audit`
    });
    expect(auditResponse.statusCode).toBe(200);
    const audit = auditResponse.json() as Array<{ traceId: string }>;
    expect(audit.some((entry) => entry.traceId === "tr_suite_contract")).toBe(true);
  });
});
