/**
 * Workflow Builder (`/workflow-builder`) -- THE policy canvas -- driven through a browser against a
 * live API.
 *
 * Build Studio (`/studio`) was merged into this page on 2026-09-24 and now redirects here. This
 * file was `sweep-ui-studio.spec.ts`; its four tests pinned behaviours that had to survive the
 * merge, and they are retargeted rather than deleted:
 *
 *   * saving a PROD policy must not demote it to DEV. `PUT /api/v1/policies/{id}` is a FULL
 *     REPLACE from PolicyUpsertRequest, whose `status` defaults to "dev" -- a client that omits
 *     it does not "leave it alone", it sets it to dev;
 *   * the save confirmation must survive the reload that save() itself triggers;
 *   * "+ New" must not be undone when the policy list finishes loading;
 *   * MECE validation must not be offered for a policy that does not exist yet.
 *
 * Workflow Builder had NO behavioural browser coverage before this -- only route-health rendered
 * it -- so these are also the first tests the surviving canvas has ever had. The tests after them
 * cover what the merge added and what it deliberately did not carry over.
 *
 * Non-destructive: builds and tears down its own fixture policies; never saves over seeded ones.
 */
import { expect, test, type Page } from "@playwright/test";

import { API_BASE_URL } from "./env";

const PROD_POLICY_ID = "zz_wb_prod";
const DEV_POLICY_ID = "zz_wb_dev";
const BRANCH_POLICY_ID = "zz_wb_branch";
const CREATED_NAME = "zz wb created from the canvas";

// Deliberately NOT `mode: "serial"`: with one worker it buys no isolation and only makes
// Playwright skip later tests after a failure, hiding findings behind the first.

async function someRuleId(request: { get: Function }): Promise<string> {
  const res = await request.get(`${API_BASE_URL}/api/v1/rules`);
  expect(res.status(), "seeded rules are required for a policy step").toBe(200);
  const rules = await res.json();
  expect(rules.length, "the suite needs at least one seeded rule").toBeGreaterThan(0);
  return rules[0].id;
}

async function makePolicy(
  request: { get: Function; post: Function; delete: Function },
  id: string,
  status: string,
  steps?: unknown[]
): Promise<void> {
  await request.delete(`${API_BASE_URL}/api/v1/policies/${id}`).catch(() => undefined);
  const ruleId = await someRuleId(request);
  const res = await request.post(`${API_BASE_URL}/api/v1/policies`, {
    data: {
      id,
      name: `zz wb ${status} ${id}`,
      status,
      defaultOutcome: "reject",
      trigger: { kind: "api" },
      steps: steps ?? [{ id: "s1", type: "rule", ref_id: ruleId }],
    },
  });
  expect(res.status(), `creating the ${status} fixture policy: ${await res.text()}`).toBe(200);
}

async function readPolicy(request: { get: Function }, id: string): Promise<Record<string, any>> {
  const res = await request.get(`${API_BASE_URL}/api/v1/policies/${id}`);
  expect(res.status()).toBe(200);
  return res.json();
}

async function openOn(page: Page, policyId: string) {
  await page.goto(`/workflow-builder?policy=${policyId}`);
  const picker = page.getByLabel("Policy", { exact: true });
  await expect(picker).toHaveValue(policyId, { timeout: 30_000 });
  return picker;
}

async function deleteById(request: { get: Function; put: Function; delete: Function }, id: string) {
  // Only DEV policies are deletable, so demote first.
  await request
    .put(`${API_BASE_URL}/api/v1/policies/${id}`, {
      data: { name: "zz cleanup", status: "dev", steps: [{ id: "s1", type: "outcome", outcome: "review" }] },
    })
    .catch(() => undefined);
  await request.delete(`${API_BASE_URL}/api/v1/policies/${id}`).catch(() => undefined);
}

test.afterAll(async ({ request }) => {
  for (const id of [PROD_POLICY_ID, DEV_POLICY_ID, BRANCH_POLICY_ID]) await deleteById(request, id);
  const all = await (await request.get(`${API_BASE_URL}/api/v1/policies`)).json().catch(() => []);
  for (const p of all as Array<{ id: string; name: string }>) {
    if (p.name?.startsWith("zz wb")) await deleteById(request, p.id);
  }
});

// ── carried over from Build Studio ────────────────────────────────────────────────────────

test("saving a PROD policy on the canvas does not demote it to DEV", async ({ page, request }) => {
  await makePolicy(request, PROD_POLICY_ID, "prod");
  await openOn(page, PROD_POLICY_ID);

  const name = page.getByLabel("Policy name");
  await expect(name).toHaveValue(`zz wb prod ${PROD_POLICY_ID}`);
  await name.fill("zz wb prod renamed");

  const saveRequest = page.waitForRequest(
    (r) => r.method() === "PUT" && r.url().includes(`/api/v1/policies/${PROD_POLICY_ID}`)
  );
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const body = JSON.parse((await saveRequest).postData() ?? "{}");
  expect(body.status, "PUT is a full replace and PolicyUpsertRequest defaults status to 'dev'").toBe("prod");

  await expect.poll(async () => (await readPolicy(request, PROD_POLICY_ID)).status, { timeout: 15_000 }).toBe("prod");
  const after = await readPolicy(request, PROD_POLICY_ID);
  expect(after.name).toBe("zz wb prod renamed");
  expect(after.defaultOutcome, "an untouched field must survive the save").toBe("reject");
  expect(after.trigger, "an untouched field must survive the save").toEqual({ kind: "api" });
});

