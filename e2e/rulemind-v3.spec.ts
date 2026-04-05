import fs from "node:fs/promises";
import { expect, test } from "@playwright/test";

test.describe.configure({ mode: "serial" });

test("theme toggle switches the full shell and persists after reload", async ({ page }) => {
  await page.goto("/variables");

  const sidebar = page.getByTestId("sidebar-root");
  const editor = page.getByTestId("variable-code");
  const topbar = page.getByTestId("topbar-root");

  await expect(page.getByTestId("theme-toggle")).toHaveText("Dark");
  await expect(sidebar).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expect(topbar).toHaveCSS("background-color", "rgb(255, 255, 255)");
  await expect(editor).toHaveCSS("background-color", "rgb(248, 249, 252)");

  await page.getByTestId("theme-toggle").click();

  await expect(page.getByTestId("theme-toggle")).toHaveText("Light");
  await expect(sidebar).toHaveCSS("background-color", "rgb(19, 22, 32)");
  await expect(topbar).toHaveCSS("background-color", "rgb(23, 25, 35)");
  await expect(editor).toHaveCSS("background-color", "rgb(13, 15, 20)");

  await page.reload();
  await expect(page.getByTestId("theme-toggle")).toHaveText("Light");
  await expect(sidebar).toHaveCSS("background-color", "rgb(19, 22, 32)");
});

test("connector toggle hides the source filter and removes inactive variables from rule dropdowns", async ({ page }) => {
  await page.goto("/connectors");

  await page.getByTestId("connector-toggle-device").click();
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

  await page.goto("/connectors");
  await page.getByTestId("connector-toggle-device").click();
  await expect(page.getByTestId("app-notice")).toContainText("Connector updated.");
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
  await expect(page.getByTestId("app-notice")).toContainText("Variable promoted.");
  await page.waitForTimeout(500);

  await page.getByTestId("env-uat").click();
  await expect(page.getByTestId(`variable-list-item-${variableId}`)).toBeVisible();
  await page.getByTestId(`variable-list-item-${variableId}`).click();
  await expect(page.getByTestId("variable-name")).toHaveValue(variableName);

  await page.getByTestId("variable-promote").click();
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

  const payload = await preview.textContent();
  const importPath = testInfo.outputPath("rulemind-import.json");
  await fs.writeFile(importPath, payload ?? "{}");
  await page.getByTestId("import-json").setInputFiles(importPath);
  await expect(page.getByTestId("app-notice")).toContainText("Configuration imported.");
});
