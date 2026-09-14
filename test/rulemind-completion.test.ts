import { describe, expect, it } from "vitest";

import {
  rulemindCompletions,
  type RuleMindCompletionScope,
} from "../apps/web/src/v3/editor/rulemind-completion";

// The completion source is the half of parity row P13 that generic Python tooling does not
// give you: the fields it offers come from the connector selected on the page and the
// variables that page has loaded, so the assertions below are about *which* completions a
// cursor position deserves, not about CodeMirror. Driving a real editor here would test the
// library; this tests the decision.

const BANK: Record<string, unknown> = {
  accountNumber: "50100XXXX",
  summary: {
    avgBalance: 45200.5,
    salaryCredits: 6,
    lastCredit: { amount: 82000, date: "2026-08-01" },
  },
  transactions: [{ amount: -1200, narration: "UPI" }],
  isActive: true,
};

const BUREAU: Record<string, unknown> = {
  scores: [{ scoreName: "CIBILTUSC3", score: 762 }],
  enquiries: [],
};

const bankScope = (): RuleMindCompletionScope => ({
  samplePayload: BANK,
  variables: [
    { id: "income_multiplier", name: "Income Multiplier", category: "Banking", description: "Avg balance over 1000" },
    { id: "bureau_score", name: "Bureau Score", category: "Bureau", description: null },
  ],
  sourceId: "bank",
  sourceIds: ["bureau", "bank", "gst", "custom"],
});

/** The cursor sits at the end of `line`; document offsets are line-relative plus `base`. */
function at(line: string, scope: RuleMindCompletionScope, base = 0, explicit = false) {
  return rulemindCompletions({ textBefore: line, pos: base + line.length, explicit }, scope);
}

const labels = (result: ReturnType<typeof rulemindCompletions>) => (result?.options ?? []).map((option) => option.label);

describe("rulemindCompletions — payload fields from the selected source", () => {
  it("offers the sample payload's top-level keys after `payload.`", () => {
    const result = at("    return payload.", bankScope());
    expect(labels(result)).toEqual(["accountNumber", "summary", "transactions", "isActive"]);
  });

  it("rewrites a dot completion into a subscript, because the sandbox passes a plain dict", () => {
    const line = "    return payload.";
    const result = at(line, bankScope());
    const summary = result?.options.find((option) => option.label === "summary");
    expect(summary?.insert).toBe('["summary"]');
    // Applying from the dot, not from after it, is what turns `payload.` into `payload["summary"]`.
    expect(summary?.insertFrom).toBe(line.length - 1);
    expect(result?.from).toBe(line.length);
  });

  it("describes each field with its Python type and a value preview", () => {
    const result = at("    return payload.", bankScope());
    const byLabel = Object.fromEntries((result?.options ?? []).map((option) => [option.label, option]));
    expect(byLabel.summary.detail).toBe("dict{3}");
    expect(byLabel.transactions.detail).toBe("list[1]");
    expect(byLabel.isActive.detail).toBe("bool · true");
    expect(byLabel.accountNumber.detail).toBe('str · "50100XXXX"');
    expect(byLabel.summary.info).toBe('payload["summary"]');

    const nested = at("    return payload.summary.", bankScope());
    const byNested = Object.fromEntries((nested?.options ?? []).map((option) => [option.label, option]));
    expect(byNested.avgBalance.detail).toBe("float · 45200.5");
    expect(byNested.salaryCredits.detail).toBe("int · 6");
    expect(byNested.lastCredit.info).toBe('payload["summary"]["lastCredit"]');
  });

  it("walks nested keys through both `.key` and `[\"key\"]` access", () => {
    expect(labels(at("    return payload.summary.", bankScope()))).toEqual([
      "avgBalance",
      "salaryCredits",
      "lastCredit",
    ]);
    expect(labels(at('    return payload["summary"].', bankScope()))).toEqual([
      "avgBalance",
      "salaryCredits",
      "lastCredit",
    ]);
    expect(labels(at('    return payload["summary"]["lastCredit"].', bankScope()))).toEqual(["amount", "date"]);
  });

  it("completes a partially typed subscript key without adding a second subscript", () => {
    const line = '    return payload["sum';
    const result = at(line, bankScope());
    const summary = result?.options.find((option) => option.label === "summary");
    expect(summary?.insert).toBe("summary");
    expect(result?.from).toBe(line.length - "sum".length);
  });

  it("follows the selected source: the same cursor offers different fields", () => {
    const scope = bankScope();
    expect(labels(at("    return payload.", scope))).toContain("accountNumber");

    const switched: RuleMindCompletionScope = { ...scope, samplePayload: BUREAU, sourceId: "bureau" };
    expect(labels(at("    return payload.", switched))).toEqual(["scores", "enquiries"]);
    expect(labels(at("    return payload.", switched))).not.toContain("accountNumber");
  });
});

