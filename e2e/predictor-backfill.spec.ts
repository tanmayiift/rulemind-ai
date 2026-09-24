/**
 * Backfilling a predictor from historical decisions (parity P12), end to end.
 *
 * The property worth checking against a live API is IDEMPOTENCE, because getting it wrong does
 * not fail — there is no unique constraint on (predictor_id, decision_id), so a second run
 * silently doubles the monitoring series and halves the apparent drift.
 *
 * Cleans up the predictor it creates; `DELETE /predictors/{id}` removes its recorded values too.
 */
import { expect, test } from "@playwright/test";

import { API_BASE_URL, TARGETS_LOCAL_API } from "./env";

test.describe.configure({ mode: "serial" });

test("a backfill is idempotent and reports what it did", async ({ request }) => {
  const created = await request.post(`${API_BASE_URL}/api/v1/predictors`, {
    data: { name: `e2e backfill ${Date.now()}`, source_type: "outcome" },
  });
  expect(created.status()).toBe(200, await created.text());
  const predictor = await created.json();

  try {
    const first = await request.post(
      `${API_BASE_URL}/api/v1/predictors/${predictor.id}/backfill`, { data: { limit: 200 } });
    expect(first.status()).toBe(200, await first.text());
    const one = await first.json();
    expect(one).toHaveProperty("scanned");
    expect(one).toHaveProperty("written");
    expect(one).toHaveProperty("skipped_already_recorded");

    const second = await request.post(
      `${API_BASE_URL}/api/v1/predictors/${predictor.id}/backfill`, { data: { limit: 200 } });
    expect(second.status()).toBe(200);
    const two = await second.json();

    // Nothing new is written for a decision already covered — the doubling this guards against
    // is silent. Every decision the second run scanned was already recorded: by the first run,
    // or, on a live deployment, as it was made (new decisions shift the scan window between the
    // two runs, which is why `skipped === one.written` only holds on a quiet database).
    expect(two.written).toBe(0);
    expect(two.skipped_already_recorded).toBe(two.scanned);
    expect(one.written + one.skipped_already_recorded).toBe(one.scanned);
    if (TARGETS_LOCAL_API) expect(two.skipped_already_recorded).toBe(one.written);
  } finally {
    await request.delete(`${API_BASE_URL}/api/v1/predictors/${predictor.id}`);
  }
});

test("backfilling an unknown predictor is a 404", async ({ request }) => {
  const response = await request.post(
    `${API_BASE_URL}/api/v1/predictors/does-not-exist/backfill`, { data: {} });
  expect(response.status()).toBe(404);
});
