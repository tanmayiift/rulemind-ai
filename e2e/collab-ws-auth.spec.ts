/**
 * The collab page must actually connect — the browser half of the WebSocket contract.
 *
 * `authz-boundaries.spec.ts` proves the SERVER half: `?api_key=` opens, anonymous and bad-key
 * handshakes are refused. What nothing covered was whether the page sends a credential at all,
 * and it did not: `connect()` built the URL from `docId` and `actor` only.
 *
 * That is not a small omission, because the browser WebSocket API CANNOT SET HEADERS. The
 * `x-api-key` this page attaches to every other request is structurally unavailable on a
 * handshake, so an API-key-authenticated dashboard — the shipped compose configuration — could
 * never open the socket. `authenticate_websocket` rejects before `accept()`, so it surfaced as a
 * console 403 and the page's own generic "WebSocket error — is the backend running?", which
 * blames the backend for a client bug.
 *
 * Found by `qa/e2e-full/web_sweep.mjs` against the compose stack (the only route of 38 with a
 * network error), then confirmed against the running server before any code changed: no
 * credential -> HTTP 403; `?api_key=` -> an `init` frame.
 *
 * ── WHY THIS FILE CLEARS extraHTTPHeaders ────────────────────────────────────────────────────
 *
 * `playwright.config.ts` sets `extraHTTPHeaders: { "x-api-key": ... }` on every context. Playwright
 * applies those to the WebSocket UPGRADE request too — an ability no real browser has. With them
 * on, the page connects whether or not it sends a credential, so the first version of this test
 * PASSED AGAINST THE BROKEN CODE: the mutation check (drop the credential) did not fail it.
 *
 * Clearing them here is what makes the test able to fail. The page authenticates its own HTTP
 * calls from the store (`NEXT_PUBLIC_RULEMIND_DEV_API_KEY` -> `apiKey` -> `x-api-key` in api.ts),
 * so nothing else in the page needs the injected header.
 */
import { test, expect } from "@playwright/test";
import { WEB_BASE_URL } from "./env";

// Make the browser behave like a browser. See the block comment — without this the test cannot fail.
test.use({ extraHTTPHeaders: {} });

test("the collab page opens its WebSocket and reports itself live", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (m) => { if (m.type() === "error") consoleErrors.push(m.text()); });

  await page.goto(`${WEB_BASE_URL}/collab`);

  // "● Live" only after ws.onopen, which only fires if the handshake authenticated.
  await expect(page.getByTestId("conn-status")).toHaveText(/Live/i, { timeout: 20000 });

  // The failure showed up as a console 403, so assert it is gone rather than only that the
  // happy path works.
  expect(consoleErrors.filter((e) => /websocket/i.test(e) && /403|failed/i.test(e))).toEqual([]);
});
