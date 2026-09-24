// ESLint for the dashboard (plan 6.8). There was none: `pnpm lint` ran `tsc --noEmit`, which checks
// types and nothing else -- not the rules of hooks, not accessibility, not Next's own footguns.
//
// next/core-web-vitals brings react, react-hooks and jsx-a11y's recommended rules; next/typescript
// adds @typescript-eslint. Errors fail the build. Warnings are held to a ratchet in package.json
// (`--max-warnings`), so the count can only go down.
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });

const config = [
  { ignores: [".next/**", ".next-e2e/**", "node_modules/**", "out/**", "next-env.d.ts"] },
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  {
    rules: {
      // A leading underscore marks a binding kept on purpose -- the rest-destructure idiom for
      // dropping keys (`const { loading: _loading, ...rest } = x`) has no other spelling.
      "@typescript-eslint/no-unused-vars": ["warn", {
        argsIgnorePattern: "^_", varsIgnorePattern: "^_", destructuredArrayIgnorePattern: "^_",
      }],
    },
  },
];

export default config;
