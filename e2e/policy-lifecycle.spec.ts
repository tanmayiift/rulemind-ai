/**
 * Release & impact on the Policies page (plan 6.5): what-if, shadow, releases + rollback, history.
 *
 * Each had a working API and no screen. Every test here drives the screen and then checks the API
 * agrees with what the screen said or did -- the page is the thing under test, the API is the
 * control.
 */
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

import { ALLOW_DESTRUCTIVE, API_BASE_URL, DESTRUCTIVE_SKIP } from "./env";

// One id per test: a policy promoted past dev cannot be deleted, so a shared id would collide
// across tests. The e2e database is recreated at the start of every run.
const freshId = (tag: string) => "zz_lifecycle_" + tag + "_" + Date.now().toString(36);

async function openPolicy(page: Page, env: "dev" | "uat" | "prod", policyId: string) {
  await page.goto("/policies");
  await page.getByTestId("env-" + env).click();
  await page.getByTestId("policy-select").selectOption(policyId);
  await expect(page.getByTestId("policy-lifecycle")).toBeVisible();
}

async function freshPolicy(request: APIRequestContext, id: string) {
  const created = await request.post(`${API_BASE_URL}/api/v1/policies`, {
    data: { id, name: "Lifecycle probe", status: "dev", defaultOutcome: "review",
            steps: [{ id: "s1", type: "outcome", ref_id: "approve", config: { outcome: "approve" }, label: "approve" }] },
  });
  expect(created.ok(), await created.text()).toBeTruthy();
  const ran = await request.post(`${API_BASE_URL}/api/v1/policies/${id}/execute`, { data: { payload: {} } });
  expect(ran.ok(), await ran.text()).toBeTruthy();
}

/** Record some decisions and compile a bundle -- what-if and replay both read through one. */
async function decisionsAndBundle(request: APIRequestContext) {
  for (let i = 0; i < 3; i += 1) {
    await request.post(`${API_BASE_URL}/api/v1/decide`, { data: { policy_id: "policy_travel_guard", payload: {} } });
  }
  const trigger = freshId("bundle_var");
  await request.post(`${API_BASE_URL}/api/v1/variables`, { data: {
    id: trigger, name: trigger, category: "Bureau", source_id: "bureau", status: "prod",
    code: `@variable(source="bureau")\ndef ${trigger}(payload, variables, apis):\n    return 1\n` } });
  await expect.poll(async () => ((await (await request.get(`${API_BASE_URL}/api/v1/bundles/versions`)).json()) as unknown[]).length,
                    { timeout: 30_000, message: "no bundle was compiled" }).toBeGreaterThan(0);
}

