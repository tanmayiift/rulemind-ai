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

/** A browser served from `origin`, with no injected config and no build-time value. */
function servedFrom(origin: string) {
  const url = new URL(origin);
  (globalThis as Record<string, unknown>).window = {
    location: { origin: url.origin, hostname: url.hostname },
  };
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

describe("same-origin resolution", () => {
  /**
   * The third cause, and the one that made the product not self-hostable at all: with no
   * injected config and no build-time value, the bundle fell through to http://localhost:8080 --
   * the adopter's own laptop. NEXT_PUBLIC_* is substituted at BUILD time, so the Helm chart's
   * runtime env: block could not reach it and the only fix was rebuilding the image with
   * --build-arg, which is not something a chart can ask of the person installing it.
   *
   * The Ingress already routes /api to the API Service, so a browser served from the cluster
   * host should simply call its own origin.
   */
  it("uses the serving origin when the app is not on a developer machine", () => {
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    servedFrom("https://rulemind.acme.internal");
    expect(resolveApiBaseUrl()).toBe("https://rulemind.acme.internal");
  });

  it("keeps the port form on localhost, where the API really is a separate origin", () => {
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    for (const host of ["http://localhost:3000", "http://127.0.0.1:3000"]) {
      servedFrom(host);
      expect(resolveApiBaseUrl()).toBe("http://localhost:8080");
    }
  });

  it("still lets an explicit build-time value win, for a split-origin deployment", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.acme.internal";
    servedFrom("https://rulemind.acme.internal");
    expect(resolveApiBaseUrl()).toBe("https://api.acme.internal");
  });

  it("still lets runtime injection win over everything", () => {
    process.env.NEXT_PUBLIC_API_BASE_URL = "https://api.acme.internal";
    const url = new URL("https://rulemind.acme.internal");
    (globalThis as Record<string, unknown>).window = {
      location: { origin: url.origin, hostname: url.hostname },
      __RULEMIND_CONFIG__: { apiBaseUrl: "https://injected.acme.internal/" },
    };
    expect(resolveApiBaseUrl()).toBe("https://injected.acme.internal");
  });

  it("falls back to localhost on the server, where there is no window", () => {
    delete process.env.NEXT_PUBLIC_API_BASE_URL;
    delete (globalThis as Record<string, unknown>).window;
    expect(resolveApiBaseUrl()).toBe("http://localhost:8080");
  });
});
