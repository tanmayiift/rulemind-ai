import path from "node:path";

import { defineConfig, devices } from "@playwright/test";

const apiBaseUrl = "http://127.0.0.1:8100";
// The backend needs Python >= 3.10, but a bare `python3` is often an older system
// interpreter (macOS ships 3.9), which fails to start the app and leaves the whole
// suite unrunnable locally. Point PYTHON_BIN at a venv to override.
const pythonBin = process.env.PYTHON_BIN || "python3";
const webBaseUrl = "http://127.0.0.1:3100";

// Deliberately NOT the shipped dev default (storage.py's rm_live_devlocaltenantkey...).
// The backend seeds whatever RULEMIND_DEV_API_KEY says, so a distinct literal here means
// this suite can never pass by accidentally inheriting the committed default.
const e2eApiKey = "rm_e2e_playwrightkey0000000000000000";

// ABSOLUTE on purpose. This used to be the relative "sqlite:///.runtime/playwright-v3.db",
// which the backend resolves against ITS cwd (apps/python-executor) rather than the repo
// root — so `rm .runtime/playwright-v3.db` from the root deleted a file nothing used, and
// "I tested it on a fresh database" was not true. That misdirection is most of why #125 was
// diagnosed as order coupling.
const e2eDbPath = path.join(__dirname, "apps", "python-executor", ".runtime", "playwright-v3.db");

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 60_000,
  expect: {
    timeout: 10_000
  },
  use: {
    baseURL: webBaseUrl,
    // The 33 `request`-fixture tests in edge-cases.spec.ts sent no auth header at all --
    // they passed only because AUTH_MODE=none disabled the check. One line here beats
    // editing 33 call sites, and a per-request `headers:` still overrides it (which the
    // negative tests in authz.spec.ts depend on).
    extraHTTPHeaders: { "x-api-key": e2eApiKey },
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure"
  },
  // Two lanes over one server pair. Splitting the SERVERS is not possible here -- `webServer` is
  // global to the config, not per-project -- but it is also not needed any more: AUTH_MODE is
  // unset below rather than "none", so authorization is live for every test in both lanes.
  //
  // The split is about signal and cost, not configuration. The functional suite is 48 browser
  // tests that navigate, render and assert on the dashboard; the security lane is a handful of
  // mostly-`request` probes that finish in seconds. Keeping them separately addressable means
  // `--project=chromium` is still the fast inner loop, `--project=authz` is a security check you
  // can run on its own, and a failure reports which kind of thing broke instead of appearing as
  // "one of 53 tests". Anything matching /authz/ lands in the security lane.
  projects: [
    {
      // The pre-existing functional suite, unchanged.
      name: "chromium",
      testIgnore: /authz/,
      use: { ...devices["Desktop Chrome"] }
    },
    {
      // e2e/authz.spec.ts (authentication + role capability) and
      // e2e/authz-boundaries.spec.ts (tenant boundary + the collab WebSocket).
      //
      // These are the only tests in the repo that can fail on an authorization regression, and
      // they are structured so they cannot pass by accident: every negative assertion is paired
      // with a positive control proving the thing IS reachable by whoever should reach it. A
      // suite of refusals against a dead server is green, which is how the original 48 ran for
      // months against an app with AUTH_MODE=none and told nobody.
      name: "authz",
      testMatch: /authz/,
      use: { ...devices["Desktop Chrome"] }
    }
  ],
  webServer: [
    {
      // Quoted: a checkout path containing a space otherwise splits into argv.
      // The `rm` is part of the server command ON PURPOSE. Playwright starts webServer
      // BEFORE globalSetup, so resetting the database from globalSetup unlinks the file out
      // from under the already-running backend: SQLite keeps writing to the unlinked inode
      // while later connections create a fresh empty file, and the suite then runs against
      // two different databases. Doing it here makes the ordering unambiguous.
      // -wal and -shm go too; deleting only the main file leaves a WAL that SQLite replays
      // on open, resurrecting the rows the reset is meant to discard.
      command: `rm -f ${JSON.stringify(e2eDbPath)} ${JSON.stringify(e2eDbPath + "-wal")} ${JSON.stringify(e2eDbPath + "-shm")} && ${JSON.stringify(pythonBin)} -m uvicorn app.main:app --host 127.0.0.1 --port 8100`,
      cwd: "./apps/python-executor",
      url: `${apiBaseUrl}/health`,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        ...process.env,
        DATABASE_URL: `sqlite:///${e2eDbPath}`,
        // Required now that the server command actually resets the database. seed_if_empty()
        // (storage.py:319) loads the demo connectors/variables/rules ONLY when this is "1",
        // so a genuinely fresh database is empty and the suite has nothing to act on.
        // Until the reset existed, the suite was quietly depending on a database file seeded
        // by some earlier run and never cleared -- which is what made its state, and
        // therefore #125, depend on history rather than on the code under test.
        RULEMIND_SEED_DEMO: "1",
        CORS_ORIGINS: webBaseUrl,
        PYTHON_SANDBOX_TIMEOUT: "2000",
        PYTHON_SANDBOX_MEMORY: "128",
        // AUTH_MODE is deliberately UNSET, not "none". middleware.py early-returns on
        // "none", skipping the API-key check, RBAC, CSRF and tenant resolution -- so all 48
        // browser tests ran against an app with no authorization and structurally could not
        // see an authz regression (#139). Unset leaves auth ON.
        RULEMIND_DEV_API_KEY: e2eApiKey,
        // Not a weakening -- the key and RBAC checks stay on. 48 serialized tests against a
        // single tenant would otherwise sit under the limiter for no signal.
        RATE_LIMIT_ENABLED: "0"
      }
    },
    {
      command: "./node_modules/.bin/next dev -H 127.0.0.1 -p 3100",
      cwd: "./apps/web",
      url: webBaseUrl,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        ...process.env,
        HOST: "127.0.0.1",
        PORT: "3100",
        NEXT_PUBLIC_API_BASE_URL: apiBaseUrl,
        // apps/web/src/lib/store.ts reads this as its initial apiKey and api.ts sends it as
        // x-api-key. Each Playwright context starts with empty localStorage, so this default
        // wins over any persisted zustand value.
        NEXT_PUBLIC_RULEMIND_DEV_API_KEY: e2eApiKey
      }
    }
  ]
});
