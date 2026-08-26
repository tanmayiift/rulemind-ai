import { describe, expect, it } from "vitest";
import { coerce, parseCsv, parseRows } from "../apps/web/src/lib/csv";

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
