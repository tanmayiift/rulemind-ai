/**
 * Journeys (parity P15), end to end: create -> lock -> duplicate -> run it from an SDK.
 *
 * The fourth clause is the one worth an e2e test against a live API, because it crosses two
 * services: what a workspace authors here has to come back out of `/sdk/v1/experience-manifest`
 * in the camelCase shape `packages/sdk-flutter/example` reads, or a device silently keeps
 * rendering the built-in demo flows.
 *
 * Cleans up after itself — both the journey and its duplicate.
 */
import { expect, test } from "@playwright/test";

import { API_BASE_URL } from "./env";

const SCREENS = [
  { id: "landing", type: "landing", title: "Apply in minutes" },
  { id: "basics", type: "form", title: "About you", fields: [{ id: "age", label: "Age", kind: "number" }] },
];

test.describe.configure({ mode: "serial" });

test("create, lock, duplicate, and the SDK manifest serves it", async ({ request }) => {
  const created = await request.post(`${API_BASE_URL}/api/v1/journeys`, {
    data: { title: `e2e journey ${Date.now()}`, category: "Lending", policy_id: "policy_x", screens: SCREENS },
  });
  expect(created.status()).toBe(200, await created.text());
  const journey = await created.json();
  const madeIds: string[] = [journey.id];

  try {
    expect(journey.step_count).toBe(2);
    expect(journey.is_locked).toBe(false);
    expect(journey.version).toBe(1);

    // --- lock: freezes the screens, but is NOT the lifecycle -------------------------------
    const locked = await request.post(`${API_BASE_URL}/api/v1/journeys/${journey.id}/lock`);
    expect(locked.status()).toBe(200);
    expect((await locked.json()).is_locked).toBe(true);
    // The version tracks what a respondent sees, so locking must not move it.
    expect((await locked.json()).version).toBe(1);

    const rejected = await request.put(`${API_BASE_URL}/api/v1/journeys/${journey.id}`,
      { data: { screens: SCREENS.slice(0, 1) } });
    expect(rejected.status()).toBe(409);
    expect(await rejected.text()).toContain("uplicate");

    const promoted = await request.put(`${API_BASE_URL}/api/v1/journeys/${journey.id}`,
      { data: { status: "prod" } });
    expect(promoted.status(), "a locked journey must still be promotable").toBe(200);

    // --- duplicate: the way to change a live flow ------------------------------------------
    const duplicated = await request.post(
      `${API_BASE_URL}/api/v1/journeys/${journey.id}/duplicate`, { data: {} });
    expect(duplicated.status()).toBe(200, await duplicated.text());
    const copy = await duplicated.json();
    madeIds.push(copy.id);
    expect(copy.id).not.toBe(journey.id);
    expect(copy.is_locked, "the copy inherited the lock it exists to escape").toBe(false);
    expect(copy.status, "an unreviewed copy landed in prod").toBe("dev");
    expect(copy.screens).toEqual(SCREENS);

    // --- run it from an SDK -----------------------------------------------------------------
    const manifest = await request.get(`${API_BASE_URL}/sdk/v1/experience-manifest`);
    expect(manifest.status()).toBe(200);
    const served = (await manifest.json()).journeys as Array<Record<string, unknown>>;
    const mine = served.find((j) => j.id === journey.id);
    expect(mine, "the authored journey never reached the manifest").toBeTruthy();
    // camelCase, because app_state.dart reads item['policyId'] and item['stepCount'].
    expect(mine!.policyId).toBe("policy_x");
    expect(mine!.stepCount).toBe(2);
    expect(mine!.locked).toBe(true);
  } finally {
    for (const id of madeIds) {
      await request.post(`${API_BASE_URL}/api/v1/journeys/${id}/unlock`);
      await request.delete(`${API_BASE_URL}/api/v1/journeys/${id}`);
    }
  }
});

test("a locked journey cannot be deleted until it is unlocked", async ({ request }) => {
  const journey = await (await request.post(`${API_BASE_URL}/api/v1/journeys`, {
    data: { title: `e2e lockdel ${Date.now()}`, screens: SCREENS },
  })).json();

  try {
    await request.post(`${API_BASE_URL}/api/v1/journeys/${journey.id}/lock`);
    const refused = await request.delete(`${API_BASE_URL}/api/v1/journeys/${journey.id}`);
    expect(refused.status()).toBe(409);
    expect((await request.get(`${API_BASE_URL}/api/v1/journeys/${journey.id}`)).status()).toBe(200);
  } finally {
    await request.post(`${API_BASE_URL}/api/v1/journeys/${journey.id}/unlock`);
    await request.delete(`${API_BASE_URL}/api/v1/journeys/${journey.id}`);
  }
});

test("the journeys page loads and offers its controls", async ({ page }) => {
  await page.goto("/journeys");
  await expect(page.getByTestId("journey-title")).toBeVisible();
  await expect(page.getByTestId("journey-policy")).toBeVisible();
  await expect(page.getByTestId("journey-save")).toBeVisible();
});
