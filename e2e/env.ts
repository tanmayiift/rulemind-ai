/**
 * Where this suite points, and what it is allowed to destroy.
 *
 * The hosts used to be five separate string literals -- two in `playwright.config.ts`, one each
 * in `edge-cases.spec.ts`, `authz.spec.ts` and `authz-boundaries.spec.ts` -- so pointing the
 * suite at a deployed environment meant editing four files and hoping none was missed. A missed
 * one is silent: the specs that still say 127.0.0.1 fail to connect, or worse, hit a local server
 * left over from an earlier run and report on the wrong deployment. One module, imported by the
 * config and by every spec, is the only way that cannot happen.
 *
 * The defaults below are the exact literals those five sites carried, so a local run with no
 * RM_E2E_* variables set is byte-identical to what it was before.
 *
 * NOT collected as a test file: `testDir: "./e2e"` uses Playwright's default testMatch
 * (`**\/*.@(spec|test).?(c|m)[jt]s?(x)`), which `env.ts` does not match.
 */

const LOCAL_API = "http://127.0.0.1:8100";
const LOCAL_WEB = "http://127.0.0.1:3100";

// Deliberately NOT the shipped dev default (storage.py's rm_live_devlocaltenantkey...).
// The backend seeds whatever RULEMIND_DEV_API_KEY says, so a distinct literal here means
// this suite can never pass by accidentally inheriting the committed default.
const LOCAL_KEY = "rm_e2e_playwrightkey0000000000000000";

// A trailing slash would produce `${API_BASE_URL}/health` -> ".../\/health". More importantly it
// would make RM_E2E_API="http://127.0.0.1:8100/" compare unequal to LOCAL_API below, so the
// config would treat a local target as remote and refuse to start the servers.
const normalize = (url: string) => url.replace(/\/+$/, "");

export const API_BASE_URL = normalize(process.env.RM_E2E_API || LOCAL_API);
export const WEB_BASE_URL = normalize(process.env.RM_E2E_WEB || LOCAL_WEB);
export const API_KEY = process.env.RM_E2E_KEY || LOCAL_KEY;

// Derived, never configured separately. The WebSocket host was hardcoded `ws://127.0.0.1:8100`,
// which against a TLS deployment produces a plaintext handshake to an https origin -- browsers
// refuse it, and `authz-boundaries.spec.ts` would then read that refusal as "the collab socket
// rejects unauthenticated clients" while its positive control failed for an unrelated reason.
// http -> ws, https -> wss.
export const WS_BASE_URL = API_BASE_URL.replace(/^http/, "ws");

/**
 * Whether the API under test is the local dev server this config knows how to start.
 *
 * `playwright.config.ts` drops its whole `webServer` array when this is false: that array boots a
 * backend and a Next dev server with `reuseExistingServer: false`, and its backend command begins
 * with `rm -f` on the local SQLite file. Running that while pointed at a deployment would delete a
 * developer's local database for a run that never touches it.
 */
export const TARGETS_LOCAL_API = API_BASE_URL === LOCAL_API;

// A local API paired with a remote dashboard is the one combination that is both useless and
// destructive, so it is refused rather than run. Useless because the shipped dashboard bundle has
// its API host inlined at BUILD time (NEXT_PUBLIC_*), so it will not talk to 127.0.0.1 whatever
// this config says. Destructive because TARGETS_LOCAL_API is still true, which boots the
// `webServer` array -- and its backend command opens with `rm -f` on the developer's local SQLite
// file. Someone setting only RM_E2E_WEB, meaning "run against the deployment", would have their
// local database deleted by a run that then failed on CORS.
//
// The mirror case (remote API, local dashboard) is NOT refused: pointing a local Next dev server
// at a deployed API is a legitimate thing to want. It drops the whole webServer array, so the
// dashboard has to be started by hand, but nothing is deleted.
if (TARGETS_LOCAL_API && WEB_BASE_URL !== LOCAL_WEB) {
  throw new Error(
    `RM_E2E_WEB is set to ${WEB_BASE_URL} but RM_E2E_API still points at the local dev server ` +
      `(${LOCAL_API}). That combination would boot the local servers -- deleting the local SQLite ` +
      `database -- and then fail, because a deployed dashboard bundle has its API host compiled in. ` +
      `Set RM_E2E_API to the same deployment, or leave both unset for a local run.`
  );
}

/**
 * Whether the suite may perform writes that a real deployment cannot take back.
 *
 * Not a blanket read-only switch -- most of the suite creates rules and variables it can afford to
 * leave behind. This gates the three that a deployment cannot: self-serve tenant signup (permanent
 * tenants holding live API keys, with no delete endpoint), `POST /api/v1/import` (a global config
 * overwrite, not scoped to anything the test created), and deactivating the shared `device`
 * connector (restored only in a `finally` whose own failure is swallowed, so a timeout leaves it
 * off for every other user of that environment).
 */
export const ALLOW_DESTRUCTIVE = process.env.RM_E2E_ALLOW_DESTRUCTIVE === "1";

/** Reason string for the `test.skip()` guards, so a remote run says why rather than going green. */
export const DESTRUCTIVE_SKIP =
  "writes this environment cannot take back; set RM_E2E_ALLOW_DESTRUCTIVE=1 to run it";


/**
 * Mint a role-scoped key in the target workspace, and remember it so it can be revoked.
 *
 * Every spec that needs a non-owner principal used to POST `/api/v1/access/keys` inline and walk
 * away: five or six live author and viewer credentials accumulated in the target's default
 * workspace on every run. Locally that is invisible, because `playwright.config.ts` deletes the
 * database at boot. Against a deployment they persist, and they are real credentials -- the
 * `viewer` ones least interesting, the `policy_maker` ones able to author.
 *
 * ALLOW_DESTRUCTIVE deliberately does not gate this. Unlike self-serve signup, these ARE
 * removable: `DELETE /api/v1/access/keys/{kid}` exists (`app/routers/identity.py`). Gating them
 * would cost the suite most of its role coverage on a deployment for a mess that can simply be
 * cleaned up instead.
 */
const mintedKids: string[] = [];

export async function mintKey(
  request: { post: Function; delete: Function },
  role: string,
  name: string
): Promise<string> {
  const res = await request.post(`${API_BASE_URL}/api/v1/access/keys`, { data: { role, name } });
  if (res.status() !== 200) {
    throw new Error(`minting a ${role} key failed: ${res.status()} ${await res.text()}`);
  }
  const body = await res.json();
  if (!body.plaintext) {
    throw new Error("POST /api/v1/access/keys must return the plaintext key exactly once");
  }
  // Recorded BEFORE the caller can throw on it, so a failing assertion downstream still cleans up.
  if (body.kid) mintedKids.push(body.kid);
  return body.plaintext as string;
}

/**
 * Revoke everything `mintKey` handed out in this worker. Call from `test.afterAll`.
 *
 * Playwright runs each spec file in its own worker process, so `mintedKids` is per-file and each
 * file revokes exactly what it minted. Failures are swallowed on purpose: teardown that throws
 * turns a green run red for a key that a later run would simply create again, and the revoke can
 * legitimately 404 if the workspace was reset between the mint and here.
 */
export async function revokeMintedKeys(request: { delete: Function }): Promise<void> {
  while (mintedKids.length) {
    const kid = mintedKids.pop() as string;
    try {
      await request.delete(`${API_BASE_URL}/api/v1/access/keys/${kid}`);
    } catch {
      /* best effort: see above */
    }
  }
}
