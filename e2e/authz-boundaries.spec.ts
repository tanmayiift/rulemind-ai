import { expect, test, type APIRequestContext, type Page } from "@playwright/test";

/**
 * The boundaries that the 48 functional tests structurally cannot see (#139).
 *
 * `e2e/authz.spec.ts` covers authentication and role capability: no key, bad key, and a viewer
 * that can read but cannot author, promote or mint. This file covers the two boundaries that are
 * not about *who you are* but about *what you may reach* — the tenant boundary and the
 * WebSocket, both of which were open within living memory of this repo:
 *
 *   - the collab WebSocket called `accept()` before authenticating anything, so any client on
 *     the network could open `/ws/v1/collab/<policy id>` and receive a document snapshot;
 *   - RBAC's unknown-write default was AUTHOR, so every mutating prefix nobody had classified
 *     was granted to `policy_maker` on arrival — `/scim/v2` member provisioning included.
 *
 * Both are now closed in the backend and covered by Python tests. These assert the same
 * properties through the deployed HTTP and WebSocket surface, where a middleware ordering
 * mistake or a router mounted outside the guarded prefix would show up and a unit test would
 * not.
 *
 * ── Two conventions this file holds to ──────────────────────────────────────────────────────
 *
 * 1. **Every negative has a positive control.** A test asserting 404 passes just as happily
 *    when the route does not exist, the server is down, or the fixture was never created. Each
 *    block below first proves the thing IS reachable by the party that should reach it, so the
 *    refusal means refusal and not absence. This is the failure mode that let the original 48
 *    look green against an app with no authorization at all.
 *
 * 2. **404, not 403, across the tenant boundary.** A 403 says "this exists, you may not have
 *    it", which turns id enumeration into a workspace census: an attacker walks ids and reads
 *    off which ones came back 403. The status code IS the control here, so it is asserted
 *    exactly rather than as a range.
 *
 * Note on headers: `playwright.config.ts` sets `extraHTTPHeaders: {"x-api-key": …}` with the
 * default workspace's owner key, so any request meant to be unauthenticated or to act as some
 * other principal has to override it explicitly.
 */

const API = "http://127.0.0.1:8100";
const WS = "ws://127.0.0.1:8100";

// Deliberately NOT `test.describe.configure({ mode: "serial" })`. The config already pins
// `fullyParallel: false` and `workers: 1`, so these run one at a time regardless; the only thing
// serial mode would add is skip-the-rest-of-the-file-on-first-failure. That is the wrong
// behaviour for a security lane. Verified by running this file against AUTH_MODE=none: under
// serial mode the run reported 2 failures and skipped 8 tests, so "the tenant boundary is open"
// hid "the WebSocket is open" behind it. Every boundary reports its own verdict.
// Each test mints its own keys and workspaces, so there is no ordering dependency to preserve.

/**
 * A second workspace, via the public self-serve signup.
 *
 * `/api/v1/onboarding/signup` is in `middleware.EXEMPT_PATHS` and returns a live dev key for a
 * brand-new tenant, which is exactly what a cross-tenant test needs and the only way to get one
 * without platform-admin credentials. Nothing here depends on it staying public: if a
 * deployment sets `RULEMIND_SELF_SERVE_SIGNUP=0` the call 403s and these tests fail loudly
 * rather than silently degrading into single-tenant tests that prove nothing.
 */
async function signUpTenant(request: APIRequestContext, company: string) {
  const res = await request.post(`${API}/api/v1/onboarding/signup`, {
    data: { company, contact_email: `${company}@example.invalid`, use_case: "e2e authz boundary" }
  });
  expect(res.status(), `signup for ${company} failed: ${await res.text()}`).toBe(200);
  const body = await res.json();
  expect(body.api_key, "signup must return a live dev key").toBeTruthy();
  expect(body.tenant_id, "signup must return a tenant id").toBeTruthy();
  return body as { tenant_id: string; api_key: string };
}

