import { describe, expect, it } from "vitest";
import { coerce, datasetColumns, parseCsv, parseDatasetFile, parseRows } from "../apps/web/src/lib/csv";

describe("CSV parsing (shared by simulation + evaluations)", () => {
  it("parses a plain table and coerces types", () => {
    expect(parseCsv("a,b,c\n1,2.5,hello")).toEqual([{ a: 1, b: 2.5, c: "hello" }]);
  });

  it("keeps a quoted field containing a comma intact", () => {
    // The old naive split(',') truncated this to '"Urban' and dropped ' High"'.
    expect(parseCsv('score,segment\n0.85,"Urban, High"')).toEqual([
      { score: 0.85, segment: "Urban, High" },
    ]);
  });

  it("strips the quotes from a quoted number instead of yielding NaN", () => {
    // Excel quotes fields freely; '"0.85"' must become 0.85, not the string '"0.85"'.
    expect(parseCsv('score,label\n"0.85","1"')).toEqual([{ score: 0.85, label: 1 }]);
  });

  it("handles escaped quotes", () => {
    expect(parseCsv('note\n"she said ""hi"""')).toEqual([{ note: 'she said "hi"' }]);
  });

  it("handles a newline inside a quoted field", () => {
    expect(parseCsv('a,b\n"line1\nline2",2')).toEqual([{ a: "line1\nline2", b: 2 }]);
  });

  it("strips a UTF-8 BOM from the first header", () => {
    const rows = parseCsv("﻿customer_id,score\nC1,700");
    expect(Object.keys(rows[0])).toEqual(["customer_id", "score"]);
  });

  it("tolerates CRLF line endings", () => {
    expect(parseCsv("a,b\r\n1,2\r\n")).toEqual([{ a: 1, b: 2 }]);
  });

  it("skips blank lines", () => {
    expect(parseCsv("a\n1\n\n2\n")).toEqual([{ a: 1 }, { a: 2 }]);
  });

  it("parseRows accepts a JSON array as well as CSV", () => {
    expect(parseRows('[{"score":0.9,"label":1}]')).toEqual([{ score: 0.9, label: 1 }]);
    expect(parseRows("score,label\n0.9,1")).toEqual([{ score: 0.9, label: 1 }]);
    expect(parseRows("   ")).toEqual([]);
  });

  it("coerce maps booleans, numbers and blanks", () => {
    expect(coerce("true")).toBe(true);
    expect(coerce("FALSE")).toBe(false);
    expect(coerce("-12.5")).toBe(-12.5);
    expect(coerce("")).toBe("");
    expect(coerce("00123abc")).toBe("00123abc");
  });
});

// parseDatasetFile: the one upload parser behind the simulation and evaluation screens. The
// evaluation screen had no file input at all before it existed, so a risk team pasted its
// portfolio into a textarea.
describe("parseDatasetFile", () => {
  const file = (name: string, body: string | Uint8Array) => new File([body], name);

  it("reads a CSV with quoted commas and coerces numbers", async () => {
    const rows = await parseDatasetFile(file("p.csv", 'score,label,note\n0.8,1,"a, b"\n0.1,0,c\n'));
    expect(rows).toEqual([{ score: 0.8, label: 1, note: "a, b" }, { score: 0.1, label: 0, note: "c" }]);
  });

  it("reads a JSON array, and refuses a JSON object", async () => {
    expect(await parseDatasetFile(file("p.json", '[{"score":0.5,"label":1}]'))).toEqual([{ score: 0.5, label: 1 }]);
    await expect(parseDatasetFile(file("p.json", '{"score":0.5}'))).rejects.toThrow("array of objects");
  });

  it("reads JSONL and names the first bad line", async () => {
    expect(await parseDatasetFile(file("p.jsonl", '{"a":1}\n\n{"a":2}\n'))).toEqual([{ a: 1 }, { a: 2 }]);
    await expect(parseDatasetFile(file("p.jsonl", '{"a":1}\nnot json\n'))).rejects.toThrow("Line 2");
  });

  it("reads the first sheet of an xlsx workbook", async () => {
    // xlsx is apps/web's dependency, not the repo root's; the parser resolves it from there.
    const XLSX = await import("../apps/web/node_modules/xlsx/xlsx.mjs");
    const book = XLSX.utils.book_new();
    XLSX.utils.book_append_sheet(book, XLSX.utils.json_to_sheet([{ score: 0.7, label: 1 }]), "data");
    const bytes = XLSX.write(book, { type: "array", bookType: "xlsx" }) as ArrayBuffer;
    expect(await parseDatasetFile(file("p.xlsx", new Uint8Array(bytes)))).toEqual([{ score: 0.7, label: 1 }]);
  });

  it("refuses an unsupported extension and an empty file", async () => {
    await expect(parseDatasetFile(file("p.parquet", "x"))).rejects.toThrow("Unsupported file");
    await expect(parseDatasetFile(file("p.csv", "score,label\n"))).rejects.toThrow("No rows");
  });
});

describe("datasetColumns", () => {
  it("lists columns in first-seen order across rows", () => {
    expect(datasetColumns([{ a: 1 }, { b: 2, a: 3 }])).toEqual(["a", "b"]);
  });
});
