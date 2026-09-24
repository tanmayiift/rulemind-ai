import fs from "node:fs/promises";
import { expect, test } from "@playwright/test";

import { ALLOW_DESTRUCTIVE, DESTRUCTIVE_SKIP } from "./env";

test.describe.configure({ mode: "serial" });

// The colour literals below are the visual contract and must track
// apps/web/src/v3/theme.ts. They drifted when the UI overhaul (#15) changed the
// palette and were never updated, because this suite could not start at all —
// playwright.config.ts invoked a bare `python3`, which on macOS is 3.9 and cannot
// run the backend. Both are fixed; keep these in step with the theme.

/** Promotion is reviewed in a dialog that requires a reason (it was window.confirm()). */
async function confirmPromotion(page: import("@playwright/test").Page, reason = "e2e: promoting after a passing test run") {
  const dialog = page.getByRole("dialog");
  await expect(dialog).toBeVisible();
  await dialog.getByTestId("promotion-reason").fill(reason);
  await dialog.getByTestId("confirm-promotion").click();
  await expect(dialog).toBeHidden();
}

test("theme toggle switches the full shell and persists after reload", async ({ page }) => {
  await page.goto("/variables");

  const sidebar = page.getByTestId("sidebar-root");
  const editor = page.getByTestId("variable-code");
  const topbar = page.getByTestId("topbar-root");

  await expect(page.getByTestId("theme-toggle")).toHaveText("Dark");
  await expect(sidebar).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expect(topbar).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expect(editor).toHaveCSS("background-color", "rgb(246, 249, 252)");

  await page.getByTestId("theme-toggle").click();

  await expect(page.getByTestId("theme-toggle")).toHaveText("Light");
  await expect(sidebar).toHaveCSS("background-color", "rgb(14, 18, 32)");
  await expect(topbar).toHaveCSS("background-color", "rgb(17, 23, 38)");
  await expect(editor).toHaveCSS("background-color", "rgb(12, 16, 28)");

  await page.reload();
  await expect(page.getByTestId("theme-toggle")).toHaveText("Light");
  await expect(sidebar).toHaveCSS("background-color", "rgb(14, 18, 32)");
});

// This test mutates SHARED state — it deactivates the `device` connector — so it has to both
// assert its precondition and restore unconditionally. Neither was true before: it restored
// only on its last line, so any earlier failure left the connector off, and the next run
// toggled it back ON and failed again. That made the failure self-perpetuating and, because a
// --grep run inherits the same database, it failed standalone too once poisoned.
// (Issue #125 read the symptom as order coupling between tests. It is leakage between runs.
// The reset lives in the `rm -f` at the head of the backend's webServer command in
// playwright.config.ts -- NOT in a globalSetup: Playwright starts webServer BEFORE globalSetup,
// so unlinking from there would pull the file out from under an already-running backend. There
// is no e2e/global-setup.ts, and an earlier version of this comment said there was.)
test("connector toggle hides the source filter and removes inactive variables from rule dropdowns", async ({ page }) => {
  // The `finally` below is the whole reason this needs a gate. It restores the connector on a
  // best-effort basis and swallows its own failure, which is correct locally -- the database is
  // recreated at the head of every run, so a failed restore costs nothing. Against a deployment
  // there is no such reset: one timed-out run leaves the shared `device` connector deactivated
  // for everyone, and the variables that depend on it read "source inactive" until a human
  // notices. Skipped, not quietly dropped, so a remote run says the coverage is missing.
  test.skip(!ALLOW_DESTRUCTIVE, `deactivates the shared 'device' connector with a best-effort restore: ${DESTRUCTIVE_SKIP}`);

  // Switching the rule builder back to simple mode raises a window.confirm
  // (RulesPage.tsx switchBuilderMode). Playwright AUTO-DISMISSES dialogs when no handler is
  // registered, so confirm() returned false and the switch silently no-opped -- the click
  // "succeeded" and did nothing, then `rule-add-condition` never rendered and the test hung
  // for its full timeout. The other three tests in this file already register this; the
  // omission here is why only this one failed (#166).
  page.on("dialog", (dialog) => dialog.accept());

  await page.goto("/connectors");

  const deviceToggle = page.getByTestId("connector-toggle-device");
  // Assert the starting state instead of assuming it. Without this the test silently inverts
  // when the connector is already off, and the failure it then reports points at the variables
  // list rather than at the state it actually depends on.
  await expect(deviceToggle).toHaveAttribute("aria-checked", "true");

  try {
    await deviceToggle.click();
    await expect(page.getByTestId("app-notice")).toContainText("Connector updated.");

    await page.goto("/variables");
    await expect(page.getByTestId("variable-filter-device")).toHaveCount(0);
    await expect(page.getByTestId("variable-list-item-lending_apps")).toContainText("source inactive");

    await page.goto("/rules");
    await page.getByTestId("rule-clear").click();
    await page.getByRole("button", { name: "Simple" }).click();
    await page.getByTestId("rule-add-condition").click();

    const firstCondition = page.locator('select[data-testid^="rule-condition-variable-"]').first();
    const optionTexts = await firstCondition.locator("option").allTextContents();
    expect(optionTexts.join(" ")).not.toContain("Lending Apps");
    expect(optionTexts.join(" ")).not.toContain("Device Risk");

  } finally {
    // Restore in a finally so a failure above cannot poison the next run.
    //
    // Wrapped in its own try/catch on purpose: when the body times out, Playwright has
    // already closed the page, and an unguarded page.goto here throws "Target page ... has
    // been closed", REPLACING the real failure in the report. A cleanup step must never be
    // the reason you cannot see what actually broke.
    try {
      await page.goto("/connectors");
      if ((await deviceToggle.getAttribute("aria-checked")) === "false") {
        await deviceToggle.click();
        await expect(page.getByTestId("app-notice")).toContainText("Connector updated.");
      }
    } catch {
      // The database is reset at the start of every run (see playwright.config.ts), so a
      // failed restore cannot leak into the next run -- it is only a courtesy for the
      // remaining tests in THIS one.
    }
  }
});

