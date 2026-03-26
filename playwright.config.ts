import { defineConfig, devices } from "@playwright/test";

const apiBaseUrl = "http://127.0.0.1:8100";
const webBaseUrl = "http://127.0.0.1:3100";

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
    trace: "on-first-retry",
    screenshot: "only-on-failure",
    video: "retain-on-failure"
  },
  projects: [
    {
      name: "chromium",
      use: { ...devices["Desktop Chrome"] }
    }
  ],
  webServer: [
    {
      command: "python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8100",
      cwd: "./apps/python-executor",
      url: `${apiBaseUrl}/health`,
      reuseExistingServer: false,
      stdout: "pipe",
      stderr: "pipe",
      env: {
        ...process.env,
        DATABASE_URL: "sqlite:///.runtime/playwright-v3.db",
        CORS_ORIGINS: webBaseUrl,
        PYTHON_SANDBOX_TIMEOUT: "2000",
        PYTHON_SANDBOX_MEMORY: "128"
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
        AUTH_MODE: "none"
      }
    }
  ]
});
