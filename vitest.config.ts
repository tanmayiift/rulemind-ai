import { defineConfig } from "vitest/config";
import { resolve } from "node:path";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
    // JUnit alongside the default console output, so a run is both readable live and parseable
    // afterwards. scripts/run_all_tests.sh collects these into TEST_RESULTS.md; before this the
    // only machine-readable output anywhere in the repo was Gradle's, and nothing consumed it.
    reporters: process.env.CI || process.env.RULEMIND_TEST_REPORT
      ? ["default", "junit"]
      : ["default"],
    outputFile: { junit: "test-results/vitest-junit.xml" }
  },
  resolve: {
    alias: {
      "@rulemind/shared": resolve(__dirname, "packages/shared/src/index.ts"),
      "@rulemind/rule-engine": resolve(__dirname, "packages/rule-engine/src/index.ts"),
      "@rulemind/widget": resolve(__dirname, "packages/widget/src/index.ts"),
      "@rulemind/sdk-js": resolve(__dirname, "packages/sdk-js/src/index.ts")
    }
  }
});