/** Mint a key in the DEFAULT workspace at a given role, using the owner key from the config. */
async function mintKey(request: APIRequestContext, role: string, name: string) {
  const res = await request.post(`${API}/api/v1/access/keys`, { data: { role, name } });
  expect(res.status(), `minting a ${role} key failed: ${await res.text()}`).toBe(200);
  const key = (await res.json()).plaintext as string;
  expect(key, "the endpoint must return the plaintext key exactly once").toBeTruthy();
  return key;
}

/**
 * Attempt a WebSocket handshake from the browser and report whether it ever opened.
 *
 * Driven from `about:blank` deliberately. The page carries no cookies for the API origin, so
 * "unauthenticated" means unauthenticated — running this from the dashboard origin would leave
 * it ambiguous whether an `rm_session` cookie had ridden along and quietly authenticated the
 * handshake we are trying to prove gets refused.
 *
 * `opened` is the assertion that matters, not the close code. The endpoint closes with 1008
 * BEFORE `accept()`, which fails the HTTP upgrade itself; browsers surface that as an error and
 * a synthetic 1006, so pinning the code would be pinning a browser implementation detail rather
 * than the contract.
 */
async function handshake(page: Page, url: string): Promise<{ opened: boolean; code: number | null }> {
  return page.evaluate(
    (target) =>
      new Promise<{ opened: boolean; code: number | null }>((resolve) => {
        const socket = new WebSocket(target);
        let opened = false;
        let settled = false;
        const finish = (code: number | null) => {
          if (settled) return;
          settled = true;
          try {
            socket.close();
          } catch {
            /* already closing */
          }
          resolve({ opened, code });
        };
        socket.onopen = () => {
          opened = true;
          finish(null);
        };
        socket.onclose = (event) => finish(event.code);
        socket.onerror = () => {
          /* onclose always follows; the timeout is the real backstop */
        };
        setTimeout(() => finish(null), 8000);
      }),
    url
  );
}

// ── The tenant boundary ───────────────────────────────────────────────────────────────────────

test("one workspace cannot read another's policy, and the refusal is 404 not 403", async ({ request }) => {
  const created = await request.post(`${API}/api/v1/policies`, {
    data: { name: "tenant-a-secret-policy", steps: [{ id: "s1", type: "rule", ref: "r1" }] }
  });
  expect(created.status(), await created.text()).toBeLessThan(300);
  const policy = await created.json();

  // Positive control. Without it, every assertion below passes against a 404 that means
  // "no such route" rather than "not yours".
  const own = await request.get(`${API}/api/v1/policies/${policy.id}`);
  expect(own.status(), "the owning workspace must be able to read its own policy").toBe(200);

  const intruder = await signUpTenant(request, "authz-e2e-intruder");
  const asIntruder = { "x-api-key": intruder.api_key };

  // Second positive control: the intruder key is real and works in its own workspace. A key
  // that is simply broken would produce the same 404 and prove nothing about isolation.
  const ownListing = await request.get(`${API}/api/v1/policies`, { headers: asIntruder });
  expect(ownListing.status(), "the intruder key must be a working key in its own workspace").toBe(200);

  const cross = await request.get(`${API}/api/v1/policies/${policy.id}`, { headers: asIntruder });
  expect(cross.status(), "reading another workspace's policy must be 404").toBe(404);
  expect(cross.status(), "403 would confirm the policy exists, which is the fact 404 hides").not.toBe(403);
  expect(await cross.text(), "the refusal body must not leak the object either").not.toContain(
    "tenant-a-secret-policy"
  );
});

