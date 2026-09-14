/**
 * Importing a spreadsheet as a decision table (parity P07), end to end.
 *
 * The workbook is parsed in the browser, so what crosses the wire is a grid plus a mapping. This
 * exercises that contract against a live API: a three-dimension pricing matrix in, a table that
 * DECIDES out, and the optimiser reporting the holes the sheet actually has.
 *
 * Non-destructive apart from the table it creates, which it deletes.
 */
import { expect, test } from "@playwright/test";

import { API_BASE_URL } from "./env";

const COLUMNS = [
  { header: "Score", role: "input", variable_id: "e2e_import_score" },
  { header: "Tier", role: "input", variable_id: "e2e_import_tier" },
  { header: "Price", role: "output" },
  { header: "Decision", role: "output", type: "outcome" },
];

const ROWS = [
  { Score: ">= 750", Tier: "gold|platinum", Price: 9.5, Decision: "approve" },
  { Score: "600 - 749", Tier: "*", Price: 12.5, Decision: "review" },
  { Score: "< 600", Tier: "*", Price: 18, Decision: "reject" },
];

test.describe.configure({ mode: "serial" });

test("a spreadsheet becomes a decision table that decides, with its gaps reported", async ({ request }) => {
  const created = await request.post(`${API_BASE_URL}/api/v1/decision-tables/import`, {
    data: { name: `e2e import ${Date.now()}`, columns: COLUMNS, rows: ROWS },
  });
  expect(created.status()).toBe(200, await created.text());
  const table = await created.json();

  try {
    expect(table.inputs).toHaveLength(2);
    expect(table.rows).toHaveLength(3);

    // Types come off the DATA, and they are not cosmetic: the optimiser reasons over intervals,
    // so a numeric column typed as a string makes every row "not analyzable" and the gap
    // analysis silently reports nothing.
    const scoreInput = table.inputs.find((i: { name: string }) => i.name === "Score");
    expect(scoreInput.field_type).toBe("number");
    const price = table.outputs.find((o: { name: string }) => o.name === "Price");
    expect(price.type).toBe("number");
    expect(["outcome", "string", "number", "boolean"]).toContain(price.type);

    const kinds = new Set(
      (table.analysis?.diagnostics ?? []).map((d: { type: string }) => d.type));
    expect(kinds.has("not_analyzable"),
      "the analyzer could not reason about a numeric column — check field_type").toBe(false);

    // And it decides: a gold applicant at 800 takes the first row's price.
    const run = await request.post(
      `${API_BASE_URL}/api/v1/decision-tables/${table.id}/evaluate`,
      { data: { variable_values: { e2e_import_score: 800, e2e_import_tier: "gold" } } });
    expect(run.status()).toBe(200, await run.text());
    const result = await run.json();
    expect(result.outcome).toBe("approve");
    expect(result.outputs.out_price).toBe(9.5);
  } finally {
    await request.delete(`${API_BASE_URL}/api/v1/decision-tables/${table.id}`);
  }
});

test("a sheet with bad cells is refused with every problem, and nothing is created", async ({ request }) => {
  const before = await (await request.get(`${API_BASE_URL}/api/v1/decision-tables`)).json();

  const response = await request.post(`${API_BASE_URL}/api/v1/decision-tables/import`, {
    data: {
      name: `e2e broken ${Date.now()}`,
      columns: COLUMNS,
      rows: [
        { Score: ">=", Tier: "gold", Price: 1, Decision: "approve" },
        { Score: "749 - 600", Tier: "gold", Price: 1, Decision: "approve" },
        { Score: "700", Tier: "gold", Price: 1, Decision: "maybe" },
      ],
    },
  });

  expect(response.status()).toBe(422);
  const problems = (await response.json()).detail.problems;
  // All three at once. One-at-a-time errors turn a 40-row sheet into 40 round trips.
  expect(problems).toHaveLength(3);
  for (const problem of problems) expect(problem).toHaveProperty("row");

  // Refused, not partially applied: a skipped cell is a WILDCARD, so a half-imported row would
  // apply its price to every input instead of the one the sheet meant.
  const after = await (await request.get(`${API_BASE_URL}/api/v1/decision-tables`)).json();
  expect(after.length).toBe(before.length);
});
