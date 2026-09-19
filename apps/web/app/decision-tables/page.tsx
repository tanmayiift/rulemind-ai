"use client";

import * as React from "react";
import { Plus, Trash2, Play, GripVertical, ArrowUp, ArrowDown, Table2, AlertTriangle, Upload } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type InputCol = { id: string; variable_id: string; name: string; field_type: string };
type OutputCol = { id: string; name: string; type: string };
type Cell = { operator: string; value?: string; value2?: string };
type Row = { id: string; cells: Record<string, Cell>; outputs: Record<string, string> };
type Table = {
  id: string; name: string; description?: string; hit_policy: string;
  inputs: InputCol[]; outputs: OutputCol[]; rows: Row[];
  default_row?: { outputs: Record<string, string> } | null;
  version?: number;
};
// ---------------------------------------------------------------- spreadsheet import (P07)
// The workbook is parsed HERE and never reaches the API. SheetJS is already a dependency (the
// simulation page uses it the same way); the deployed API runs on 512MB with a resident ML
// stack; and OOXML is a zip of XML, a decompression-bomb and XXE surface that a list of JSON
// rows is not. `app/decision_table_import.py` carries the same reasoning from the other side.
type ImportRole = "input" | "output" | "ignore";
type ImportColumn = { header: string; role: ImportRole; variable_id?: string; field_type?: string; type?: string };
type ImportProblem = { row?: number; header?: string; column?: number; message: string };

/** A first guess at each column's role, visible and changeable in the mapping table. */
function guessRole(header: string, index: number, total: number): ImportRole {
  // Spreadsheets put the answer on the right. Worth guessing, because the alternative is mapping
  // twelve columns by hand before finding out whether the sheet imports at all.
  if (/price|amount|rate|limit|decision|outcome|result|action|premium|fee/i.test(header)) return "output";
  return index >= total - 1 ? "output" : "input";
}

type Diagnostic = { type: string; severity: string; description: string; rows?: string[]; shadowedBy?: string };
type Analysis = { diagnostics: Diagnostic[]; ok: boolean; hasConflicts: boolean; hasGaps: boolean; hasInvalidValues: boolean; hasUnreachableRows: boolean; hasUnanalyzableRows?: boolean; rowCount: number; hitPolicy: string };
type Variable = { id: string; name: string; field_type?: string };

const OPERATORS = ["any", "==", "!=", ">", ">=", "<", "<=", "between", "in", "not_in", "regex", "exists", "!exists"];
const HIT_POLICIES = [
  { v: "first", label: "First match" },
  { v: "priority", label: "Priority" },
  { v: "unique", label: "Unique (no overlap)" },
  { v: "collect", label: "Collect all" },
];
const OUTCOMES = ["approve", "review", "reject"];
const uid = (p: string) => `${p}_${Math.random().toString(36).slice(2, 8)}`;

function sevTone(sev: string): { bg: string; fg: string } {
  if (sev === "error") return { bg: "var(--rm-danger-bg)", fg: "var(--rm-danger)" };
  if (sev === "warning") return { bg: "var(--rm-warning-bg)", fg: "var(--rm-warning)" };
  return { bg: "var(--rm-accent-bg)", fg: "var(--rm-accent)" };
}

function emptyTable(): Table {
  const inScore = uid("in");
  const outDecision = uid("out");
  return {
    id: "", name: "", hit_policy: "first",
    inputs: [{ id: inScore, variable_id: "", name: "Score", field_type: "number" }],
    outputs: [{ id: outDecision, name: "Decision", type: "outcome" }],
    rows: [{ id: uid("r"), cells: { [inScore]: { operator: ">=", value: "750" } }, outputs: { [outDecision]: "approve" } }],
    default_row: null,
  };
}

