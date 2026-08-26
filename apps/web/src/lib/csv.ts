// One CSV parser for the whole app.
//
// This lived inline in the simulation page while the evaluations page carried a
// second, naive `line.split(",")` implementation. That one kept the quote
// characters on quoted values (so "0.85" became the string '"0.85"', which
// Number() reads as NaN) and split any quoted field containing a comma in half,
// silently discarding the remainder. On the model-evaluation screen that meant a
// standard Excel export produced wrong Gini/AUC/KS with no error shown.

/** Narrow a raw CSV cell to a boolean, number or string. */
export function coerce(v: unknown): unknown {
  if (typeof v !== "string") return v;
  const s = v.trim();
  if (s === "") return "";
  if (s === "true" || s === "TRUE") return true;
  if (s === "false" || s === "FALSE") return false;
  if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  return s;
}

/** Split RFC-4180 text into rows of raw cells. Handles quoted fields containing
 *  commas, escaped `""` quotes, and newlines inside quotes. */
export function parseCsvRows(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') { field += '"'; i++; } else inQuotes = false;
      } else field += c;
    } else if (c === '"') inQuotes = true;
    else if (c === ",") { row.push(field); field = ""; }
    else if (c === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else if (c === "\r") { /* bare CR outside quotes is a line-ending artefact */ }
    else field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  return rows.filter((r) => r.some((c) => c.trim() !== ""));
}

/** Parse CSV text into objects keyed by the header row, with values coerced.
 *  `String.trim()` also strips a UTF-8 BOM, so Excel exports key correctly. */
export function parseCsv(text: string): Record<string, unknown>[] {
  const rows = parseCsvRows(text);
  if (rows.length < 1) return [];
  const headers = rows[0].map((h) => h.trim());
  return rows.slice(1).map((r) => {
    const obj: Record<string, unknown> = {};
    headers.forEach((h, i) => { if (h) obj[h] = coerce(r[i]); });
    return obj;
  });
}

/** Accept a pasted dataset as a JSON array or as CSV. */
export function parseRows(text: string): Record<string, unknown>[] {
  const t = text.trim();
  if (!t) return [];
  if (t.startsWith("[")) return JSON.parse(t) as Record<string, unknown>[];
  return parseCsv(t);
}
