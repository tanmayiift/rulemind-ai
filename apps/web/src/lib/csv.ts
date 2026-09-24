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

/** Parse a dataset FILE into rows: .csv, .json (an array), .jsonl/.ndjson, .xls/.xlsx.
 *  One implementation for every upload in the app — it lived inline in the simulation page, and
 *  the evaluations page had no file input at all (a risk team pasted its dataset into a textarea).
 *  SheetJS is imported dynamically so it stays off the main bundle. */
export async function parseDatasetFile(file: File): Promise<Record<string, unknown>[]> {
  let parsed: Record<string, unknown>[];
  if (/\.(jsonl|ndjson)$/i.test(file.name)) {
    // One JSON object per line — blank lines skipped, a bad line stops with its number.
    parsed = [];
    const lines = (await file.text()).split("\n");
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line) continue;
      try {
        const obj = JSON.parse(line);
        parsed.push(obj && typeof obj === "object" ? obj : { _value: obj });
      } catch {
        throw new Error(`Line ${i + 1} is not valid JSON.`);
      }
    }
  } else if (/\.json$/i.test(file.name)) {
    const value = JSON.parse(await file.text());
    if (!Array.isArray(value)) throw new Error("A .json dataset must be an array of objects.");
    parsed = value as Record<string, unknown>[];
  } else if (/\.csv$/i.test(file.name)) {
    parsed = parseCsv(await file.text());
  } else if (/\.xlsx?$/i.test(file.name)) {
    const XLSX = await import("xlsx");
    const wb = XLSX.read(await file.arrayBuffer(), { type: "array" });
    const sheet = wb.Sheets[wb.SheetNames[0]];
    parsed = (XLSX.utils.sheet_to_json(sheet, { defval: "" }) as Record<string, unknown>[]).map((r) => {
      const o: Record<string, unknown> = {};
      for (const k of Object.keys(r)) o[k] = coerce(r[k]);
      return o;
    });
  } else {
    throw new Error("Unsupported file — use .csv, .xls, .xlsx, .json or .jsonl.");
  }
  if (!parsed.length) throw new Error("No rows found in the file.");
  return parsed;
}

/** Column names across the first rows of a dataset, in first-seen order. */
export function datasetColumns(rows: Record<string, unknown>[], sample = 50): string[] {
  const seen = new Set<string>();
  rows.slice(0, sample).forEach((row) => Object.keys(row).forEach((k) => seen.add(k)));
  return [...seen];
}
