/**
 * The decision-table spreadsheet import, driven through the BROWSER.
 *
 * `e2e/decision-table-import.spec.ts` covers this feature from the API side only -- both of its
 * tests use the `request` fixture and POST a ready-made `columns` + `rows` payload. But the
 * workbook is parsed in the browser and never reaches the API (see the note at
 * decision-tables/page.tsx:19), so the half that turns a FILE into that payload -- SheetJS,
 * `guessRole`, the header/variable matcher and the mapping table -- had no coverage at all. A
 * regression in any of it is invisible to the API tests, which hand the server a payload the UI
 * never produced.
 *
 * These tests start from a real file on the file input and stop at a table the server confirms.
 *
 * Non-destructive: creates two variables and one table, and deletes the table.
 */
import { expect, test } from "@playwright/test";

import { API_BASE_URL } from "./env";

const SCORE_VAR = "zz_sweep_dt_score";
const TIER_VAR = "zz_sweep_dt_tier";

// Deliberately NOT named after the variables. The mapping must be made in the UI; if the headers
// matched the variable ids, `guessRole`'s auto-binder (page.tsx:226) would do it and the test
// would pass without the selects ever being touched.
const SHEET = [
  "Applicant Rating,Customer Band,Premium,Decision",
  ">= 750,gold|platinum,9.5,approve",
  "600 - 749,*,12.5,review",
  "< 600,*,18,reject",
].join("\n");

/**
 * Create the variable if it is not already there, and PROVE it exists before any test runs.
 *
 * The verify step is not ceremony. `category` is required by VariableUpsertRequest and was
 * missing from the first draft of this helper; the POST 422'd, the `.catch` swallowed it, and
 * the only symptom was `selectOption` timing out sixty seconds later on an option that was
 * never rendered. Setup that fails quietly turns every test downstream into a coin flip.
 */
async function ensureVariable(
  request: { get: Function; post: Function },
  id: string,
  name: string
): Promise<void> {
  const res = await request.post(`${API_BASE_URL}/api/v1/variables`, {
    data: { id, name, source_id: "bureau", category: "bureau", code: "value = 0" },
  });
  if (res.status() !== 200) {
    // A duplicate is fine -- anything else is a broken fixture and must not be hidden.
    const existing = await request.get(`${API_BASE_URL}/api/v1/variables/${id}`);
    if (existing.status() !== 200) {
      throw new Error(`fixture variable ${id} could not be created: ${res.status()} ${await res.text()}`);
    }
  }
}

test.beforeAll(async ({ request }) => {
  await ensureVariable(request, SCORE_VAR, "ZZ Sweep Score");
  await ensureVariable(request, TIER_VAR, "ZZ Sweep Tier");
});

/**
 * Wait until React has hydrated and the variables fetch has landed.
 *
 * Without this the test is a coin flip: `setInputFiles` dispatches a change event, and if the
 * page has not hydrated yet there is no React onChange attached to receive it -- the file is
 * silently dropped and the assertion times out against a page that never heard about it. The
 * populated "bind variable" select is the signal, because it requires both hydration AND the
 * variables GET that `readSheet`'s auto-binder depends on.
 */
async function waitForInteractive(page: import("@playwright/test").Page) {
  await expect(
    page.locator('select.rm-select:has(option:text-is("— bind variable —"))').first()
  ).toBeVisible({ timeout: 30_000 });
  await expect
    .poll(
      async () =>
        page.locator('select.rm-select:has(option:text-is("— bind variable —"))').first().locator("option").count(),
      { timeout: 30_000 }
    )
    .toBeGreaterThan(1);
}

/** Load the sheet through the real <input type=file> and wait for the mapping table. */
async function loadSheet(page: import("@playwright/test").Page, csv = SHEET) {
  await page.goto("/decision-tables");
  await waitForInteractive(page);
  await page.getByTestId("dt-import-file").setInputFiles({
    name: "zz-sweep-pricing.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(csv),
  });
  await expect(page.getByText("Map the spreadsheet")).toBeVisible({ timeout: 20_000 });
}