test("the save confirmation survives the reload it triggers", async ({ page, request }) => {
  await makePolicy(request, DEV_POLICY_ID, "dev");
  await openOn(page, DEV_POLICY_ID);
  await page.getByLabel("Policy name").fill("zz wb dev renamed");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await expect(page.getByText(/Saved "zz wb dev renamed"/)).toBeVisible();
  await page.waitForTimeout(1500);
  await expect(page.getByText(/Saved "zz wb dev renamed"/),
    "save() reloads and reselects; the confirmation must not be wiped by that").toBeVisible();
  expect((await readPolicy(request, DEV_POLICY_ID)).name).toBe("zz wb dev renamed");
});

test("starting a new policy is not undone when the policy list finishes loading", async ({ page }) => {
  // Slow the policies GET so the race is deterministic rather than machine-speed dependent.
  await page.route("**/api/v1/policies", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await route.continue();
  });
  await page.goto("/workflow-builder");
  await page.getByRole("button", { name: "+ New" }).click();
  await page.getByLabel("Policy name").fill("zz wb untitled draft");
  await page.waitForTimeout(2500);

  expect(await page.getByLabel("Policy", { exact: true }).inputValue(),
    "the picker must stay on '— new policy —'; jumping to policies[0] also repoints Save at it").toBe("");
  expect(await page.getByLabel("Policy name").inputValue()).toBe("zz wb untitled draft");
});

