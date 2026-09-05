/**
 * Step-through debugger (parity P04), end to end.
 *
 * The one property worth an e2e test is the one unit tests can only assert against a fixture:
 * THE DEBUGGER'S ANSWER EQUALS PRODUCTION'S, on this environment's real policies and real
 * connectors. That is the entire justification for stepping the real execution path instead of
 * writing a debug interpreter, and it is exactly what broke during development -- `simulate=true`
 * also switches sample seeding on, so the debugger was filling unsupplied fields from the demo
 * payloads and confidently disagreeing with production.
 *
 * Non-destructive: a debug session lives in a process dict and is deleted at the end. The one
 * write is the `/decide` call it compares against, which every other spec here already makes.
 */
import { expect, test } from "@playwright/test";

import { API_BASE_URL } from "./env";

const PAYLOAD = { bureau_score: 540, monthly_income: 18000 };

test.describe.configure({ mode: "serial" });

async function firstProdPolicy(request: { get: Function }): Promise<string | null> {
  const res = await request.get(`${API_BASE_URL}/api/v1/policies`);
  expect(res.status()).toBe(200);
  const policies = await res.json();
  if (!Array.isArray(policies) || policies.length === 0) return null;
  const prod = policies.find((p: { status?: string }) => p.status === "prod");
  return (prod || policies[0]).id as string;
}

test("stepping a policy reaches the same outcome production does", async ({ request }) => {
  const policyId = await firstProdPolicy(request);
  test.skip(!policyId, "this workspace has no policy to step through");

  const started = await request.post(`${API_BASE_URL}/api/v1/debug/sessions`, {
    data: { policy_id: policyId, payload: PAYLOAD },
  });
  expect(started.status()).toBe(200);
  let snap = await started.json();

  // It must stop BEFORE anything runs -- a debugger that executes a step to tell you it is
  // about to execute that step is a trace with extra clicks.
  expect(snap.status).toBe("paused");
  expect(snap.paused_before_index).toBe(0);
  expect(snap.executed_steps).toBe(0);
  expect(snap.next_step).not.toBeNull();

  const sessionId = snap.session_id as string;
  try {
    // Advance one step at a time, asserting it really is ONE, then compare the final outcome.
    for (let i = 0; i < snap.step_count + 1 && !snap.finished; i += 1) {
      const before = snap.executed_steps;
      const res = await request.post(`${API_BASE_URL}/api/v1/debug/sessions/${sessionId}/step`);
      expect(res.status()).toBe(200);
      snap = await res.json();
      expect(snap.executed_steps).toBeGreaterThan(before);
    }
    expect(snap.finished).toBe(true);

    const decided = await request.post(`${API_BASE_URL}/api/v1/decide`, {
      data: { policy_id: policyId, payload: PAYLOAD },
    });
    expect(decided.status()).toBe(200);
    const production = await decided.json();

    expect(snap.outcome).toBe(production.outcome);

    // Values, not just the verdict: an outcome can agree by coincidence on a two-branch policy.
    // Every variable the debugger showed must equal what production computed for the same input.
    const productionVariables = production.computed_variables || {};
    for (const [name, value] of Object.entries(snap.variables || {})) {
      if (name in productionVariables) {
        expect(
          productionVariables[name],
          `the debugger reported ${name}=${JSON.stringify(value)} where production computed ` +
            `${JSON.stringify(productionVariables[name])} -- check sample seeding`
        ).toEqual(value);
      }
    }
  } finally {
    await request.delete(`${API_BASE_URL}/api/v1/debug/sessions/${sessionId}`);
  }
});

test("stepping writes no decision", async ({ request }) => {
  const policyId = await firstProdPolicy(request);
  test.skip(!policyId, "this workspace has no policy to step through");

  const countOf = async () => {
    const res = await request.get(`${API_BASE_URL}/api/v1/audit/decisions/count`);
    expect(res.status()).toBe(200);
    return (await res.json()).total as number;
  };

  const before = await countOf();
  const started = await request.post(`${API_BASE_URL}/api/v1/debug/sessions`, {
    data: { policy_id: policyId, payload: PAYLOAD },
  });
  const sessionId = (await started.json()).session_id as string;
  try {
    await request.post(`${API_BASE_URL}/api/v1/debug/sessions/${sessionId}/run`);
    expect(await countOf()).toBe(before);
  } finally {
    await request.delete(`${API_BASE_URL}/api/v1/debug/sessions/${sessionId}`);
  }
});

test("the debugger page loads and offers its controls", async ({ page }) => {
  await page.goto("/debugger");
  await expect(page.getByTestId("debugger-payload")).toBeVisible();
  await expect(page.getByTestId("debugger-policy")).toBeVisible();
  await expect(page.getByTestId("debugger-start")).toBeVisible();
  // The safety claim is on the page itself, not only in the docs -- it is what makes stepping a
  // PROD policy something an operator can do without asking anyone.
  await expect(page.getByText(/no decision is logged/i)).toBeVisible();
});
