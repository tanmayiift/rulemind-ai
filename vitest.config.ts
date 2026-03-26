import { defineConfig } from "vitest/config";
import { resolve } from "node:path";

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"]
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
