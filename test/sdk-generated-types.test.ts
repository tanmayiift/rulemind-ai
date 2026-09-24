import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * The JS SDK's response types are generated from the API's contract (plan 5.1). A generated file
 * that nobody regenerates is a hand-written copy with extra steps, so this regenerates it from the
 * committed spec and requires the committed file to match byte for byte.
 */
const root = resolve(__dirname, "..");
const sdk = join(root, "packages/sdk-js");

describe("sdk-js generated API types", () => {
  it("match the committed OpenAPI snapshot", () => {
    const out = mkdtempSync(join(tmpdir(), "rm-openapi-"));
    try {
      const target = join(out, "openapi.ts");
      execFileSync(join(sdk, "node_modules/.bin/openapi-typescript"),
        [join(root, "apps/python-executor/openapi.json"), "-o", target], { stdio: "pipe" });
      const committed = readFileSync(join(sdk, "src/generated/openapi.ts"), "utf8");
      expect(committed === readFileSync(target, "utf8"),
        "stale: run `pnpm --filter @rulemind/sdk-js gen:types`").toBe(true);
    } finally {
      rmSync(out, { recursive: true, force: true });
    }
  }, 60_000);

  it("carry the decide contract, including the rule results both server paths now agree on", () => {
    const generated = readFileSync(join(sdk, "src/generated/openapi.ts"), "utf8");
    for (const schema of ["DecideResponse", "RuleResult", "SdkDecideResponse", "ErrorResponse"]) {
      expect(generated).toContain(`        ${schema}: {`);
    }
  });
});