test("variable creation, testing, promotion, and rule availability work end to end", async ({ page }) => {
  const variableName = `Income Multiplier ${Date.now()}`;

  await page.goto("/variables");
  await page.getByTestId("env-dev").click();
  await page.getByTestId("variable-new").click();
  await expect(page.getByTestId("variable-delete")).toHaveCount(0);
  await page.getByTestId("variable-name").fill(variableName);
  await page.getByTestId("variable-source").selectOption("bank");
  await page.getByTestId("variable-category").fill("Banking");
  await page.getByTestId("variable-code").fill(
    '@variable(source="bank")\n' +
      "def income_multiplier(payload, variables, apis):\n" +
      '    return round(payload["summary"]["avgBalance"] / 1000, 1)\n'
  );
  await expect(page.getByTestId("variable-name")).toHaveValue(variableName);
  await expect(page.getByTestId("variable-source")).toHaveValue("bank");
  await expect(page.getByTestId("variable-code")).toContainText("income_multiplier");

  await page.getByTestId("variable-test").click();
  await expect(page.getByTestId("variable-output")).toContainText("45.2");

  await page.getByTestId("variable-save").click();
  await expect(page.getByTestId("app-notice")).toContainText("Variable saved.");

  const variableId = variableName.toLowerCase().replace(/[^a-z0-9]+/g, "_").replace(/^_|_$/g, "");
  await expect(page.getByTestId(`variable-list-item-${variableId}`)).toBeVisible();
  await page.getByTestId(`variable-list-item-${variableId}`).click();
  await expect(page.getByTestId("variable-name")).toHaveValue(variableName);

  page.on("dialog", (dialog) => dialog.accept());

  await page.getByTestId("variable-promote").click();
  await confirmPromotion(page);
  await expect(page.getByTestId("app-notice")).toContainText("Variable promoted.");
  await page.waitForTimeout(500);

  await page.getByTestId("env-uat").click();
  await expect(page.getByTestId(`variable-list-item-${variableId}`)).toBeVisible();
  await page.getByTestId(`variable-list-item-${variableId}`).click();
  await expect(page.getByTestId("variable-name")).toHaveValue(variableName);

  await page.getByTestId("variable-promote").click();
  await confirmPromotion(page);
  await expect(page.getByTestId("app-notice")).toContainText("Variable promoted.");

  await page.getByTestId("env-prod").click();
  await expect(page.getByTestId(`variable-list-item-${variableId}`)).toBeVisible();

  await page.goto("/rules");
  await page.getByTestId("rule-clear").click();
  await page.getByRole("button", { name: "Simple" }).click();
  await page.getByTestId("rule-add-condition").click();
  const firstCondition = page.locator('select[data-testid^="rule-condition-variable-"]').first();
  await expect(firstCondition).toContainText(variableName);
});