test("a CSV chosen in the browser is parsed, mapped in the UI, and imported as a real table", async ({ page, request }) => {
  await loadSheet(page);

  // The parse happened client-side: every header from the file is on screen (each sits in a div
  // alongside a sample value from row 1, so this is a substring match, not an exact one), and the
  // import button counts the DATA rows (3), not the 4 lines in the file.
  for (const header of ["Applicant Rating", "Customer Band", "Premium", "Decision"]) {
    await expect(page.getByText(header, { exact: false }).first()).toBeVisible();
  }
  const importButton = page.getByTestId("dt-import-run");
  await expect(importButton).toHaveText(/Import 3 rows/);

  // `guessRole` must have read "Premium" and "Decision" as outputs from their names, and left
  // the two unrecognised columns as dimensions. Bind the two dimension columns. These selects only exist for role === "input", so finding
  // exactly two of them is itself the assertion that the roles were guessed correctly.
  const varSelects = page.locator('select.rm-select:has(option:text-is("Choose a variable…"))');
  await expect(varSelects, "exactly two columns must have been guessed as dimensions").toHaveCount(2);
  await varSelects.nth(0).selectOption(SCORE_VAR);
  await varSelects.nth(1).selectOption(TIER_VAR);

  // The mapping card's own "Table name" field. Located by the value `readSheet` prefills from
  // the file's basename (page.tsx:239) -- the editor below has a "Table name" field too, and
  // this is the only one that can be identified without depending on DOM order.
  const importName = page.locator('input[value="zz-sweep-pricing"]');
  await expect(importName, "the import name must be prefilled from the file name").toHaveCount(1);
  await importName.fill("zz sweep imported pricing");

  await importButton.click();

  // The page must reflect the created table, not just fire and forget.
  await expect(page.getByText(/Imported 3 rows/)).toBeVisible({ timeout: 20_000 });

  // And the server must actually hold it, with the mapping the UI made.
  const list = await (await request.get(`${API_BASE_URL}/api/v1/decision-tables`)).json();
  const created = list.find((t: { name: string }) => t.name === "zz sweep imported pricing");
  expect(created, "the imported table must exist server-side").toBeTruthy();
  expect(created.rows.length).toBe(3);
  expect(
    created.inputs.map((i: { variable_id: string }) => i.variable_id).sort(),
    "the variables bound in the mapping UI must be the ones persisted"
  ).toEqual([SCORE_VAR, TIER_VAR].sort());
  expect(created.inputs.map((i: { name: string }) => i.name)).toEqual(["Applicant Rating", "Customer Band"]);

  await request.delete(`${API_BASE_URL}/api/v1/decision-tables/${created.id}`).catch(() => undefined);
});

test("an unbound dimension is refused per-column, not as a wall of JSON", async ({ page }) => {
  await loadSheet(page);

  // Import WITHOUT binding either dimension. The API answers 422 with a `problems` list naming
  // each column; `apiJson` throws with the raw response TEXT, so the page has to JSON.parse its
  // own error message to recover that list (page.tsx:272). That recovery is the thing under test
  // -- if it breaks, the per-column detail silently degrades to a raw-JSON error banner.
  await page.getByTestId("dt-import-run").click();

  const panel = page.getByTestId("dt-import-problems");
  await expect(panel).toBeVisible({ timeout: 20_000 });
  await expect(panel).toContainText("Applicant Rating");
  await expect(panel).toContainText("Customer Band");
  await expect(panel).toContainText(/is not bound to a variable/);

  // The generic error card must NOT be how this is reported.
  await expect(panel).not.toContainText('{"detail"');
  await expect(page.getByText('{"detail":', { exact: false })).toHaveCount(0);
});

test("a file the parser cannot read is reported, and no mapping table is offered", async ({ page }) => {
  await page.goto("/decision-tables");
  await waitForInteractive(page);
  await page.getByTestId("dt-import-file").setInputFiles({
    name: "notes.txt",
    mimeType: "text/plain",
    buffer: Buffer.from("this is not a spreadsheet"),
  });

  await expect(page.getByText("Unsupported file", { exact: false })).toBeVisible({ timeout: 20_000 });
  await expect(
    page.getByText("Map the spreadsheet"),
    "a mapping table with no sheet behind it is a form about a file that does not exist"
  ).toHaveCount(0);
});
