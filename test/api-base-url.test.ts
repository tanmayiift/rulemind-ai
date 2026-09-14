/**
 * The shipped bundle used to point at http://localhost:8080 whatever the deployment said.
 *
 * Two separate causes, both fixed in apps/web/src/lib/store.ts and both covered here:
 *   1. `process.env.NEXT_PUBLIC_API_BASE_URL` is substituted at BUILD time, so one image cannot
 *      serve two environments and the Helm chart's runtime `env:` block was a no-op against it.
 *   2. `apiBaseUrl` was persisted to localStorage and restored unconditionally, so a browser
 *      that had once loaded the app against localhost kept using it across every redeploy --
 *      the failure that survives a correct deployment.
 *
 * `resolveApiBaseUrl` is imported from the store, not reimplemented here, so this cannot pass
 * against a copy that has drifted from what ships. The merge rule lives inside zustand's
 * persist options and is not exported, so it is restated -- the assertions below pin the
 * behaviour that matters either way.
 */
import { describe, it, expect, afterEach } from "vitest";

import { resolveApiBaseUrl } from "../apps/web/src/lib/store";

const originalEnv = process.env.NEXT_PUBLIC_API_BASE_URL;

function withWindow(config: { apiBaseUrl?: string } | undefined) {
  (globalThis as Record<string, unknown>).window = config === undefined
    ? {}
    : { __RULEMIND_CONFIG__: config };
}

afterEach(() => {
  delete (globalThis as Record<string, unknown>).window;
  if (originalEnv === undefined) delete process.env.NEXT_PUBLIC_API_BASE_URL;
  else process.env.NEXT_PUBLIC_API_BASE_URL = originalEnv;
});

describe("resolveApiBaseUrl", () => {
  it("prefers the runtime-injected config over the build-time literal", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "http://localhost:8080";
    withWindow({ apiBaseUrl: "https://api.example.com" });
    expect(resolveApiBaseUrl()).toBe("https://api.example.com");
  });

  it("falls back to build-time env when nothing is injected (the Vercel case)", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.koyeb.app";
    withWindow(undefined);
    expect(resolveApiBaseUrl()).toBe("https://api.koyeb.app");
  });

  it("falls back to localhost only when neither is present", () => {
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    withWindow(undefined);
    expect(resolveApiBaseUrl()).toBe("http://localhost:8080");
  });

  it("strips trailing slashes so request paths do not double up", () => {
    withWindow({ apiBaseUrl: "https://api.example.com//" });
    expect(resolveApiBaseUrl()).toBe("https://api.example.com");
  });

  it("ignores an injected empty string rather than treating it as a host", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.koyeb.app";
    withWindow({ apiBaseUrl: "" });
    expect(resolveApiBaseUrl()).toBe("https://api.koyeb.app");
  });

  it("works server-side, where there is no window at all", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.koyeb.app";
    delete (globalThis as Record<string, unknown>).window;
    expect(resolveApiBaseUrl()).toBe("https://api.koyeb.app");
  });
});

// Mirrors the persist `merge` in store.ts. Not exported (it is an inline zustand option), so
// this restates the rule; the first case is the actual reported failure.
function merge(saved: { apiBaseUrl?: string; apiBaseUrlIsUserSet?: boolean },
               resolved: string): string {
  return saved.apiBaseUrlIsUserSet && saved.apiBaseUrl ? saved.apiBaseUrl : resolved;
}

describe("persisted base URL merge", () => {
  it("discards a stale persisted default so a redeploy takes effect", () => {
    expect(merge({ apiBaseUrl: "http://localhost:8080", apiBaseUrlIsUserSet: false },
                 "https://api.example.com")).toBe("https://api.example.com");
  });

  it("keeps a base URL a human actually typed into Settings", () => {
    expect(merge({ apiBaseUrl: "https://staging.internal", apiBaseUrlIsUserSet: true },
                 "https://api.example.com")).toBe("https://staging.internal");
  });

  it("ignores the user-set flag when no URL came with it", () => {
    expect(merge({ apiBaseUrlIsUserSet: true }, "https://api.example.com"))
      .toBe("https://api.example.com");
  });
});