test("a listing never includes another workspace's objects", async ({ request }) => {
  // The other half of the boundary: a per-id 404 is worth nothing if the collection endpoint
  // hands the whole set over anyway.
  const created = await request.post(`${API}/api/v1/policies`, {
    data: { name: "tenant-a-listing-probe", steps: [{ id: "s1", type: "rule", ref: "r1" }] }
  });
  expect(created.status(), await created.text()).toBeLessThan(300);
  const policy = await created.json();

  const mine = await request.get(`${API}/api/v1/policies`);
  expect(mine.status()).toBe(200);
  expect(
    ((await mine.json()) as Array<{ id: string }>).map((p) => p.id),
    "positive control: the owner's own listing must contain it"
  ).toContain(policy.id);

  const intruder = await signUpTenant(request, "authz-e2e-lister");
  const theirs = await request.get(`${API}/api/v1/policies`, {
    headers: { "x-api-key": intruder.api_key }
  });
  expect(theirs.status()).toBe(200);
  const theirIds = ((await theirs.json()) as Array<{ id: string }>).map((p) => p.id);
  expect(theirIds, "another workspace's listing must not contain it").not.toContain(policy.id);
  expect(await theirs.text()).not.toContain("tenant-a-listing-probe");
});

// ── Identity surfaces are admin-only for writes ───────────────────────────────────────────────

test("an author-level key cannot provision members through /scim/v2", async ({ request }) => {
  // `policy_maker` is READ + DECIDE + AUTHOR. Under the old fail-open default an unmapped
  // mutating prefix resolved to AUTHOR, so this role could create — and deactivate — members,
  // the workspace owner included. This is that exact request.
  const author = await mintKey(request, "policy_maker", "e2e-author-scim");
  const asAuthor = { "x-api-key": author };

  // Positive control, and a precise statement of the contract: reads are READ, writes are
  // MANAGE_ACCESS. Asserting only the 403 would pass equally if someone blocked /scim entirely.
  const read = await request.get(`${API}/scim/v2/Users`, { headers: asAuthor });
  expect(read.status(), "GET /scim/v2/Users is a READ and stays available").toBe(200);

  const provision = await request.post(`${API}/scim/v2/Users`, {
    headers: asAuthor,
    data: {
      schemas: ["urn:ietf:params:scim:schemas:core:2.0:User"],
      userName: "escalation@example.invalid",
      active: true
    }
  });
  expect(provision.status(), "provisioning a member is MANAGE_ACCESS, not AUTHOR").toBe(403);
});

test("a viewer key cannot reach the platform admin surface", async ({ request }) => {
  const viewer = await mintKey(request, "viewer", "e2e-viewer-admin");
  const res = await request.get(`${API}/api/admin/v1/tenants`, { headers: { "x-api-key": viewer } });
  expect(
    res.status(),
    "/api/admin/v1 is the superadmin surface; a workspace key must not enumerate tenants"
  ).not.toBe(200);
});

// ── The collaborative-editing WebSocket ───────────────────────────────────────────────────────

test("the collab WebSocket refuses an unauthenticated handshake", async ({ page, request }) => {
  const doc = `authz-e2e-${Date.now()}`;
  const key = await mintKey(request, "policy_maker", "e2e-ws-author");

  await page.goto("about:blank");

  // Positive control FIRST, and it is doing real work here: if the port were wrong, the route
  // renamed, or the backend down, the unauthenticated case below would "pass" for the wrong
  // reason — which is precisely the bug class this file exists to catch.
  const authenticated = await handshake(page, `${WS}/ws/v1/collab/${doc}?api_key=${key}`);
  expect(authenticated.opened, "a credentialed handshake must still open the socket").toBe(true);

  const anonymous = await handshake(page, `${WS}/ws/v1/collab/${doc}`);
  expect(
    anonymous.opened,
    "an uncredentialed handshake must be refused before accept() -- it used to receive a full document snapshot"
  ).toBe(false);

  const badKey = await handshake(page, `${WS}/ws/v1/collab/${doc}?api_key=rm_live_notarealkey00000000000000000`);
  expect(badKey.opened, "an invalid key must be refused too, not merely a missing one").toBe(false);
});
