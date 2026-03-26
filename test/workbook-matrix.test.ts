import { describe, expect, it } from "vitest";
import { runWorkbookAutomation } from "../qa/workbook-runner";

describe("workbook automation", () => {
  it("passes every automated operator and topology row from the attached workbook", () => {
    const result = runWorkbookAutomation();
    const failures = [...result.operatorResults, ...result.topologyResults].filter((entry) => !entry.pass);

    expect(failures, JSON.stringify(failures, null, 2)).toEqual([]);
    expect(result.summary.operators.total).toBe(53);
    expect(result.summary.topology.total).toBe(24);
  });
});