test("MECE validation is not offered for a policy that does not exist yet", async ({ page }) => {
  await page.goto("/workflow-builder");
  await expect(page.getByLabel("Policy", { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "+ New" }).click();
  await page.getByRole("button", { name: "Rule", exact: true }).click();
  await expect(page.getByRole("button", { name: "Validate (MECE)" }),
    "analyze-mece resolves the policy id first; for an unsaved policy it can only 404").toBeDisabled();
});

// ── what the merge added ─────────────────────────────────────────────────────────────────

test("a policy can be CREATED on the canvas -- this page could only edit before", async ({ page, request }) => {
  await page.goto("/workflow-builder");
  await expect(page.getByLabel("Policy", { exact: true })).toBeVisible({ timeout: 30_000 });
  await page.getByRole("button", { name: "+ New" }).click();
  await page.getByLabel("Policy name").fill(CREATED_NAME);
  await page.getByRole("button", { name: "Outcome", exact: true }).click();

  const created = page.waitForRequest((r) => r.method() === "POST" && r.url().endsWith("/api/v1/policies"));
  await page.getByRole("button", { name: "Save", exact: true }).click();
  await created;
  await expect(page.getByText(new RegExp(`Saved "${CREATED_NAME}"`))).toBeVisible();

  const all = await (await request.get(`${API_BASE_URL}/api/v1/policies`)).json();
  const mine = all.find((p: { name: string }) => p.name === CREATED_NAME);
  expect(mine, "the new policy must exist server-side").toBeTruthy();
  expect(mine.steps.map((s: { type: string }) => s.type)).toEqual(["outcome"]);
});

test("steps can be reordered from the KEYBOARD", async ({ page, request }) => {
  const ruleId = await someRuleId(request);
  await makePolicy(request, DEV_POLICY_ID, "dev", [
    { id: "a", type: "rule", ref_id: ruleId, label: "first" },
    { id: "b", type: "outcome", outcome: "review", label: "second" },
  ]);
  await openOn(page, DEV_POLICY_ID);

  // Drag-and-drop alone made this impossible without a mouse. Tab to the button, press Enter.
  await page.getByRole("button", { name: "Move step 2 up" }).focus();
  await page.keyboard.press("Enter");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await expect.poll(async () => (await readPolicy(request, DEV_POLICY_ID)).steps.map((s: { id: string }) => s.id),
    { timeout: 15_000 }).toEqual(["b", "a"]);
});

test("a rule can be authored inline and is linked to the step", async ({ page, request }) => {
  await makePolicy(request, DEV_POLICY_ID, "dev", [{ id: "o", type: "outcome", outcome: "review" }]);
  await openOn(page, DEV_POLICY_ID);
  // Make the post-create reload SLOW, deterministically. The editor used to say "Rule created and
  // linked." and only link after reloading seven lists, so a Save in that window stored an
  // unlinked rule step. It reproduced on a cold first run and hid on a warm one -- the reload beat
  // the click -- so the first mutation check of this test PASSED with the bug restored. Slowing the
  // rules GET (only the GET; the create is a POST) holds the window open on every machine.
  await page.route("**/api/v1/rules", async (route) => {
    if (route.request().method() === "GET") await new Promise((resolve) => setTimeout(resolve, 2500));
    await route.continue();
  });
  await page.getByRole("button", { name: "Rule", exact: true }).click();

  const variables = await (await request.get(`${API_BASE_URL}/api/v1/variables`)).json();
  expect(variables.length, "inline rule authoring needs at least one variable").toBeGreaterThan(0);
  await page.getByLabel("New rule name").fill("zz wb inline rule");
  await page.getByLabel("Condition 1 variable").selectOption(variables[0].id);
  await page.getByLabel("Condition 1 value").fill("700");

  const created = page.waitForResponse((r) => r.request().method() === "POST" && r.url().endsWith("/api/v1/rules"));
  await page.getByRole("button", { name: "Create & link rule" }).click();
  const rule = await (await created).json();
  await expect(page.getByText("Rule created and linked.")).toBeVisible();

  await page.getByRole("button", { name: "Save", exact: true }).click();
  await expect.poll(async () => {
    const steps = (await readPolicy(request, DEV_POLICY_ID)).steps as Array<{ type: string; ref_id?: string }>;
    return steps.find((s) => s.type === "rule")?.ref_id;
  }, { timeout: 15_000 }).toBe(rule.id);

  await request.delete(`${API_BASE_URL}/api/v1/rules/${rule.id}`).catch(() => undefined);
});

test("a review gate's settings reach the saved policy", async ({ page, request }) => {
  // This page had NO editor for a review gate; the queue, role and SLA were unreachable.
  await makePolicy(request, DEV_POLICY_ID, "dev", [{ id: "o", type: "outcome", outcome: "review" }]);
  await openOn(page, DEV_POLICY_ID);
  await page.getByRole("button", { name: "Review gate", exact: true }).click();
  await page.getByLabel("Assign to queue").fill("zz_underwriting");
  await page.getByLabel("Role allowed to decide").fill("underwriter");
  await page.getByLabel("SLA (hours)").fill("12");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await expect.poll(async () => {
    const steps = (await readPolicy(request, DEV_POLICY_ID)).steps as Array<{ type: string; config?: any }>;
    return steps.find((s) => s.type === "review_gate")?.config;
  }, { timeout: 15_000 }).toMatchObject({ assignTo: "zz_underwriting", role: "underwriter", slaHours: 12 });
});

// ── what the merge deliberately did NOT carry over ─────────────────────────────────────────

test("a branch is saved in the shape the EXECUTOR routes on", async ({ page, request }) => {
  // Studio's branch editor wrote `config.condition` on the branch step. executor._execute_branch
  // reads `config.branches[].condition` and `config.default`, and Studio had no way to add a
  // branch -- so a branch built there routed NOTHING. This asserts the shape that actually runs.
  await makePolicy(request, BRANCH_POLICY_ID, "dev", [{ id: "o", type: "outcome", outcome: "review" }]);
  await openOn(page, BRANCH_POLICY_ID);
  await page.getByRole("button", { name: "Branch", exact: true }).click();
  await page.getByRole("button", { name: "+ Add branch" }).click();
  await page.getByPlaceholder("variables['x'] >= 700").fill("variables['bureau_score'] >= 700");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  await expect.poll(async () => {
    const steps = (await readPolicy(request, BRANCH_POLICY_ID)).steps as Array<{ type: string; config?: any }>;
    const branch = steps.find((s) => s.type === "branch");
    return branch?.config?.branches?.[0]?.condition;
  }, { timeout: 15_000 }).toBe("variables['bureau_score'] >= 700");
});

test("/studio redirects to the canvas instead of 404ing", async ({ page }) => {
  await page.goto("/studio");
  await expect(page).toHaveURL(/\/workflow-builder/);
  await expect(page.getByLabel("Policy", { exact: true })).toBeVisible({ timeout: 30_000 });
});

test("the Policies page opens the canvas ON the policy it is showing", async ({ page, request }) => {
  await makePolicy(request, BRANCH_POLICY_ID, "dev", [
    { id: "br", type: "branch", config: { branches: [], default: [] } },
    { id: "o", type: "outcome", outcome: "review" },
  ]);
  await page.goto(`/policies?policy=${BRANCH_POLICY_ID}`);
  const link = page.getByTestId("policy-step-open-canvas").first();
  await expect(link, "a step Policies cannot edit must offer the canvas").toBeVisible({ timeout: 30_000 });
  await link.click();
  await expect(page).toHaveURL(new RegExp(`/workflow-builder\\?policy=${BRANCH_POLICY_ID}`));
  await expect(page.getByLabel("Policy", { exact: true })).toHaveValue(BRANCH_POLICY_ID, { timeout: 30_000 });
});
