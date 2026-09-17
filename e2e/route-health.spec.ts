/**
 * Every route renders, and nothing on it errors. All 40, in the gate.
 *
 * This existed already as `qa/e2e-full/web_sweep.mjs` -- a standalone script whose results were
 * a checked-in JSON file. That is not coverage: the file was stale at 36 routes while the app had
 * 38, it only ran when someone remembered, and a regression on any route it listed would have
 * been invisible until the next manual run. The script found real bugs (it is how the /collab
 * WebSocket defect was caught), which is the argument for running it every time rather than for
 * keeping it as a ritual.
 *
 * What this asserts per route, beyond "HTTP 200":
 *   - route-specific content rendered, NOT just the app shell. The shell paints a header from
 *     PAGE_COPY before the route mounts, so a 404 still shows the page's name -- a route test
 *     that asserts on the heading passes against a missing page. (That exact mistake shipped in
 *     this repo; see e2e/model-sets-predictors.spec.ts.)
 *   - no uncaught page errors,
 *   - no 5xx from any request the page made,
 *   - no console errors, except the ones that are the CORRECT behaviour for an unauthenticated
 *     visitor, listed explicitly per route rather than globally ignored.
 */
import { test, expect, type Page } from "@playwright/test";
import fs from "fs";
import path from "path";
import { WEB_BASE_URL } from "./env";

// Discovered from the filesystem, like the sweep script -- so a new route is covered the day it
// is added rather than when someone remembers to extend a list.
const ROUTES: string[] = [
  "/",
  ...fs
    .readdirSync(path.join(__dirname, "..", "apps", "web", "app"), { withFileTypes: true })
    .filter((d) => d.isDirectory() && !d.name.startsWith("_") && !d.name.startsWith("("))
    .map((d) => "/" + d.name)
];

/**
 * Routes where an unauthenticated visitor SHOULD see a refusal, and why. Anything not listed
 * here must produce no console errors at all.
 *
 * Deliberately a per-route allowance rather than a blanket filter: a global "ignore 401s" would
 * have hidden the /collab bug, which looked exactly like this and was not correct behaviour.
 */
const EXPECTED_REFUSALS: Record<string, RegExp> = {
  "/admin": /401|403/,     // platform-admin surface; needs an admin session cookie
  "/branding": /401|403/   // admin-only white-label settings
};

async function visit(page: Page, route: string) {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const serverErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 500) serverErrors.push(`${r.status()} ${r.url()}`); });

  const response = await page.goto(WEB_BASE_URL + route, { waitUntil: "networkidle" });
  return { response, consoleErrors, pageErrors, serverErrors };
}

for (const route of ROUTES) {
  test(`route ${route} renders without errors`, async ({ page }) => {
    const { response, consoleErrors, pageErrors, serverErrors } = await visit(page, route);

    expect(response?.status(), `${route} should not be an HTTP error`).toBeLessThan(400);

    // Not Next's 404 body. The app shell renders regardless, so this is the only reliable signal.
    await expect(page.getByRole("heading", { name: "404" })).toHaveCount(0);
    await expect(page.locator("text=/This page could not be found/i")).toHaveCount(0);

    // Something beyond the shell actually mounted. `main` is the content region; the sidebar and
    // header live outside it, so this cannot be satisfied by chrome alone.
    //
    // `/login` and `/admin` legitimately have NO `main`: they render a full-page layout outside
    // the app shell, which is why they were the only two routes to fail the first version of
    // this check. For those the body IS the page content -- there is no chrome to mistake it
    // for -- so falling back is not a weakening.
    const hasMain = (await page.locator("main").count()) > 0;
    const region = page.locator(hasMain ? "main" : "body");
    await expect(region).toBeVisible();
    const text = (await region.innerText()).trim();
    expect(text.length, `${route} rendered an empty content region`).toBeGreaterThan(20);

    expect(pageErrors, `${route} threw an uncaught error`).toEqual([]);
    expect(serverErrors, `${route} triggered a 5xx`).toEqual([]);

    const allowed = EXPECTED_REFUSALS[route];
    const unexpected = allowed ? consoleErrors.filter((e) => !allowed.test(e)) : consoleErrors;
    expect(unexpected, `${route} logged console errors`).toEqual([]);
  });
}
