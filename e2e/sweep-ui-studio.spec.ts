/**
 * Build Studio (`/studio`), driven through a browser against a live API.
 *
 * The page had ZERO browser coverage: `qa/e2e-full/web_sweep_results.json` records only that it
 * renders. Rendering is not the interesting part -- Save is. Studio deliberately lists policies
 * from EVERY environment (its own comment at page.tsx:119 defends this, so a live PROD policy
 * stays editable), which makes what Save transmits a production concern rather than a cosmetic
 * one.
 *
 * `PUT /api/v1/policies/{id}` is a FULL REPLACE built from `PolicyUpsertRequest`, whose `status`
 * field defaults to `"dev"` (main.py:348). A client that omits `status` therefore does not "leave
 * it alone" -- it sets it to dev. Studio omitted it, so saving a label change on a PROD policy
 * silently dropped that policy out of production. These tests pin the round trip: what you edit
 * changes, what you did not edit does not.
 *
 * Non-destructive: creates two policies of its own and deletes them. It never saves over the
 * seeded ones.
 */
import { expect, test } from "@playwright/test";

import { API_BASE_URL } from "./env";

const PROD_POLICY_ID = "zz_sweep_studio_prod";
const DEV_POLICY_ID = "zz_sweep_studio_dev";

// Deliberately NOT `mode: "serial"`. The config already runs one worker with fullyParallel off,
// so serial buys no isolation here -- it only makes Playwright SKIP the later tests once an
// earlier one fails, which hides two of the three findings behind the first and makes a mutation
// check unattributable. Each test below builds and tears down its own fixture policy.

/** A rule id that actually exists -- `validate_policy_steps` rejects a dangling rule ref. */
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
  status: string
): Promise<void> {
  await request.delete(`${API_BASE_URL}/api/v1/policies/${id}`).catch(() => undefined);
  const ruleId = await someRuleId(request);
  const res = await request.post(`${API_BASE_URL}/api/v1/policies`, {
    data: {
      id,
      name: `zz sweep studio ${status}`,
      status,
      defaultOutcome: "reject",
      trigger: { kind: "api" },
      steps: [{ id: "s1", type: "rule", ref_id: ruleId }],
    },
  });
  expect(res.status(), `creating the ${status} fixture policy: ${await res.text()}`).toBe(200);
  expect((await res.json()).status).toBe(status);
}

/** Read a policy back through the API -- the browser's own claim about what it saved is not evidence. */
async function readPolicy(request: { get: Function }, id: string): Promise<Record<string, unknown>> {
  const res = await request.get(`${API_BASE_URL}/api/v1/policies/${id}`);
  expect(res.status()).toBe(200);
  return res.json();
}

/** Open Studio and select `policyId` in the top-bar policy picker. */
async function openStudioOn(page: import("@playwright/test").Page, policyId: string) {
  await page.goto("/studio");
  const picker = page.locator("select").first();
  // The page auto-selects policies[0] once its seven parallel loads resolve; wait for the
  // option to exist rather than racing it.
  await expect(picker.locator(`option[value="${policyId}"]`)).toHaveCount(1, { timeout: 20_000 });
  await picker.selectOption(policyId);
  return picker;
}

test.afterAll(async ({ request }) => {
  for (const id of [PROD_POLICY_ID, DEV_POLICY_ID]) {
    // Only DEV policies are deletable (policies.py:125), so demote first -- which, if the bug
    // under test is present, has usually already happened.
    await request
      .put(`${API_BASE_URL}/api/v1/policies/${id}`, {
        data: { name: "zz sweep cleanup", status: "dev", steps: [{ id: "s1", type: "outcome", outcome: "review" }] },
      })
      .catch(() => undefined);
    await request.delete(`${API_BASE_URL}/api/v1/policies/${id}`).catch(() => undefined);
  }
});

