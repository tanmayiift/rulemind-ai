import { describe, expect, it } from "vitest";

import { snapshotChanges } from "../src/v3/pages/PolicyLifecycle";

describe("history diff", () => {
  it("lists only the fields that changed, ignoring bookkeeping", () => {
    const changes = snapshotChanges(
      { name: "A", steps: [1], version: 1, updated_at: "x" },
      { name: "B", steps: [1], version: 2, updated_at: "y", status: "uat" },
    );
    expect(changes.map((c) => c.key)).toEqual(["name", "status"]);
    expect(changes[0]).toEqual({ key: "name", before: "A", after: "B" });
  });

  it("compares nested values by content, not identity", () => {
    expect(snapshotChanges({ steps: [{ id: "s" }] }, { steps: [{ id: "s" }] })).toEqual([]);
  });
});
