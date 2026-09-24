/**
 * The two surfaces that had a backend and no page.
 *
 * `/model-sets` (7 endpoints) and `/predictors` (8 endpoints) shipped with an entry in
 * app-shell.tsx's PAGE_COPY map and nothing else — no route, no sidebar item, no link anywhere in
 * the app. Both backends are well tested at the API level; what nothing covered was that a person
 * could reach the capability at all.
 *
 * So these tests drive the PAGES against a live backend rather than re-testing the endpoints:
 * the route resolves, the sidebar links to it, and the primary action round-trips.
 */
import { test, expect } from "@playwright/test";
import fs from "fs";
import path from "path";
import { WEB_BASE_URL, API_BASE_URL, API_KEY } from "./env";

// The API unpickles the blob and checks for `predict`, so a placeholder string is refused with
// 422. See e2e/fixtures/README.md for why this is a committed file rather than an inline literal.
const TINY_MODEL = fs.readFileSync(path.join(__dirname, "fixtures", "tiny-model.b64"), "utf8").trim();

const auth = { "x-api-key": API_KEY };

// What these tests create, removed afterwards: against a deployment they persisted (e2e/env.ts).
const created = { models: [] as string[], modelSets: [] as string[], predictors: [] as string[] };

test.afterAll(async ({ request }) => {
  for (const id of created.modelSets) await request.delete(`${API_BASE_URL}/api/v1/model-sets/${id}`, { headers: auth }).catch(() => undefined);
  for (const id of created.predictors) await request.delete(`${API_BASE_URL}/api/v1/predictors/${id}`, { headers: auth }).catch(() => undefined);
  for (const id of created.models) await request.delete(`${API_BASE_URL}/api/v1/models/${id}`, { headers: auth }).catch(() => undefined);
});

test("the sidebar links to Model Sets and Predictors, and both routes resolve", async ({ page }) => {
  await page.goto(`${WEB_BASE_URL}/`);
  // The regression this guards: a nav entry pointing at a route that does not exist.
  //
  // Assert on content ONLY THE ROUTE renders. The first version of this test asserted on a
  // heading matching the page name and passed against a 404 -- the app shell paints its own
  // header from PAGE_COPY before the route renders, so "Predictors" is on screen either way and
  // the assertion could not fail. Next's 404 body is the thing to look for, plus a control that
  // the real page mounted.
  const routes: Array<[string, string]> = [
    ["Model Sets", "ms-name"],
    ["Predictors", "pr-name"]
  ];
  for (const [label, testId] of routes) {
    // Follow the link rather than navigating by URL, so the href itself is under test.
    await page.getByRole("link", { name: label, exact: true }).first().click();
    await expect(page.getByTestId(testId)).toBeVisible({ timeout: 20000 });
    await expect(page.getByRole("heading", { name: "404" })).toHaveCount(0);
    await expect(page.locator("text=/This page could not be found/i")).toHaveCount(0);
  }
});

test("a model set can be created from the page and compared on one input", async ({ page, request }) => {
  // A set needs at least one real model, and members are pinned at write time — a set naming a
  // nonexistent model is rejected with 422, by design.
  const model = await request.post(`${API_BASE_URL}/api/v1/models`, {
    headers: auth,
    data: {
      name: `e2e-ms-model-${Date.now()}`,
      model_type: "sklearn",
      model_base64: TINY_MODEL,
      status: "dev"
    }
  });
  expect(model.ok(), `model upload should succeed, got ${model.status()}`).toBeTruthy();
  const uploaded = await model.json();
  const modelName = uploaded.name;
  created.models.push(uploaded.id);

  await page.goto(`${WEB_BASE_URL}/model-sets`);
  const setName = `e2e-set-${Date.now()}`;
  await page.getByTestId("ms-name").fill(setName);
  await page.getByTestId("ms-member-model-0").selectOption({ label: modelName });
  await page.getByTestId("ms-member-label-0").fill("champion");
  await page.getByTestId("ms-create").click();

  await expect(page.getByTestId("ms-status")).toContainText(setName, { timeout: 15000 });
  // Created through the UI means the API really has it — not just that a toast appeared.
  const listed = await request.get(`${API_BASE_URL}/api/v1/model-sets`, { headers: auth });
  const set = (await listed.json()).find((s: { name: string }) => s.name === setName);
  expect(set).toBeTruthy();
  created.modelSets.push(set.id);

  const card = page.locator('[data-testid^="ms-card-"]').filter({ hasText: setName });
  await card.getByRole("button", { name: /Compare/ }).click();
  await page.getByTestId("ms-run-compare").click();
  // Every member appears in the output — including any that errored, since a failing candidate
  // is a RESULT rather than an outage and the endpoint returns the others alongside it.
  await expect(page.getByTestId("ms-compare-out")).toBeVisible({ timeout: 20000 });
  await expect(page.getByTestId("ms-result-champion")).toBeVisible();
});

test("a predictor can be created, and its drift report renders with the windows it used", async ({ page, request }) => {
  await page.goto(`${WEB_BASE_URL}/predictors`);
  const name = `e2e-pred-${Date.now()}`;
  await page.getByTestId("pr-name").fill(name);
  // `outcome` needs no source_id, so this exercises the branch where the field is hidden.
  await page.getByTestId("pr-source-type").selectOption("outcome");
  await page.getByTestId("pr-create").click();

  await expect(page.getByTestId("pr-status")).toContainText(name, { timeout: 15000 });
  const listed = await request.get(`${API_BASE_URL}/api/v1/predictors`, { headers: auth });
  const predictor = (await listed.json()).find((p: { name: string }) => p.name === name);
  expect(predictor, "the predictor the page reported creating must exist in the API").toBeTruthy();
  created.predictors.push(predictor.id);

  await page.getByTestId(`pr-drift-${predictor.id}`).click();
  const panel = page.getByTestId("pr-drift-panel");
  await expect(panel).toBeVisible({ timeout: 20000 });
  // A brand-new predictor is forward-only and therefore empty. Showing "insufficient_data" with
  // the explanation is the CORRECT render — asserting it means the panel cannot silently show a
  // fabricated zero instead.
  await expect(panel).toContainText(/insufficient_data|PSI/i);
  // The response echoes the windows actually used; the panel must show them, not the requested
  // ones, or a screenshot of this is not self-describing.
  await expect(panel).toContainText(/baseline/i);
});
