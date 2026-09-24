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

// route -> what it rendered. Filled by the per-route tests, read by the uniqueness tests at the
// end. Safe because playwright.config.ts pins `workers: 1` and `fullyParallel: false`, so this
// file runs in one process, in order.
const RENDERED = new Map<string, { heading: string; body: string }>();

async function visit(page: Page, route: string) {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  const serverErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });
  page.on("pageerror", (e) => pageErrors.push(String(e)));
  page.on("response", (r) => { if (r.status() >= 500) serverErrors.push(`${r.status()} ${r.url()}`); });

  // `domcontentloaded`, then a BEST-EFFORT wait for the network to settle.
  //
  // This was `waitUntil: "networkidle"`, which made the whole check hostage to a dev server
  // going quiet. Under load, `/rules` -- the largest page in the app, 1400 lines plus the rule
  // tree editor -- exceeded the 60s test timeout while Next.js was still compiling it on first
  // visit, and the run failed with no assertion having been evaluated at all. The page was
  // fine: probed directly it loads in ~11s with no console errors, no page errors and nothing
  // pending. "The network went quiet" was never what this test is about.
  //
  // The listeners above are attached BEFORE the navigation, so console errors, uncaught errors
  // and 5xx responses are captured whenever they happen -- the idle wait only decides how long
  // to give the page to make its calls, and a timeout there is a slow machine, not a defect.
  const response = await page.goto(WEB_BASE_URL + route, { waitUntil: "domcontentloaded" });
  await page.waitForLoadState("networkidle", { timeout: 20_000 }).catch(() => {});
  return { response, consoleErrors, pageErrors, serverErrors };
}

for (const route of ROUTES) {
  test(`route ${route} renders without errors`, async ({ page }) => {
    // Triples the timeout. The webServer here is `next dev`, which compiles a route the first
    // time it is visited -- so the first load of a large page is a COMPILE, not a page load.
    // `/rules` is 1400 lines plus the rule tree editor and blew the 60s budget on a loaded
    // machine while Next was still building it; probed on its own it loads in ~11s clean.
    // Charging compilation to an assertion about rendering is measuring the wrong thing.
    test.slow();
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
    // Retried, because the idle wait above is now best-effort: on a slow first compile the
    // region can still be filling when we get here. `expect.poll` gives it the same patience
    // the old networkidle wait did, without making a timeout there a failure.
    await expect
      .poll(async () => (await region.innerText()).trim().length,
            { timeout: 20_000, message: `${route} rendered an empty content region` })
      .toBeGreaterThan(20);
    const text = (await region.innerText()).trim();

    expect(pageErrors, `${route} threw an uncaught error`).toEqual([]);
    expect(serverErrors, `${route} triggered a 5xx`).toEqual([]);

    const allowed = EXPECTED_REFUSALS[route];
    const unexpected = allowed ? consoleErrors.filter((e) => !allowed.test(e)) : consoleErrors;
    expect(unexpected, `${route} logged console errors`).toEqual([]);

    // What the route CALLS ITSELF. The shell paints this from PAGE_COPY keyed on the pathname,
    // so a route that renders another page's component still names itself here -- which is what
    // makes it a usable fingerprint below rather than just another shell assertion.
    const heading = (await page.locator("h1, h2").first().innerText().catch(() => "")).trim();
    RENDERED.set(route, { heading, body: text.slice(0, 400) });
  });
}

/**
 * NO TWO ROUTES MAY RENDER THE SAME PAGE.
 *
 * The per-route test above asserts the content region has more than 20 characters in it. A
 * route that renders a DIFFERENT route's page passes that trivially, and five of them do:
 * `/approvals` renders Deploy, `/segments` and `/workflows` and `/prototype` render the
 * Dashboard, `/batch` renders the Test Console. They are deliberate URL aliases -- kept out of
 * the sidebar, with the header naming what you are actually looking at -- so they are not the
 * "ghost routes" they have been described as. But the CLASS is real and nothing could see it:
 * add a new route tomorrow that silently renders an existing page and every check here passes.
 *
 * So the aliases are declared, and anything else that collides fails. Declaring them is the
 * point: the list is the difference between "this is intentional" and "nobody noticed".
 */
const DECLARED_ALIASES: Record<string, string> = {
  "/approvals": "/deploy",
  "/batch": "/test-console",
  "/segments": "/",
  "/workflows": "/",
  "/prototype": "/"
};

test("no two routes render the same page, except the declared aliases", async () => {
  const byBody = new Map<string, string[]>();
  for (const [route, { body }] of RENDERED) {
    if (!body) continue;
    const group = byBody.get(body) ?? [];
    group.push(route);
    byBody.set(body, group);
  }

  const undeclared: string[] = [];
  for (const group of byBody.values()) {
    if (group.length < 2) continue;
    // One of the colliding routes is the real page; every OTHER one has to declare it.
    for (const route of group) {
      const target = DECLARED_ALIASES[route];
      if (target && group.includes(target)) continue;
      if (!group.some((other) => other !== route && !DECLARED_ALIASES[other])) continue;
      if (!target) undeclared.push(`${route} renders the same page as ${group.filter((r) => r !== route).join(", ")}`);
    }
  }
  expect(undeclared,
    "these routes render another route's page and are not declared aliases -- either give them "
    + "their own page, redirect them, or add them to DECLARED_ALIASES with the route they mirror"
  ).toEqual([]);
});

test("every declared alias still points at a route that exists", async () => {
  // Otherwise the allow-list outlives the thing it allows, and a genuinely broken alias hides
  // behind an entry nobody re-checked -- the failure mode every allow-list in this repo has.
  const missing = Object.entries(DECLARED_ALIASES)
    .filter(([, target]) => !ROUTES.includes(target))
    .map(([route, target]) => `${route} -> ${target}`);
  expect(missing, "a declared alias points at a route that does not exist").toEqual([]);
});

test("every declared alias is actually still an alias", async () => {
  // The other direction: if someone gives /approvals its own page, this entry becomes a lie
  // that suppresses a real future collision. Shrink the list when the code shrinks it.
  const stale = Object.entries(DECLARED_ALIASES)
    .filter(([route, target]) => {
      const a = RENDERED.get(route);
      const b = RENDERED.get(target);
      return a && b && a.body && a.body !== b.body;
    })
    .map(([route, target]) => `${route} no longer renders the same page as ${target}`);
  expect(stale, "remove these from DECLARED_ALIASES -- they have their own page now").toEqual([]);
});
