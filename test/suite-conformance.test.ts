import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterAll, beforeAll, describe, expect, it } from "vitest";
import { buildApp } from "../apps/api/src/server";
import { evaluateRule, generateExpression, validateRuleDefinition } from "../packages/rule-engine/src";
import type { RuleDefinition } from "../packages/shared/src";

const root = "/Users/tanmaykumar/Downloads/RuleMind.AI";
const loadFixture = <T>(name: string) =>
  JSON.parse(readFileSync(join(root, "Test Cases", name), "utf8")) as T;

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
    const simpleRule = loadFixture<RuleDefinition>("rule_simple_credit.json");
    const complexRule = loadFixture<RuleDefinition>("rule_complex_underwriting.json");
    const partialRule = loadFixture<RuleDefinition>("rule_partial_no_connections.json");

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

    expect(() => JSON.parse(readFileSync(join(root, "Test Cases", "rule_malformed.json"), "utf8"))).toThrow();
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
      payload: { fromEnv: "dev", toEnv: "staging" }
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
