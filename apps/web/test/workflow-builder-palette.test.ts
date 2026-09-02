import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

// These invariants span TypeScript and Python, so they are checked by parsing
// source rather than importing. That is the point: the bugs they catch are
// exactly drift between the web builder's palette and the backend executor's
// dispatch, which no runtime import could compare.
//
// This suite would have caught all three defects it was written for:
//   - `decision_table` dispatched by the executor but missing from the palette
//   - `model` in the palette with no `needsRef`, so it could never be given a
//     ref_id and `_execute_model` raised "Unknown model: None"
//   - a `needsRef` kind with no corresponding fetch, leaving an empty dropdown

const ROOT = resolve(__dirname, "../../..");
const read = (p: string) => readFileSync(resolve(ROOT, p), "utf8");

/** Step types the Python executor actually dispatches. */
function executorStepTypes(): Set<string> {
  const src = read("apps/python-executor/app/executor.py");
  const body = src.slice(src.indexOf("async def _execute_step_body"));
  const end = body.indexOf('return None, "Unknown step type');
  expect(end, "could not locate the end of _execute_step_body").toBeGreaterThan(0);
  const types = [...body.slice(0, end).matchAll(/step_type == "([a-z_]+)"/g)].map((m) => m[1]);
  expect(types.length, "parsed no step types from the executor").toBeGreaterThan(5);
  return new Set(types);
}

/** `{ type, needsRef }` for every palette entry in a builder page. */
function palette(file: string): Array<{ type: string; needsRef?: string }> {
  const src = read(file);
  const start = src.indexOf("const PALETTE");
  expect(start, `no PALETTE in ${file}`).toBeGreaterThan(0);
  const block = src.slice(start, src.indexOf("];", start));
  return [...block.matchAll(/\{\s*type:\s*"([a-z_]+)"[^}]*?\}/g)].map((m) => {
    const needs = /needsRef:\s*"([a-z_]+)"/.exec(m[0]);
    return { type: m[1], needsRef: needs?.[1] };
  });
}

/** The keys the `refs` state is initialised with. */
function refKeys(file: string): Set<string> {
  const src = read(file);
  const init = /useState<RefMap>\(\{([^}]*)\}\)|setRefs\(\{([^}]*)\}\)/.exec(src);
  const chunk = init?.[1] ?? init?.[2] ?? "";
  return new Set([...chunk.matchAll(/([a-z_]+)\s*:/g)].map((m) => m[1]));
}

const BUILDERS = [
  "apps/web/app/workflow-builder/page.tsx",
  "apps/web/app/studio/page.tsx",
];

describe("workflow builders stay in step with the executor", () => {
  const dispatched = executorStepTypes();

  it("the executor still dispatches the types we rely on", () => {
    for (const t of ["rule", "scorecard", "decision_table", "model", "transform"]) {
      expect(dispatched, `executor no longer dispatches ${t}`).toContain(t);
    }
  });

  for (const file of BUILDERS) {
    const name = file.split("/").slice(-2)[0];

    it(`${name}: every palette step type is dispatched by the executor`, () => {
      const offered = palette(file).map((p) => p.type);
      expect(offered.length).toBeGreaterThan(5);
      const undispatched = offered.filter((t) => !dispatched.has(t));
      // The worse direction: a step a user can add that the backend cannot run.
      expect(undispatched, `${name} offers steps the executor cannot run`).toEqual([]);
    });

    it(`${name}: every needsRef kind is actually fetched`, () => {
      const keys = refKeys(file);
      const missing = palette(file)
        .map((p) => p.needsRef)
        .filter((k): k is string => Boolean(k))
        .filter((k) => !keys.has(k));
      // A needsRef with no fetch renders an empty dropdown, so the step can
      // never be configured.
      expect(missing, `${name} references entity kinds it never loads`).toEqual([]);
    });
  }

  it("workflow-builder offers decision_table", () => {
    const types = palette(BUILDERS[0]).map((p) => p.type);
    expect(types, "decision_table is dispatched by the executor but unreachable in the UI")
      .toContain("decision_table");
  });

  it("every ref-taking step can be given a ref", () => {
    // `model` shipped in the palette with no needsRef, so the Inspector never
    // rendered a selector and _execute_model raised "Unknown model: None".
    for (const file of BUILDERS) {
      for (const entry of palette(file)) {
        if (["rule", "scorecard", "decision_table", "model", "connector"].includes(entry.type)) {
          expect(entry.needsRef, `${file}: ${entry.type} has no needsRef, so it can never be configured`)
            .toBeTruthy();
        }
      }
    }
  });
});