test.describe("policy release & impact", () => {
  test.skip(!ALLOW_DESTRUCTIVE, DESTRUCTIVE_SKIP);

  test.afterEach(async ({ request }) => {
    await request.delete(`${API_BASE_URL}/api/v1/policies/policy_travel_guard/shadow`).catch(() => undefined);
  });

  test("what-if runs from the page and reports what the API computes", async ({ page, request }) => {
    // What-if replays recorded decisions through a COMPILED bundle. A fresh e2e database has
    // neither, which would leave only the refusal path tested -- so record decisions, and compile
    // a bundle by creating a prod asset (the same trigger a real promotion to prod pulls).
    await decisionsAndBundle(request);

    await openPolicy(page, "prod", "policy_travel_guard");
    await page.getByRole("tab", { name: "What-if" }).click();
    await page.getByTestId("whatif-run").click();
    const result = page.getByTestId("whatif-result");
    await expect(result).toBeVisible({ timeout: 30_000 });
    // Control: the same request straight to the API.
    const api = await request.post(`${API_BASE_URL}/api/v1/policies/policy_travel_guard/whatif`, {
      data: { kpis: [{ name: "approval_rate", type: "outcome_rate", outcome: "approve" },
                     { name: "rejection_rate", type: "outcome_rate", outcome: "reject" }], sample: 200 },
    });
    expect(api.ok(), await api.text()).toBeTruthy();
    const body = await api.json();
    expect(body.scanned, "the replay must have real decisions to compare").toBeGreaterThan(0);
    await expect(result).toContainText(`${body.scanned} decision(s) replayed`);
    await expect(result.getByRole("row", { name: /approval_rate/ })).toBeVisible();
  });

  test("shadow starts and stops from the page", async ({ page, request }) => {
    await openPolicy(page, "prod", "policy_travel_guard");
    await page.getByRole("tab", { name: "Shadow" }).click();
    await page.getByTestId("shadow-candidate").selectOption("policy_sme_underwriting");
    await page.getByTestId("shadow-start").click();
    await expect(page.getByTestId("shadow-active")).toContainText("policy_sme_underwriting");
    expect((await (await request.get(`${API_BASE_URL}/api/v1/policies/policy_travel_guard/shadow`)).json()).active).toBe(true);

    await page.getByTestId("shadow-stop").click();
    await expect(page.getByTestId("shadow-candidate")).toBeVisible();
    expect((await (await request.get(`${API_BASE_URL}/api/v1/policies/policy_travel_guard/shadow`)).json()).active).toBe(false);
  });

  test("a release is rolled back through a reviewed dialog, and the ledger records why", async ({ page, request }) => {
    const FRESH = freshId("rollback");
    await freshPolicy(request, FRESH);
    const promoted = await request.post(`${API_BASE_URL}/api/v1/policies/${FRESH}/promote`, {
      data: { reason: "e2e baseline release" } });
    expect(promoted.ok(), await promoted.text()).toBeTruthy();

    await openPolicy(page, "uat", FRESH);
    await page.getByRole("tab", { name: "Releases" }).click();
    const table = page.getByTestId("release-table");
    await expect(table).toContainText("e2e baseline release");
    await table.getByRole("button", { name: "Roll back to this" }).first().click();

    const dialog = page.getByRole("dialog");
    await expect(dialog.getByTestId("confirm-rollback")).toBeDisabled();
    const reason = "Rolling back: approval spike in canary";
    await dialog.getByTestId("rollback-reason").fill(reason);
    await dialog.getByTestId("confirm-rollback").click();
    await expect(dialog).toBeHidden();

    const ledger = (await (await request.get(`${API_BASE_URL}/api/v1/audit/promotions`)).json()) as Array<{
      entity_id: string; reason: string | null; promoted_by: string;
    }>;
    const rollback = ledger.find((row) => row.entity_id === FRESH && row.reason === reason);
    expect(rollback, "the rollback's reason reached the ledger").toBeTruthy();
    expect(rollback?.promoted_by).not.toBe("system");
  });

  test("history compares two versions field by field", async ({ page, request }) => {
    const FRESH = freshId("history");
    await freshPolicy(request, FRESH);
    const updated = await request.put(`${API_BASE_URL}/api/v1/policies/${FRESH}`, {
      data: { name: "Lifecycle probe v2", status: "dev", defaultOutcome: "review",
              steps: [{ id: "s1", type: "outcome", ref_id: "approve", config: { outcome: "approve" }, label: "approve" }] },
    });
    expect(updated.ok(), await updated.text()).toBeTruthy();

    await openPolicy(page, "dev", FRESH);
    await page.getByRole("tab", { name: "History" }).click();
    const compare = page.getByTestId("history-compare");
    const nameRow = compare.getByRole("row", { name: /^name/ });
    await expect(nameRow).toContainText("Lifecycle probe");
    await expect(nameRow).toContainText("Lifecycle probe v2");
  });

  test("a decision replays against a bundle version from the Decision Explorer", async ({ page, request }) => {
    // A decision whose policy has CHANGED since, so the replay must report a different outcome --
    // with an unchanged policy a screen that always said "the same outcome" passed this test.
    const id = freshId("replay");
    const policy = (outcome: string) => ({
      id, name: "Replay probe", status: "prod", defaultOutcome: "review",
      steps: [{ id: "s1", type: "outcome", ref_id: outcome, config: { outcome }, label: outcome }],
    });
    const versions = async () => ((await (await request.get(`${API_BASE_URL}/api/v1/bundles/versions`)).json()) as unknown[]).length;
    const before = await versions();
    expect((await request.post(`${API_BASE_URL}/api/v1/policies`, { data: policy("approve") })).ok()).toBeTruthy();
    await expect.poll(versions, { timeout: 30_000 }).toBeGreaterThan(before);
    const decided = await (await request.post(`${API_BASE_URL}/api/v1/decide`, { data: { policy_id: id, payload: {} } })).json();
    const decisionId = decided.decision_id ?? decided.decisionId ?? decided.id;
    expect(decided.outcome).toBe("approve");
    const compiled = await versions();
    expect((await request.put(`${API_BASE_URL}/api/v1/policies/${id}`, { data: policy("reject") })).ok()).toBeTruthy();
    await expect.poll(versions, { timeout: 30_000 }).toBeGreaterThan(compiled);

    const api = await (await request.post(`${API_BASE_URL}/api/v1/decisions/${decisionId}/replay`)).json();
    expect(api.changed, "the control: the latest bundle must decide this differently").toBe(true);

    await page.goto("/decision-explorer");
    await page.getByText(String(decisionId).slice(0, 14)).first().click();
    await page.getByTestId("decision-replay").getByRole("button", { name: "Replay" }).click();
    const result = page.getByTestId("decision-replay-result");
    await expect(result).toContainText("Recorded approve");
    await expect(result).toContainText("decides reject");
    await expect(result).toContainText("a different outcome");
  });
});