export default function DecisionTablesPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [tables, setTables] = React.useState<Table[]>([]);
  const [variables, setVariables] = React.useState<Variable[]>([]);
  const [draft, setDraft] = React.useState<Table>(emptyTable());
  const [analysis, setAnalysis] = React.useState<Analysis | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<string | null>(null);
  const [testValues, setTestValues] = React.useState("{}");
  const [testResult, setTestResult] = React.useState<Record<string, unknown> | null>(null);
  const dragRow = React.useRef<number | null>(null);
  const [importColumns, setImportColumns] = React.useState<ImportColumn[] | null>(null);
  const [importRows, setImportRows] = React.useState<Record<string, unknown>[]>([]);
  const [importName, setImportName] = React.useState("");
  const [importProblems, setImportProblems] = React.useState<ImportProblem[]>([]);
  const [importing, setImporting] = React.useState(false);
  const fileInput = React.useRef<HTMLInputElement | null>(null);

  const load = React.useCallback(async () => {
    try {
      const [t, v] = await Promise.all([
        apiJson<Table[]>(apiBaseUrl, "/api/v1/decision-tables", {}, apiKey),
        apiJson<Variable[]>(apiBaseUrl, "/api/v1/variables", {}, apiKey).catch(() => []),
      ]);
      setTables(t); setVariables(v); setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load decision tables.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void load(); }, [load]);

  // Live optimiser — re-analyze the draft (debounced) on every edit.
  React.useEffect(() => {
    const handle = setTimeout(async () => {
      try {
        const a = await apiJson<Analysis>(apiBaseUrl, "/api/v1/decision-tables/analyze", {
          method: "POST",
          body: JSON.stringify({ hit_policy: draft.hit_policy, inputs: draft.inputs, outputs: draft.outputs, rows: draft.rows, default_row: draft.default_row }),
        }, apiKey);
        setAnalysis(a);
      } catch { /* transient — keep last analysis */ }
    }, 350);
    return () => clearTimeout(handle);
  }, [draft, apiBaseUrl, apiKey]);

  const patch = (p: Partial<Table>) => setDraft((d) => ({ ...d, ...p }));

  const addInput = () => { const id = uid("in"); patch({ inputs: [...draft.inputs, { id, variable_id: "", name: `Input ${draft.inputs.length + 1}`, field_type: "number" }] }); };
  const removeInput = (id: string) => patch({
    inputs: draft.inputs.filter((c) => c.id !== id),
    rows: draft.rows.map((r) => { const cells = { ...r.cells }; delete cells[id]; return { ...r, cells }; }),
  });
  const setInput = (id: string, p: Partial<InputCol>) => patch({ inputs: draft.inputs.map((c) => (c.id === id ? { ...c, ...p } : c)) });

  const addOutput = () => { const id = uid("out"); patch({ outputs: [...draft.outputs, { id, name: `Output ${draft.outputs.length + 1}`, type: "string" }] }); };
  const removeOutput = (id: string) => patch({
    outputs: draft.outputs.filter((c) => c.id !== id),
    rows: draft.rows.map((r) => { const outputs = { ...r.outputs }; delete outputs[id]; return { ...r, outputs }; }),
  });
  const setOutput = (id: string, p: Partial<OutputCol>) => patch({ outputs: draft.outputs.map((c) => (c.id === id ? { ...c, ...p } : c)) });

  const addRow = () => patch({ rows: [...draft.rows, { id: uid("r"), cells: {}, outputs: {} }] });
  const removeRow = (id: string) => patch({ rows: draft.rows.filter((r) => r.id !== id) });
  const setCell = (rowId: string, inputId: string, p: Partial<Cell>) => patch({
    rows: draft.rows.map((r) => {
      if (r.id !== rowId) return r;
      const base: Cell = r.cells[inputId] ?? { operator: "==" };
      return { ...r, cells: { ...r.cells, [inputId]: { ...base, ...p } } };
    }),
  });
  const setRowOutput = (rowId: string, outId: string, value: string) => patch({
    rows: draft.rows.map((r) => (r.id === rowId ? { ...r, outputs: { ...r.outputs, [outId]: value } } : r)),
  });
  const moveRow = (from: number, to: number) => {
    if (to < 0 || to >= draft.rows.length) return;
    const rows = [...draft.rows];
    const [m] = rows.splice(from, 1);
    rows.splice(to, 0, m);
    patch({ rows });
  };

  const rowSeverity = (rowId: string): string | null => {
    if (!analysis) return null;
    let worst: string | null = null;
    for (const d of analysis.diagnostics) {
      if ((d.rows || []).includes(rowId)) {
        if (d.severity === "error") return "error";
        if (d.severity === "warning") worst = "warning";
        else if (!worst) worst = "info";
      }
    }
    return worst;
  };

  const save = async () => {
    setError(null); setStatus(null);
    if (!draft.name.trim()) { setError("Give the table a name."); return; }
    try {
      const body = JSON.stringify({
        name: draft.name, description: draft.description, hit_policy: draft.hit_policy,
        inputs: draft.inputs, outputs: draft.outputs, rows: draft.rows, default_row: draft.default_row,
      });
      const saved = draft.id
        ? await apiJson<Table>(apiBaseUrl, `/api/v1/decision-tables/${draft.id}`, { method: "PUT", body }, apiKey)
        : await apiJson<Table>(apiBaseUrl, "/api/v1/decision-tables", { method: "POST", body }, apiKey);
      setStatus(`Saved "${saved.name}" (v${saved.version ?? 1}).`);
      setDraft(saved);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    }
  };

  const del = async (id: string) => {
    try {
      await apiJson(apiBaseUrl, `/api/v1/decision-tables/${id}`, { method: "DELETE" }, apiKey);
      if (draft.id === id) setDraft(emptyTable());
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Delete failed."); }
  };

  const runTest = async () => {
    setError(null); setTestResult(null);
    if (!draft.id) { setError("Save the table before testing."); return; }
    try {
      const variable_values = JSON.parse(testValues || "{}");
      const res = await apiJson<Record<string, unknown>>(apiBaseUrl, `/api/v1/decision-tables/${draft.id}/evaluate`, {
        method: "POST", body: JSON.stringify({ variable_values }),
      }, apiKey);
      setTestResult(res);
    } catch (e) { setError(e instanceof Error ? e.message : "Invalid JSON or evaluation failed."); }
  };

  const cellNeedsValue = (op: string) => !["any", "exists", "!exists"].includes(op);

  const readSheet = React.useCallback(async (file: File) => {
    setImportProblems([]); setError(null); setStatus(null);
    try {
      let rows: Record<string, unknown>[] = [];
      if (/\.csv$/i.test(file.name)) {
        // SheetJS reads CSV too, so one code path handles both rather than a hand-rolled parser
        // that would get quoted commas wrong.
        const XLSX = await import("xlsx");
        const wb = XLSX.read(await file.text(), { type: "string" });
        rows = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { defval: "" });
      } else if (/\.xlsx?$/i.test(file.name)) {
        const XLSX = await import("xlsx"); // dynamic — keeps SheetJS off the main bundle
        const wb = XLSX.read(await file.arrayBuffer(), { type: "array" });
        rows = XLSX.utils.sheet_to_json(wb.Sheets[wb.SheetNames[0]], { defval: "" });
      } else {
        throw new Error("Unsupported file — use .csv, .xls or .xlsx.");
      }
      if (rows.length === 0) throw new Error("That sheet has a header but no data rows.");

      const headers = Object.keys(rows[0]);
      setImportColumns(headers.map((header, index) => {
        const role = guessRole(header, index, headers.length);
        const match = variables.find((v) => v.id === header || v.name === header
          || v.id === header.toLowerCase().replace(/[^a-z0-9]+/g, "_"));
        return role === "input"
          // field_type only when the variable declares one -- otherwise the backend reads it
          // off the column's own values, which beats defaulting a numeric column to "string".
          ? { header, role, variable_id: match?.id || "", field_type: match?.field_type || undefined }
          // No `type` for a value column: the backend infers number/string/boolean from what
          // is actually in it. Sending a literal "value" (an earlier version did) is a type the
          // authoring editor has no <option> for, so it renders the first one and a price column
          // reads as "outcome" on screen.
          : { header, role, type: /decision|outcome|action/i.test(header) ? "outcome" : undefined };
      }));
      setImportRows(rows);
      setImportName(file.name.replace(/\.[^.]+$/, ""));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read that file.");
      setImportColumns(null); setImportRows([]);
    }
  }, [variables]);

  const cancelImport = React.useCallback(() => {
    setImportColumns(null); setImportRows([]); setImportProblems([]); setImportName("");
    if (fileInput.current) fileInput.current.value = "";
  }, []);

  const runImport = React.useCallback(async () => {
    if (!importColumns) return;
    setImporting(true); setImportProblems([]); setError(null);
    try {
      const created = await apiJson<Table & { analysis?: Analysis }>(
        apiBaseUrl, "/api/v1/decision-tables/import",
        { method: "POST", body: JSON.stringify({
            name: importName || "Imported table",
            columns: importColumns,
            rows: importRows,
            hit_policy: draft.hit_policy,
          }) }, apiKey);
      cancelImport();
      await load();
      setDraft(created);
      setStatus(`Imported ${created.rows.length} rows. Check the optimiser panel for gaps.`);
    } catch (e) {
      // The API answers 422 with EVERY problem it found, each naming its row and column, so the
      // author fixes the sheet once instead of round-tripping per cell. `apiJson` throws with the
      // raw response TEXT as the message (not a parsed body), so the list has to be recovered
      // from it -- otherwise the panel renders a wall of JSON and the per-cell detail is wasted.
      let problems: ImportProblem[] = [];
      try {
        problems = JSON.parse(e instanceof Error ? e.message : "")?.detail?.problems ?? [];
      } catch {
        problems = [];
      }
      if (problems.length) setImportProblems(problems);
      else setError(e instanceof Error ? e.message : "The import failed.");
    } finally {
      setImporting(false);
    }
  }, [apiBaseUrl, apiKey, cancelImport, draft.hit_policy, importColumns, importName, importRows, load]);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="Decision Tables"
        subtitle="Grid-authored decision logic with a live optimiser."
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <input
              ref={fileInput}
              type="file"
              accept=".csv,.xls,.xlsx"
              style={{ display: "none" }}
              data-testid="dt-import-file"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) void readSheet(f); }}
            />
            <Button variant="secondary" onClick={() => fileInput.current?.click()} data-testid="dt-import-open">
              <Upload size={15} /> Import spreadsheet
            </Button>
            <Button onClick={() => { setDraft(emptyTable()); setTestResult(null); setStatus(null); }}><Plus size={15} /> New table</Button>
          </div>
        }
      />

      {/* ---- Import mapping. Shown only once a sheet is loaded, because a mapping table with no
              sheet behind it is a form asking about a file that does not exist yet. ---- */}
      {importColumns ? (
        <Card>
          <SectionTitle right={
            <div style={{ display: "flex", gap: 8 }}>
              <Button variant="secondary" onClick={cancelImport}>Cancel</Button>
              <Button onClick={() => void runImport()} disabled={importing} data-testid="dt-import-run">
                {importing ? "Importing…" : `Import ${importRows.length} rows`}
              </Button>
            </div>
          }>
            Map the spreadsheet
          </SectionTitle>
          <div style={{ fontSize: 12.5, color: "var(--rm-dim)", margin: "2px 0 14px" }}>
            Each input column becomes a dimension of the matrix and needs a variable to read.
            Cells may say <code>&gt;= 1000</code>, <code>100 - 999</code>, <code>gold|silver</code>,
            or <code>*</code> for “any”.
          </div>

          <div style={{ display: "grid", gridTemplateColumns: "1fr 140px 1fr", gap: 10, marginBottom: 14 }}>
            <Field label="Table name"><Input value={importName} onChange={(e) => setImportName(e.target.value)} /></Field>
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
            {importColumns.map((column, index) => (
              <div key={column.header} style={{ display: "grid", gridTemplateColumns: "1fr 130px 1fr", gap: 10, alignItems: "center" }}>
                <div style={{ fontSize: 13, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis" }}>
                  {column.header}
                  <span style={{ color: "var(--rm-dim)", fontWeight: 400, marginLeft: 8, fontSize: 11.5, fontFamily: "var(--font-mono)" }}>
                    {String(importRows[0]?.[column.header] ?? "")}
                  </span>
                </div>
                <Select
                  value={column.role}
                  onChange={(e) => setImportColumns(importColumns.map((c, i) =>
                    i === index ? { ...c, role: e.target.value as ImportRole } : c))}
                >
                  <option value="input">Dimension</option>
                  <option value="output">Output</option>
                  <option value="ignore">Ignore</option>
                </Select>
                {column.role === "input" ? (
                  <Select
                    value={column.variable_id || ""}
                    onChange={(e) => {
                      const v = variables.find((x) => x.id === e.target.value);
                      setImportColumns(importColumns.map((c, i) => i === index
                        ? { ...c, variable_id: e.target.value, field_type: v?.field_type || undefined } : c));
                    }}
                  >
                    <option value="">Choose a variable…</option>
                    {variables.map((v) => <option key={v.id} value={v.id}>{v.name || v.id}</option>)}
                  </Select>
                ) : column.role === "output" ? (
                  <Select
                    value={column.type || ""}
                    onChange={(e) => setImportColumns(importColumns.map((c, i) =>
                      i === index ? { ...c, type: e.target.value || undefined } : c))}
                  >
                    <option value="">Value — type read from the column</option>
                    <option value="outcome">Outcome (approve / review / reject)</option>
                  </Select>
                ) : (
                  <div style={{ fontSize: 12, color: "var(--rm-dim)" }}>Not imported.</div>
                )}
              </div>
            ))}
          </div>

          {importProblems.length ? (
            <div style={{ marginTop: 16, padding: 12, borderRadius: 10, border: "1px solid var(--rm-danger)" }} data-testid="dt-import-problems">
              <div style={{ fontSize: 13, fontWeight: 700, color: "var(--rm-danger)", marginBottom: 8 }}>
                <AlertTriangle size={14} style={{ verticalAlign: "-2px" }} /> {importProblems.length} problem{importProblems.length === 1 ? "" : "s"} — nothing was imported
              </div>
              {/* Every problem at once, each naming its cell. The import is refused rather than
                  partially applied: a skipped condition is a WILDCARD, so a half-imported row
                  would apply its price to every input instead of the one the sheet meant. */}
              <div style={{ display: "flex", flexDirection: "column", gap: 4, maxHeight: 220, overflowY: "auto" }}>
                {importProblems.map((problem, index) => (
                  <div key={index} style={{ fontSize: 12.5 }}>
                    {problem.row ? <strong>Row {problem.row}</strong> : null}
                    {problem.header ? <span style={{ color: "var(--rm-dim)" }}> · {problem.header}</span> : null}
                    <span> — {problem.message}</span>
                  </div>
                ))}
              </div>
            </div>
          ) : null}
        </Card>
      ) : null}

      {error ? <Card><div style={{ color: "var(--rm-danger)", fontSize: 13 }}>{error}</div></Card> : null}
      {status ? <Card><div style={{ color: "var(--rm-success)", fontSize: 13 }}>{status}</div></Card> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 300px", gap: 20, alignItems: "start" }}>
        {/* ---- Editor ---- */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
          <Card>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 200px", gap: 12 }}>
              <Field label="Table name"><Input value={draft.name} onChange={(e) => patch({ name: e.target.value })} placeholder="e.g. Risk tiering" /></Field>
              <Field label="Hit policy">
                <Select value={draft.hit_policy} onChange={(e) => patch({ hit_policy: e.target.value })}>
                  {HIT_POLICIES.map((h) => <option key={h.v} value={h.v}>{h.label}</option>)}
                </Select>
              </Field>
            </div>
          </Card>

          {/* Column headers editor */}
          <Card>
            <SectionTitle right={<div style={{ display: "flex", gap: 8 }}><Button variant="secondary" onClick={addInput}><Plus size={14} /> Input</Button><Button variant="secondary" onClick={addOutput}><Plus size={14} /> Output</Button></div>}>Columns</SectionTitle>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 10, marginTop: 6 }}>
              {draft.inputs.map((c) => (
                <div key={c.id} style={{ border: "1px solid var(--rm-border)", borderRadius: 8, padding: 10, minWidth: 220, background: "var(--rm-accent-bg)" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                    <span style={{ fontSize: 11, fontWeight: 700, color: "var(--rm-accent)", textTransform: "uppercase", letterSpacing: 0.4 }}>Input</span>
                    <button onClick={() => removeInput(c.id)} title="Remove input" style={{ background: "none", border: "none", cursor: "pointer", color: "var(--rm-muted)" }}><Trash2 size={14} /></button>
                  </div>
                  <Input value={c.name} onChange={(e) => setInput(c.id, { name: e.target.value })} aria-label="Column label" placeholder="Column label" />
                  <div style={{ height: 6 }} />
                  <Select value={c.variable_id} onChange={(e) => { const v = variables.find((x) => x.id === e.target.value); setInput(c.id, { variable_id: e.target.value, field_type: v?.field_type || c.field_type }); }}>
                    <option value="">— bind variable —</option>
                    {variables.map((v) => <option key={v.id} value={v.id}>{v.name}</option>)}
                  </Select>
                  <div style={{ height: 6 }} />
                  <Select value={c.field_type} onChange={(e) => setInput(c.id, { field_type: e.target.value })}>
                    {["number", "string", "boolean"].map((t) => <option key={t} value={t}>{t}</option>)}
                  </Select>
                </div>
              ))}
              {draft.outputs.map((c) => (
                <div key={c.id} style={{ border: "1px solid var(--rm-border)", borderRadius: 8, padding: 10, minWidth: 200, background: "var(--rm-success-bg)" }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 6 }}>
                    <span style={{ fontSize: 11, fontWeight: 700, color: "var(--rm-success)", textTransform: "uppercase", letterSpacing: 0.4 }}>Output</span>
                    <button onClick={() => removeOutput(c.id)} title="Remove output" style={{ background: "none", border: "none", cursor: "pointer", color: "var(--rm-muted)" }}><Trash2 size={14} /></button>
                  </div>
                  <Input value={c.name} onChange={(e) => setOutput(c.id, { name: e.target.value })} aria-label="Output label" placeholder="Output label" />
                  <div style={{ height: 6 }} />
                  <Select value={c.type} onChange={(e) => setOutput(c.id, { type: e.target.value })}>
                    {["outcome", "string", "number", "boolean"].map((t) => <option key={t} value={t}>{t}</option>)}
                  </Select>
                </div>
              ))}
            </div>
          </Card>

          {/* Rows grid */}
          <Card>
            <SectionTitle right={<Button variant="secondary" onClick={addRow}><Plus size={14} /> Row</Button>}>Rows</SectionTitle>
            <div style={{ overflowX: "auto" }}>
              <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
                <thead>
                  <tr>
                    <th style={{ width: 34 }} />
                    {draft.inputs.map((c) => <th key={c.id} style={thStyle("var(--rm-accent)")}>{c.name}</th>)}
                    {draft.outputs.map((c) => <th key={c.id} style={thStyle("var(--rm-success)")}>{c.name}</th>)}
                    <th style={{ width: 74 }} />
                  </tr>
                </thead>
                <tbody>
                  {draft.rows.map((row, i) => {
                    const sev = rowSeverity(row.id);
                    const rowBg = sev === "error" ? "var(--rm-danger-bg)" : sev === "warning" ? "var(--rm-warning-bg)" : "transparent";
                    return (
                      <tr
                        key={row.id}
                        draggable
                        onDragStart={() => { dragRow.current = i; }}
                        onDragOver={(e) => e.preventDefault()}
                        onDrop={() => { if (dragRow.current !== null) { moveRow(dragRow.current, i); dragRow.current = null; } }}
                        style={{ background: rowBg, borderBottom: "1px solid var(--rm-border)" }}
                      >
                        <td style={{ textAlign: "center", cursor: "grab", color: "var(--rm-muted)" }} title="Drag to reorder"><GripVertical size={14} /></td>
                        {draft.inputs.map((c) => {
                          const cell = row.cells[c.id] || { operator: "any" };
                          return (
                            <td key={c.id} style={tdStyle}>
                              <div style={{ display: "flex", gap: 4 }}>
                                <Select value={cell.operator} onChange={(e) => setCell(row.id, c.id, { operator: e.target.value })} style={{ width: 78, fontSize: 12, padding: "4px 6px" }}>
                                  {OPERATORS.map((o) => <option key={o} value={o}>{o}</option>)}
                                </Select>
                                {cellNeedsValue(cell.operator) ? (
                                  <Input aria-label="Condition cell value" value={cell.value ?? ""} onChange={(e) => setCell(row.id, c.id, { value: e.target.value })} placeholder="value" style={{ width: 74, fontSize: 12, padding: "4px 6px" }} />
                                ) : null}
                                {cell.operator === "between" ? (
                                  <Input value={cell.value2 ?? ""} onChange={(e) => setCell(row.id, c.id, { value2: e.target.value })} placeholder="…max" style={{ width: 60, fontSize: 12, padding: "4px 6px" }} />
                                ) : null}
                              </div>
                            </td>
                          );
                        })}
                        {draft.outputs.map((c) => (
                          <td key={c.id} style={tdStyle}>
                            {c.type === "outcome" ? (
                              <Select value={row.outputs[c.id] ?? ""} onChange={(e) => setRowOutput(row.id, c.id, e.target.value)} style={{ fontSize: 12, padding: "4px 6px" }}>
                                <option value="">—</option>
                                {OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}
                              </Select>
                            ) : (
                              <Input aria-label="Output cell value" value={row.outputs[c.id] ?? ""} onChange={(e) => setRowOutput(row.id, c.id, e.target.value)} placeholder="value" style={{ fontSize: 12, padding: "4px 6px" }} />
                            )}
                          </td>
                        ))}
                        <td style={{ textAlign: "center", whiteSpace: "nowrap" }}>
                          <button onClick={() => moveRow(i, i - 1)} title="Up" style={iconBtn}><ArrowUp size={13} /></button>
                          <button onClick={() => moveRow(i, i + 1)} title="Down" style={iconBtn}><ArrowDown size={13} /></button>
                          <button onClick={() => removeRow(row.id)} title="Delete row" style={iconBtn}><Trash2 size={13} /></button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div style={{ marginTop: 14, display: "flex", gap: 8 }}>
              <Button onClick={save}>{draft.id ? "Save changes" : "Create table"}</Button>
            </div>
          </Card>

          {/* Test panel */}
          <Card>
            <SectionTitle>Test</SectionTitle>
            <Field label="Variable values (JSON)">
              <textarea value={testValues} onChange={(e) => setTestValues(e.target.value)} rows={4}
                style={{ width: "100%", fontFamily: "var(--rm-mono, monospace)", fontSize: 12, padding: 10, borderRadius: 8, border: "1px solid var(--rm-border)", background: "var(--rm-input)", color: "var(--rm-text)" }}
                placeholder='{ "score": 800, "fraud_flag": false }' />
            </Field>
            <Button variant="secondary" onClick={runTest}><Play size={14} /> Evaluate</Button>
            {testResult ? (
              <div style={{ marginTop: 12 }}>
                <div style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 8 }}>
                  <span style={{ fontSize: 13, color: "var(--rm-muted)" }}>Outcome:</span>
                  <Badge tone={testResult.outcome === "approve" ? "success" : testResult.outcome === "reject" ? "danger" : "warning"}>{String(testResult.outcome ?? "—")}</Badge>
                  <span style={{ fontSize: 12, color: "var(--rm-muted)" }}>row {String(testResult.winning_row_id ?? "—")}</span>
                </div>
                <pre style={{ background: "var(--rm-bg)", padding: 12, borderRadius: 8, fontSize: 11, overflowX: "auto", maxHeight: 220, border: "1px solid var(--rm-border)" }}>{JSON.stringify(testResult, null, 2)}</pre>
              </div>
            ) : null}
          </Card>
        </div>

        {/* ---- Right column: optimiser + saved tables ---- */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card>
            <SectionTitle right={analysis ? <Badge tone={analysis.ok ? "success" : "danger"}>{analysis.ok ? "OK" : "Issues"}</Badge> : undefined}>Optimiser</SectionTitle>
            {!analysis || analysis.diagnostics.length === 0 ? (
              <div style={{ fontSize: 13, color: "var(--rm-muted)" }}>No conflicts, gaps, unreachable rows, or invalid values detected.</div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {analysis.diagnostics.map((d, idx) => {
                  const tone = sevTone(d.severity);
                  return (
                    <div key={idx} style={{ background: tone.bg, borderRadius: 8, padding: "8px 10px", fontSize: 12 }}>
                      <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 2 }}>
                        <AlertTriangle size={12} color={tone.fg} />
                        <span style={{ color: tone.fg, fontWeight: 700, textTransform: "uppercase", fontSize: 10, letterSpacing: 0.4 }}>{d.type.replace(/_/g, " ")} · {d.severity}</span>
                      </div>
                      <div style={{ color: "var(--rm-text)" }}>{d.description}</div>
                    </div>
                  );
                })}
              </div>
            )}
          </Card>

          <Card>
            <SectionTitle>Saved tables</SectionTitle>
            {tables.length === 0 ? (
              <EmptyState icon={<Table2 size={20} />} title="No tables yet" hint="Author a grid and save it." />
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {tables.map((t) => (
                  <div key={t.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, padding: "8px 10px", borderRadius: 8, border: "1px solid var(--rm-border)", background: draft.id === t.id ? "var(--rm-accent-bg)" : "transparent" }}>
                    <button onClick={() => { void apiJson<Table>(apiBaseUrl, `/api/v1/decision-tables/${t.id}`, {}, apiKey).then((full) => { setDraft(full); setTestResult(null); }); }}
                      style={{ background: "none", border: "none", textAlign: "left", cursor: "pointer", color: "var(--rm-text)", fontSize: 13, fontWeight: 600, flex: 1 }}>
                      {t.name}
                      <span style={{ display: "block", fontSize: 11, color: "var(--rm-muted)", fontWeight: 400 }}>{t.hit_policy} · v{t.version ?? 1}</span>
                    </button>
                    <button onClick={() => del(t.id)} title="Delete" style={{ background: "none", border: "none", cursor: "pointer", color: "var(--rm-muted)" }}><Trash2 size={14} /></button>
                  </div>
                ))}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

const thStyle = (color: string): React.CSSProperties => ({ textAlign: "left", padding: "8px 6px", fontSize: 11, fontWeight: 700, color, textTransform: "uppercase", letterSpacing: 0.4, borderBottom: "2px solid var(--rm-border)" });
const tdStyle: React.CSSProperties = { padding: "6px" };
const iconBtn: React.CSSProperties = { background: "none", border: "none", cursor: "pointer", color: "var(--rm-muted)", padding: 2 };
