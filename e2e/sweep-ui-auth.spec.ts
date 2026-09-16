/**
 * The sign-in screen, driven through a browser.
 *
 * Nothing in this repo had ever driven an auth flow through a browser: `authz.spec.ts` and
 * `authz-boundaries.spec.ts` probe the API with the `request` fixture, and the rest of the suite
 * rides the dev API key that `NEXT_PUBLIC_RULEMIND_DEV_API_KEY` puts in the store. So `/login`
 * itself -- the first screen a new user sees -- was exercised by nothing.
 *
 * `test.use({ extraHTTPHeaders: {} })` is REQUIRED here. The global config sends `x-api-key` on
 * every request including ones the page makes; a login test that authenticates by header is not
 * testing login. Clearing it makes the browser arrive the way a signed-out person does.
 */
import { expect, test } from "@playwright/test";

import { API_BASE_URL } from "./env";

const MEMBER_EMAIL = "zz-sweep-login@example.com";
const MEMBER_PASSWORD = "sweep-Passw0rd!";

// The page's own credential-bearing requests must not be silently authenticated by the harness.
test.use({ extraHTTPHeaders: {} });

test("a rejected sign-in explains itself in words, not raw JSON", async ({ page }) => {
  await page.goto("/login");
  await page.getByPlaceholder("you@company.com").fill("nobody@example.com");
  await page.locator('input[type="password"]').fill("definitely-wrong");
  await page.getByRole("button", { name: /Sign in/ }).click();

  // The API answers 401 {"detail":"Invalid email or password."} and `apiJson` rejects with the
  // raw response TEXT, so the un-decoded message is what reached the screen.
  await expect(page.getByText("Invalid email or password.")).toBeVisible({ timeout: 15_000 });
  await expect(
    page.getByText('{"detail"', { exact: false }),
    "the sign-in screen must not print the API's JSON envelope at the user"
  ).toHaveCount(0);

  // A failed sign-in must not navigate.
  await expect(page).toHaveURL(/\/login$/);
});

test("valid credentials sign the member in and leave the login screen", async ({ page, request }) => {
  // Created with the suite key via an explicit header -- `extraHTTPHeaders` is cleared for this
  // file, so the `request` fixture is unauthenticated unless it says otherwise.
  const key = process.env.RM_E2E_KEY || "rm_e2e_playwrightkey0000000000000000";
  const created = await request.post(`${API_BASE_URL}/api/v1/access/members`, {
    headers: { "x-api-key": key },
    data: { email: MEMBER_EMAIL, name: "ZZ Sweep Login", role: "viewer", password: MEMBER_PASSWORD },
  });
  // A rerun against a warm database will 409/422 on the duplicate; that is fine, the member
  // exists either way. Anything else is a broken fixture and must be loud.
  if (created.status() !== 200 && created.status() !== 409 && created.status() !== 422) {
    throw new Error(`could not create the login fixture member: ${created.status()} ${await created.text()}`);
  }

  await page.goto("/login");
  await page.getByPlaceholder("you@company.com").fill(MEMBER_EMAIL);
  await page.locator('input[type="password"]').fill(MEMBER_PASSWORD);

  const loginCall = page.waitForResponse(
    (r) => r.url().includes("/api/v1/auth/login") && r.request().method() === "POST"
  );
  await page.getByRole("button", { name: /Sign in/ }).click();

  const response = await loginCall;
  expect(response.status(), `sign-in should succeed: ${await response.text().catch(() => "")}`).toBe(200);

  // `finish()` stores the session and routes to "/". Landing anywhere but /login is the
  // observable contract; asserting the dashboard rendered would test the shell, not login.
  await expect(page).not.toHaveURL(/\/login/, { timeout: 15_000 });
  await expect(page.getByText("Sign in to RuleMind")).toHaveCount(0);
});

test("the Sign in button stays disabled until both fields are filled", async ({ page }) => {
  await page.goto("/login");
  const submit = page.getByRole("button", { name: /Sign in/ });

  await expect(submit, "no credentials typed yet").toBeDisabled();
  await page.getByPlaceholder("you@company.com").fill(MEMBER_EMAIL);
  await expect(submit, "email alone is not enough to attempt a sign-in").toBeDisabled();
  await page.locator('input[type="password"]').fill("x");
  await expect(submit).toBeEnabled();
});