test("saving a PROD policy in Studio does not demote it to DEV", async ({ page, request }) => {
  await makePolicy(request, PROD_POLICY_ID, "prod");

  await openStudioOn(page, PROD_POLICY_ID);

  // The smallest possible edit: rename. Nothing here touches status.
  const nameInput = page.getByPlaceholder("Policy name");
  await expect(nameInput).toHaveValue("zz sweep studio prod");
  await nameInput.fill("zz sweep studio prod renamed");

  const saveRequest = page.waitForRequest(
    (r) => r.method() === "PUT" && r.url().includes(`/api/v1/policies/${PROD_POLICY_ID}`)
  );
  await page.getByRole("button", { name: "Save", exact: true }).click();
  const sent = await saveRequest;

  // Assert on the wire, then on the server. The wire assertion is what names the defect: a
  // partial body against a full-replace endpoint.
  const body = JSON.parse(sent.postData() ?? "{}");
  expect(
    body.status,
    "Studio must transmit the policy's current status; PUT is a full replace and PolicyUpsertRequest defaults status to 'dev'"
  ).toBe("prod");

  await expect
    .poll(async () => (await readPolicy(request, PROD_POLICY_ID)).status, { timeout: 15_000 })
    .toBe("prod");

  const after = await readPolicy(request, PROD_POLICY_ID);
  expect(after.name, "the rename must still have been applied").toBe("zz sweep studio prod renamed");
  expect(after.defaultOutcome, "an untouched field must survive the save").toBe("reject");
  expect(after.trigger, "an untouched field must survive the save").toEqual({ kind: "api" });
});

test("Studio reports the save, and the confirmation survives the reload it triggers", async ({ page, request }) => {
  await makePolicy(request, DEV_POLICY_ID, "dev");

  await openStudioOn(page, DEV_POLICY_ID);
  await page.getByPlaceholder("Policy name").fill("zz sweep studio dev renamed");
  await page.getByRole("button", { name: "Save", exact: true }).click();

  // `save()` sets the status line and THEN re-selects the policy, and `selectPolicy` clears
  // status. The confirmation must still be on screen after that settles, or the user's only
  // signal that the write landed is a flash they may not see.
  await expect(page.getByText(/Saved "zz sweep studio dev renamed"/)).toBeVisible();
  await page.waitForTimeout(1500);
  await expect(
    page.getByText(/Saved "zz sweep studio dev renamed"/),
    "the save confirmation must not be wiped by the reload that save() itself kicks off"
  ).toBeVisible();

  expect((await readPolicy(request, DEV_POLICY_ID)).name).toBe("zz sweep studio dev renamed");
});

test("starting a new policy is not undone when the policy list finishes loading", async ({ page }) => {
  // The mount effect fires seven parallel GETs and then auto-selects policies[0]. Slowing the
  // policies GET makes the race deterministic instead of machine-speed dependent -- it was found
  // because this file's third test passed alone and failed when a warm Next bundle let the click
  // land before the list did.
  await page.route("**/api/v1/policies", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 1200));
    await route.continue();
  });

  await page.goto("/studio");
  await page.getByRole("button", { name: "+ New" }).click();
  await page.getByPlaceholder("Policy name").fill("zz sweep untitled draft");

  // Let the in-flight load resolve. The author's canvas must survive it.
  await page.waitForTimeout(2500);

  expect(
    await page.locator("select").first().inputValue(),
    "the picker must stay on '— new policy —'; jumping to policies[0] also repoints Save at that policy"
  ).toBe("");
  expect(
    await page.getByPlaceholder("Policy name").inputValue(),
    "the name the author typed must not be replaced by policies[0].name"
  ).toBe("zz sweep untitled draft");
});

test("Analyze MECE is not offered for a policy that does not exist yet", async ({ page }) => {
  await page.goto("/studio");
  await expect(page.locator("select").first()).toBeVisible({ timeout: 20_000 });

  // "+ New" clears policyId. Studio then built `/api/v1/policies/draft/analyze-mece`, which is a
  // 404 ("Policy not found.") -- the endpoint accepts posted steps for unsaved EDITS, but still
  // resolves the policy id first. Offering the button here can only produce a raw HTTP error in
  // the validation banner.
  await page.getByRole("button", { name: "+ New" }).click();
  await page.getByRole("button", { name: "Rule" }).click();

  const analyze = page.getByRole("button", { name: "Analyze MECE" });
  await expect(
    analyze,
    "Analyze MECE must be disabled until the policy exists server-side; /policies/draft/analyze-mece is a 404"
  ).toBeDisabled();
});