test("multi-source rule flow saves, tests, and promotes correctly", async ({ page }) => {
  page.on("dialog", (dialog) => dialog.accept());
  const ruleName = `Composite Source Gate ${Date.now()}`;

  await page.goto("/rules");
  await page.getByTestId("env-dev").click();
  await page.getByTestId("rule-clear").click();
  await page.getByRole("button", { name: "Simple" }).click();
  await page.getByTestId("rule-name").fill(ruleName);

  await page.getByTestId("rule-add-condition").click();
  await page.getByTestId("rule-add-and").click();
  await page.getByTestId("rule-add-condition").click();
  await page.getByTestId("rule-add-and").click();
  await page.getByTestId("rule-add-condition").click();
  await page.getByTestId("rule-add-approve").click();

  const variableSelects = page.locator('select[data-testid^="rule-condition-variable-"]');
  await variableSelects.nth(0).selectOption("bureau_score");
  await variableSelects.nth(1).selectOption("avg_balance");
  await variableSelects.nth(2).selectOption("device_risk");

  const operatorSelects = page.locator("select").filter({ has: page.locator("option[value='>=']") });
  await operatorSelects.nth(0).selectOption(">=");
  await operatorSelects.nth(1).selectOption(">=");
  await operatorSelects.nth(2).selectOption("==");

  const valueInputs = page.locator("input[placeholder='Value']");
  await valueInputs.nth(0).fill("700");
  await valueInputs.nth(1).fill("20000");
  await valueInputs.nth(2).fill("0");

  await expect(page.getByTestId("rule-expression")).toContainText("Bureau Score");
  await expect(page.getByTestId("rule-expression")).toContainText("Avg Balance");
  await expect(page.getByTestId("rule-expression")).toContainText("Device Risk");
  await expect(page.getByTestId("rule-expression")).toContainText("APPROVE");

  await page.getByTestId("rule-test").click();
  await expect(page.getByTestId("rule-inline-outcome")).toContainText("approve");

  await page.getByTestId("rule-save").click();
  await expect(page.getByTestId("app-notice")).toContainText("Rule saved.");

  await page.getByRole("button", { name: "Test Console" }).click();
  await page.getByTestId("rules-test-select").selectOption({ label: ruleName });
  await page.getByTestId("rules-test-run").click();
  await expect(page.getByTestId("rule-saved-outcome")).toContainText("approve");

  await page.goto("/rules");
  await page.getByRole("button", { name: "Saved Rules" }).click();
  const savedCard = page.getByText(ruleName).locator("xpath=ancestor::div[contains(@style,'border-radius')]");
  await savedCard.getByRole("button", { name: "Promote" }).click();
  await confirmPromotion(page);
  await expect(page.getByTestId("app-notice")).toContainText("Rule promoted.");

  await page.getByTestId("env-uat").click();
  await expect(page.getByText(ruleName)).toBeVisible();
});

test("scorecard calculation, policy execution, and export/import round-trip work", async ({ page }, testInfo) => {
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto("/scorecards");
  await page.getByTestId("env-prod").click();
  await page.getByTestId("scorecard-select").selectOption("sc1");
  await page.getByTestId("scorecard-test").click();
  await expect(page.getByTestId("scorecard-live-score")).toHaveText("370");

  await page.goto("/policies");
  await page.getByTestId("env-uat").click();
  await page.getByTestId("policy-select").selectOption("policy_pl_underwriting");
  await page.getByTestId("policy-run").click();
  await expect(page.getByTestId("policy-outcome")).toContainText(/approve|reject/);
  await expect(page.getByTestId("policy-pipeline")).toContainText("Bureau");
  await expect(page.getByTestId("policy-pipeline")).toContainText("SCORECARD");

  await page.goto("/exports");
  await page.getByTestId("export-format-json").click();
  const preview = page.getByTestId("export-preview");
  await expect(preview).toContainText('"connectors"');
  await expect(preview).toContainText('"variables"');

  // The scorecard, policy and export assertions above run everywhere. They are not literally
  // read-only -- `policy-run` posts to /api/v1/policies/{id}/execute, which appends a decision and
  // an audit row -- but they are append-only telemetry in the workspace's own history rather than
  // configuration a deployment would have to undo, which is the distinction that matters here. The
  // import below is not: the file picker posts to /api/v1/import, which replaces the entire
  // configuration rather than merging the objects this test produced, and nothing undoes it.
  // Skipping HERE rather than at the top of the test keeps the read-only half covered against a
  // deployment while still reporting the round-trip as skipped instead of passed.
  test.skip(!ALLOW_DESTRUCTIVE, `POST /api/v1/import overwrites the entire configuration: ${DESTRUCTIVE_SKIP}`);

  const payload = await preview.textContent();
  const importPath = testInfo.outputPath("rulemind-import.json");
  await fs.writeFile(importPath, payload ?? "{}");
  await page.getByTestId("import-json").setInputFiles(importPath);
  await expect(page.getByTestId("app-notice")).toContainText("Configuration imported.");
});