describe("rulemindCompletions — variables, parameters and the decorator", () => {
  it("offers the other loaded variables after `variables.`", () => {
    const result = at("    base = variables.", bankScope());
    expect(labels(result)).toEqual(["income_multiplier", "bureau_score"]);
    const first = result?.options[0];
    expect(first?.insert).toBe('["income_multiplier"]');
    expect(first?.detail).toBe("variable · Banking");
    expect(first?.info).toBe("Income Multiplier — Avg balance over 1000");
  });

  it("offers the three function parameters on a bare identifier", () => {
    expect(labels(at("    return pay", bankScope()))).toEqual(["payload", "variables", "apis"]);
    expect(at("    return pay", bankScope())?.options[0].detail).toBe("parameter · dict");
  });

  it("offers the decorator after `@`, bound to the source the page has selected", () => {
    const result = at("@", bankScope());
    expect(labels(result)).toEqual(["variable"]);
    expect(result?.options[0].insert).toBe('variable(source="bank")');

    const gst: RuleMindCompletionScope = { ...bankScope(), sourceId: "gst" };
    expect(at("@", gst)?.options[0].insert).toBe('variable(source="gst")');
  });

  it("offers connector ids inside the decorator's source string", () => {
    const line = '@variable(source="b';
    const result = at(line, bankScope());
    expect(labels(result)).toEqual(["bureau", "bank", "gst", "custom"]);
    expect(result?.from).toBe(line.length - 1);
  });

  it("offers the one key the runtime actually puts in `apis`", () => {
    expect(labels(at("    handle = apis.", bankScope()))).toEqual(["connectors"]);
  });
});

describe("rulemindCompletions — where it must stay silent", () => {
  it("does not fire inside a comment", () => {
    expect(at("    # check payload.", bankScope())).toBeNull();
    expect(at("    # pay", bankScope())).toBeNull();
  });

  it("does not fire inside an ordinary string literal", () => {
    expect(at('    label = "monthly pay', bankScope())).toBeNull();
  });

  it("does not fire on a dot after a scalar or a list", () => {
    expect(at("    return payload.summary.avgBalance.", bankScope())).toBeNull();
    expect(at("    return payload.transactions.", bankScope())).toBeNull();
  });

  it("does not fire on a dot after an unknown object", () => {
    expect(at("    return account.", bankScope())).toBeNull();
    expect(at("    return mypayload.", bankScope())).toBeNull();
  });

  it("does not fire below the top level of `variables` or `apis`, whose values are opaque", () => {
    expect(at("    return variables.income_multiplier.", bankScope())).toBeNull();
    expect(at("    return apis.connectors.", bankScope())).toBeNull();
  });

  it("does not fire on an empty word unless the completion key was pressed", () => {
    expect(at("    ", bankScope())).toBeNull();
    expect(labels(at("    ", bankScope(), 0, true))).toEqual(["payload", "variables", "apis"]);
  });

  it("offers nothing for a source with no sample payload", () => {
    expect(at("    return payload.", { ...bankScope(), samplePayload: null })).toBeNull();
  });
});

describe("rulemindCompletions — document offsets", () => {
  it("returns absolute document offsets, not line-relative ones", () => {
    const line = "    return payload.sum";
    const base = 137;
    const result = at(line, bankScope(), base);
    expect(result?.from).toBe(base + line.length - "sum".length);
    expect(result?.options[0].insertFrom).toBe(base + line.indexOf("payload.") + "payload".length);
  });
});
