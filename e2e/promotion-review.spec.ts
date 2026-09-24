/**
 * Promotion is reviewed in a dialog (plan 6.5), not `window.confirm()`.
 *
 * Every page promoted through a native confirm and recorded a fixed reason -- "Manual promotion
 * from Deploy board." on every row of the promotion ledger. The ledger could say that someone
 * clicked, never why. These tests hold the properties that make the dialog worth having: nothing
 * is promoted until a reason is written, Escape really cancels, the written reason is what the
 * ledger records, and the ledger names the authenticated caller rather than a string the page sent.
 */
import { expect, test } from "@playwright/test";

import { ALLOW_DESTRUCTIVE, API_BASE_URL, DESTRUCTIVE_SKIP } from "./env";

const VARIABLE_ID = "zz_promotion_review_var";

test.describe("promotion review", () => {
  test.skip(!ALLOW_DESTRUCTIVE, DESTRUCTIVE_SKIP);

  test.beforeEach(async ({ request }) => {
    await request.delete(`${API_BASE_URL}/api/v1/variables/${VARIABLE_ID}`).catch(() => undefined);
    const created = await request.post(`${API_BASE_URL}/api/v1/variables`, {
      data: {
        id: VARIABLE_ID, name: "Promotion review probe", category: "Bureau", source_id: "bureau", status: "dev",
        code: "@variable(source=\"bureau\")\ndef zz_promotion_review_var(payload, variables, apis):\n    return 1\n",
      },
    });
    expect(created.ok(), await created.text()).toBeTruthy();
    // Promotable only after a passing test run -- the Deploy board's own rule.
    const tested = await request.post(`${API_BASE_URL}/api/v1/variables/${VARIABLE_ID}/test`, { data: { payload: {} } });
    expect((await tested.json()).result.passed).toBe(true);
  });

  test.afterEach(async ({ request }) => {
    await request.delete(`${API_BASE_URL}/api/v1/variables/${VARIABLE_ID}`).catch(() => undefined);
  });

  test("the deploy board promotes only after a written reason, and records it", async ({ page, request }) => {
    let nativeDialogs = 0;
    page.on("dialog", async (native) => { nativeDialogs += 1; await native.dismiss(); });
    await page.goto("/deploy");
    const card = page.locator("div").filter({ hasText: "Promotion review probe" }).filter({ has: page.getByRole("button", { name: "Promote" }) }).last();
    await card.getByRole("button", { name: "Promote" }).click();

    const dialog = page.getByRole("dialog", { name: "Promote Promotion review probe to UAT" });
    await expect(dialog).toBeVisible();
    const confirm = dialog.getByTestId("confirm-promotion");
    await expect(confirm, "promotion without a reason").toBeDisabled();
    await dialog.getByTestId("promotion-reason").fill("too short");
    await expect(confirm).toBeDisabled();

    // Escape is a real cancel: nothing is promoted.
    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    const stillDev = await (await request.get(`${API_BASE_URL}/api/v1/variables/${VARIABLE_ID}`)).json();
    expect(stillDev.status).toBe("dev");

    const reason = "UAT sign-off for ticket RISK-4821";
    await card.getByRole("button", { name: "Promote" }).click();
    await dialog.getByTestId("promotion-reason").fill(reason);
    await confirm.click();
    await expect(dialog).toBeHidden();
    await expect(page.getByText("Promoted Promotion review probe to UAT.")).toBeVisible();

    const promoted = await (await request.get(`${API_BASE_URL}/api/v1/variables/${VARIABLE_ID}`)).json();
    expect(promoted.status).toBe("uat");
    const ledger = (await (await request.get(`${API_BASE_URL}/api/v1/audit/promotions`)).json()) as Array<{
      entity_id: string; reason: string; promoted_by: string;
    }>;
    const row = ledger.find((entry) => entry.entity_id === VARIABLE_ID);
    expect(row?.reason, "the ledger records what the reviewer wrote").toContain(reason);
    expect(row?.promoted_by, "the actor is the authenticated caller, not a name the page sent").not.toBe("web");
    expect(nativeDialogs, "a native confirm() is still in the flow").toBe(0);
  });
});
