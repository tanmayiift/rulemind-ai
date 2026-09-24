/**
 * Signup and the auth gate (plan 6.6).
 *
 * There was no signup screen -- the onboarding state machine had no UI -- and no auth gate: an
 * anonymous visitor got the full navigation and 41 pages that each failed on their own 401. These
 * run as a visitor with NO credentials: the suite's default x-api-key header is removed and the
 * stored key is empty, so every request is authenticated only by what the page itself earned.
 */
import { expect, test, type Page } from "@playwright/test";

import { ALLOW_DESTRUCTIVE, API_BASE_URL, API_KEY, DESTRUCTIVE_SKIP } from "./env";

test.use({ extraHTTPHeaders: {} });

async function asAnonymousVisitor(page: Page) {
  // First visit only. An init script runs on EVERY page load, so an unconditional write would also
  // wipe whatever the app itself persisted later -- including the member a stale session leaves
  // behind, which is one of the things under test.
  await page.addInitScript(() => {
    if (!window.localStorage.getItem("rulemind-v3-ui")) {
      window.localStorage.setItem("rulemind-v3-ui", JSON.stringify({ state: { apiKey: "" }, version: 2 }));
    }
  });
}

test.describe("signup and the auth gate", () => {
  test.skip(!ALLOW_DESTRUCTIVE, DESTRUCTIVE_SKIP);

  test("an anonymous visitor is sent to sign in, not shown an app that fails", async ({ page }) => {
    await asAnonymousVisitor(page);
    await page.goto("/policies");
    await expect(page).toHaveURL(/\/login\?next=%2Fpolicies$/);
    await expect(page.getByRole("heading", { name: "Sign in to RuleMind" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Workflow Builder" }), "the app nav rendered for a visitor").toHaveCount(0);
  });

  test("a new workspace signs up, sees its key once, and completes setup", async ({ page, request }) => {
    const stamp = Date.now().toString(36);
    const email = `owner-${stamp}@signup.example`;
    const password = "correct horse battery staple";
    await asAnonymousVisitor(page);

    await page.goto("/signup");
    await page.getByLabel("Company").fill(`Signup Probe ${stamp}`);
    await page.getByLabel("Work email").fill(email);
    await page.getByLabel(/Password/).fill(password);
    await page.getByRole("button", { name: "Create workspace" }).click();

    const keyField = page.getByTestId("signup-api-key");
    await expect(keyField).toHaveValue(/^rm_live_/);
    const devKey = await keyField.inputValue();
    await page.getByTestId("signup-continue").click();

    // Signed in by the signup itself: the gate lets the owner through to setup.
    await expect(page).toHaveURL(/\/onboarding$/);
    const setup = page.getByTestId("workspace-setup");
    await expect(setup.locator('[data-step="org"]')).toHaveAttribute("data-done", "true");
    await page.getByTestId("setup-test-decision").click();
    await expect(setup.locator('[data-step="verified"]')).toHaveAttribute("data-done", "true");
    await page.getByTestId("setup-ai-decline").click();
    await expect(setup.locator('[data-step="ai"]')).toHaveAttribute("data-done", "true");
    await page.getByTestId("setup-prod-key").click();
    const prodKey = page.getByTestId("setup-prod-key-value");
    await expect(prodKey).toHaveValue(/^rm_live_/);
    await page.getByRole("button", { name: "I have stored it" }).click();
    await expect(setup.locator('[data-step="prod_key"]')).toHaveAttribute("data-done", "true");

    // Control: the NEW workspace, through its own dev key, agrees -- the page acted on it, not on
    // the suite's default tenant.
    const status = await (await request.get(`${API_BASE_URL}/api/v1/onboarding/status`, {
      headers: { "x-api-key": devKey } })).json();
    expect(status.complete).toBe(true);

    // Sign out, then sign in again and land where the gate sent us from.
    await page.context().clearCookies();
    await page.goto("/decision-explorer");
    await expect(page).toHaveURL(/\/login\?next=%2Fdecision-explorer$/);
    // Retried: typing before React hydrates the page is wiped when it does (controlled inputs).
    await expect(async () => {
      await page.getByLabel("Work email").fill(email);
      await page.getByLabel("Password").fill(password);
      await expect(page.getByLabel("Work email")).toHaveValue(email, { timeout: 500 });
    }).toPass({ timeout: 15_000 });
    // AFTER hydration: before it, the persisted member has not been restored yet, so "not greeted"
    // was true by construction and this passed with the stale member still stored.
    await expect(page.getByText("currently signed in"), "a dead session still greeted by name").toHaveCount(0);
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).toHaveURL(/\/decision-explorer$/);
  });

  test("signing in replaces a key the browser already holds", async ({ page, request }) => {
    // The suite's build ships a default dev key, stored in the browser. The API resolves a key
    // AHEAD of a session, so signing in as a member used to keep acting as the key's tenant.
    const stamp = Date.now().toString(36);
    const email = `keyed-${stamp}@signup.example`;
    const password = "correct horse battery staple";
    const created = await request.post(`${API_BASE_URL}/api/v1/onboarding/signup`, {
      data: { company: `Keyed ${stamp}`, contact_email: email, password } });
    expect(created.ok(), await created.text()).toBeTruthy();

    await page.addInitScript((key) => {
      if (!window.localStorage.getItem("rulemind-v3-ui")) {
        window.localStorage.setItem("rulemind-v3-ui", JSON.stringify({ state: { apiKey: key }, version: 2 }));
      }
    }, API_KEY);
    await page.goto("/login");
    await expect(async () => {
      await page.getByLabel("Work email").fill(email);
      await page.getByLabel("Password").fill(password);
      await expect(page.getByLabel("Work email")).toHaveValue(email, { timeout: 500 });
    }).toPass({ timeout: 15_000 });
    await page.getByRole("button", { name: "Sign in" }).click();
    await expect(page).not.toHaveURL(/\/login/);

    await page.goto("/onboarding");
    // A workspace made by signup has its org step done and no decisions yet; the key's tenant
    // (the seeded suite workspace) did not come through signup.
    await expect(page.getByTestId("workspace-setup").locator('[data-step="org"]')).toHaveAttribute("data-done", "true");
    await expect(page.getByTestId("workspace-setup").locator('[data-step="verified"]')).toHaveAttribute("data-done", "false");
  });
});
