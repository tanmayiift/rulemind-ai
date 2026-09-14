import { expect, test } from "@playwright/test";

import { API_BASE_URL, mintKey, revokeMintedKeys } from "./env";

// Three tests below mint viewer keys. Against a deployment those persist as real
// credentials unless something removes them, so they go through the shared helper that
// records each kid, and this hook revokes them when the file finishes.
test.afterAll(async ({ request }) => {
  await revokeMintedKeys(request);
});

/**
 * Authorization is actually enforced (#139).
 *
 * Until now `playwright.config.ts` set AUTH_MODE=none on both servers, which makes
 * middleware.py early-return before the API-key check, RBAC, CSRF and tenant resolution.
 * All 48 browser tests therefore ran against an app with no authorization — they could not
 * have failed on an authz regression, because there was no authz to regress.
 *
 * Turning auth on fixes that, but on its own it only proves the happy path: 48 tests that
 * authenticate correctly still cannot tell "authz works" from "authz is off". These seven
 * negatives are the part that can actually fail. The last one — a viewer minting itself an
 * owner key — is the privilege escalation, and is the reason this file exists.
 *
 * Note on headers: playwright.config.ts sets `extraHTTPHeaders: {"x-api-key": …}` globally,
 * so every test below that wants to be unauthenticated must override it explicitly.
 */

// Was a hardcoded "http://127.0.0.1:8100". RM_E2E_API overrides it; the default is unchanged.
const API = API_BASE_URL;
const BAD_KEY = "rm_live_notarealkey00000000000000000";

// `test.describe.configure({ mode: "serial" })` was removed here (2026-09-04). The config already
// sets `fullyParallel: false` and `workers: 1`, so these were never running concurrently — serial
// mode was contributing only its skip-the-rest-on-first-failure semantics, and every test below
// mints its own key, so there is no ordering dependency it was protecting. Measured against
// AUTH_MODE=none: with serial mode the suite reported 1 failure here and skipped the other 4, so a
// single broken boundary concealed the state of the rest. A security lane should report every
// boundary it can see, not stop at the first one.

test("a request with no API key is rejected", async ({ request }) => {
  const res = await request.get(`${API}/api/v1/rules`, { headers: { "x-api-key": "" } });
  expect(res.status()).toBe(401);
});

test("a request with an invalid API key is rejected", async ({ request }) => {
  const res = await request.get(`${API}/api/v1/rules`, { headers: { "x-api-key": BAD_KEY } });
  expect(res.status()).toBe(401);
});

test("health probes stay open without a key", async ({ request }) => {
  // Pins EXEMPT_PATHS. A load balancer polling /health must not need a credential — and
  // equally, this asserts the exemption list has not silently widened to anything else.
  for (const path of ["/health", "/ready", "/metrics"]) {
    const res = await request.get(`${API}${path}`, { headers: { "x-api-key": "" } });
    expect(res.status(), `${path} should be exempt`).toBe(200);
  }
});

test("a viewer key can read but cannot author, promote, or mint keys", async ({ request }) => {
  // Minted with the owner key that extraHTTPHeaders supplies.
  const viewer = await mintKey(request, "viewer", "e2e-viewer");
  const asViewer = { "x-api-key": viewer };

  // READ is allowed.
  const read = await request.get(`${API}/api/v1/rules`, { headers: asViewer });
  expect(read.status()).toBe(200);

  // AUTHOR is not.
  const author = await request.post(`${API}/api/v1/rules`, {
    headers: asViewer,
    data: { name: "viewer-should-not-create", outcome: "approve", nodes: [{ id: "n1", type: "approve" }] }
  });
  expect(author.status(), "a viewer must not be able to author rules").toBe(403);

  // …and the refusal must not have a side effect.
  const listing = await request.get(`${API}/api/v1/rules`);
  const names = ((await listing.json()) as Array<{ name: string }>).map((r) => r.name);
  expect(names).not.toContain("viewer-should-not-create");

  // PRIVILEGE ESCALATION: a viewer minting itself a key is the one that matters most.
  const escalate = await request.post(`${API}/api/v1/access/keys`, {
    headers: asViewer,
    data: { role: "owner", name: "escalation-attempt" }
  });
  expect(escalate.status(), "a viewer must not be able to mint an owner key").toBe(403);
});

test("a viewer key cannot promote", async ({ request }) => {
  const viewer = await mintKey(request, "viewer", "e2e-viewer-promote");

  const rules = await request.get(`${API}/api/v1/rules`);
  const first = ((await rules.json()) as Array<{ id: string }>)[0];
  test.skip(!first, "no rules seeded");

  const res = await request.post(`${API}/api/v1/rules/${first.id}/promote`, {
    headers: { "x-api-key": viewer }
  });
  expect(res.status(), "promotion is an author/deploy capability").toBe(403);
});
