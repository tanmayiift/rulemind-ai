"use client";

import * as React from "react";
import { Upload, Sparkles, Braces, FileSpreadsheet } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Select, Textarea, Stat, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type Policy = { id: string; name: string };
type BatchRow = { index: number; result: { outcome: string; latency_ms?: number; error?: string } };
type Performance = { server_ms: number; throughput_tps: number | null; avg_ms: number | null; path: "fast" | "full_executor"; workers: number };
type BatchResponse = { rows: BatchRow[]; count: number; performance?: Performance };
type DecideResult = { outcome?: string; score?: number; explanation?: unknown; trace_id?: string; [k: string]: unknown };
type Mode = "synthetic" | "upload" | "json";

const OUTCOME_TONE: Record<string, "success" | "warning" | "danger"> = { approve: "success", review: "warning", reject: "danger" };

// ---- data helpers -------------------------------------------------------------

function coerce(v: unknown): unknown {
  if (typeof v !== "string") return v;
  const s = v.trim();
  if (s === "") return "";
  if (s === "true" || s === "TRUE") return true;
  if (s === "false" || s === "FALSE") return false;
  if (/^-?\d+(\.\d+)?$/.test(s)) return Number(s);
  return s;
}

// Minimal RFC-4180-ish CSV parser (quotes, escaped quotes, commas, newlines).
function parseCsv(text: string): Record<string, unknown>[] {
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
    else if (c === "\r") { /* skip */ }
    else field += c;
  }
  if (field.length || row.length) { row.push(field); rows.push(row); }
  const nonEmpty = rows.filter((r) => r.some((c) => c.trim() !== ""));
  if (nonEmpty.length < 1) return [];
  const headers = nonEmpty[0].map((h) => h.trim());
  return nonEmpty.slice(1).map((r) => {
    const obj: Record<string, unknown> = {};
    headers.forEach((h, i) => { if (h) obj[h] = coerce(r[i]); });
    return obj;
  });
}

type SchemaField = { name: string; sample: unknown; source_id: string };

// Deterministic pseudo-random in [0,1) for reproducible synthetic runs.
const rand = (i: number, n: number) => Math.abs(Math.sin(i * 12.9898 + n * 78.233) * 43758.5453) % 1;

// Generate a realistic value for one policy input field, spanning decision
// boundaries so outcomes actually vary (e.g. bureau_score across 500–850).
function synthValue(field: SchemaField, i: number, n: number): unknown {
  const name = field.name.toLowerCase();
  const s = field.sample;
  const r = rand(i, n);
  if (typeof s === "boolean") return r > 0.5;
  if (typeof s === "string") return s;
  if (typeof s === "number" || s === null || s === undefined) {
    if (name.includes("flag") || name.endsWith("_verified") || (s === 0 || s === 1)) return r > 0.5 ? 1 : 0;
    if (name.includes("score")) {
      // bureau/credit/CIBIL scores span 500–850 (~half below a 700 cutoff);
      // other scores (0–100 style) span their natural 0–100 range.
      if (typeof s === "number" && s > 0 && s <= 100) return Math.round(r * 100 * 10) / 10;
      return 500 + Math.floor(r * 350);
    }
    if (name.includes("ratio") || (typeof s === "number" && s > 0 && s < 1)) return Math.round(r * 100) / 100;
    if (name.includes("income") || name.includes("balance") || name.includes("amount") || name.includes("inr")) {
      const base = typeof s === "number" && s > 0 ? s : 100000;
      return Math.floor(base * (0.3 + r * 1.6));
    }
    if (name.includes("age")) return 21 + Math.floor(r * 45);
    const base = typeof s === "number" && s !== 0 ? s : 50;
    return Math.round(base * (0.4 + r * 1.4) * 100) / 100;
  }
  return s;
}

// Policy-aware synthetic case: populates the policy's REAL input fields (so they
// actually reach its rules). Falls back to a generic set when a policy has no
// connector-declared inputs.
function makeCase(i: number, fields: SchemaField[]): Record<string, unknown> {
  const base: Record<string, unknown> = { customer_id: `SIM-${String(i).padStart(6, "0")}` };
  if (fields.length) {
    fields.forEach((f, idx) => { base[f.name] = synthValue(f, i, idx + 1); });
    return base;
  }
  const r = (n: number) => rand(i, n);
  return {
    ...base,
    credit_score: 500 + Math.floor(r(1) * 350), annual_income: 20000 + Math.floor(r(2) * 180000),
    age: 21 + Math.floor(r(3) * 45), dti_ratio: Math.round(r(6) * 60) / 100,
    loan_amount: 1000 + Math.floor(r(11) * 49000), kyc_verified: r(13) > 0.1,
  };
}

function pct(values: number[], p: number): number {
  if (!values.length) return 0;
  const s = [...values].sort((a, b) => a - b);
  return s[Math.min(s.length - 1, Math.floor(p * s.length))];
}

export default function SimulationPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();

  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [policyId, setPolicyId] = React.useState("");
  const [schemaFields, setSchemaFields] = React.useState<SchemaField[]>([]);
  const [mode, setMode] = React.useState<Mode>("synthetic");
  const [count, setCount] = React.useState(500);
  const [jsonText, setJsonText] = React.useState('[\n  { "customer_id": "sim-1", "credit_score": 720 }\n]');
  const [cases, setCases] = React.useState<Record<string, unknown>[]>([]);
  const [fileName, setFileName] = React.useState<string | null>(null);
  const [parseError, setParseError] = React.useState<string | null>(null);
  const [dragOver, setDragOver] = React.useState(false);

  const [rows, setRows] = React.useState<BatchRow[] | null>(null);
  const [wallMs, setWallMs] = React.useState<number | null>(null);
  const [perf, setPerf] = React.useState<Performance | null>(null);
  const [running, setRunning] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [selected, setSelected] = React.useState<number | null>(null);
  const [detail, setDetail] = React.useState<DecideResult | null>(null);
  const [detailBusy, setDetailBusy] = React.useState(false);
  const [drivers, setDrivers] = React.useState<Array<{ variable_name: string; operator: string; threshold: unknown; fail_count: number; fail_rate: number; share_of_focus: number }> | null>(null);

  React.useEffect(() => {
    (async () => {
      try {
        const p = await apiJson<Policy[]>(apiBaseUrl, "/api/v1/policies", {}, apiKey);
        setPolicies(p);
        setPolicyId(p[0]?.id ?? "");
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unable to load policies.");
      }
    })();
  }, [apiBaseUrl, apiKey]);

  // Fetch the selected policy's real input fields so synthetic cases actually
  // drive its rules (e.g. bureau_score), not arbitrary columns.
  React.useEffect(() => {
    if (!policyId) { setSchemaFields([]); return; }
    let active = true;
    apiJson<{ fields: SchemaField[] }>(apiBaseUrl, `/api/v1/policies/${policyId}/input-schema`, {}, apiKey)
      .then((s) => { if (active) setSchemaFields(s.fields || []); })
      .catch(() => { if (active) setSchemaFields([]); });
    return () => { active = false; };
  }, [apiBaseUrl, apiKey, policyId]);

  // keep `cases` in sync with the active input mode
  React.useEffect(() => {
    if (mode === "synthetic") {
      setCases(Array.from({ length: Math.max(1, Math.min(count, 5000)) }, (_, i) => makeCase(i + 1, schemaFields)));
      setParseError(null);
    } else if (mode === "json") {
      try {
        const v = JSON.parse(jsonText);
        if (!Array.isArray(v)) throw new Error("JSON must be an array of objects.");
        setCases(v);
        setParseError(null);
      } catch (e) {
        setCases([]);
        setParseError(e instanceof Error ? e.message : "Invalid JSON.");
      }
    }
  }, [mode, count, jsonText, schemaFields]);

  const ingestFile = async (file: File) => {
    setParseError(null);
    setFileName(file.name);
    try {
      let parsed: Record<string, unknown>[];
      if (/\.csv$/i.test(file.name)) {
        parsed = parseCsv(await file.text());
      } else if (/\.xlsx?$/i.test(file.name)) {
        const XLSX = await import("xlsx"); // dynamic — keeps SheetJS off the main bundle
        const wb = XLSX.read(await file.arrayBuffer(), { type: "array" });
        const sheet = wb.Sheets[wb.SheetNames[0]];
        parsed = (XLSX.utils.sheet_to_json(sheet, { defval: "" }) as Record<string, unknown>[]).map((r) => {
          const o: Record<string, unknown> = {};
          for (const k of Object.keys(r)) o[k] = coerce(r[k]);
          return o;
        });
      } else {
        throw new Error("Unsupported file — use .csv, .xls, or .xlsx.");
      }
      if (!parsed.length) throw new Error("No rows found in the file.");
      setCases(parsed);
      setMode("upload");
    } catch (e) {
      setCases([]);
      setParseError(e instanceof Error ? e.message : "Could not read the file.");
    }
  };

  const columns = React.useMemo(() => {
    const set = new Set<string>();
    cases.slice(0, 50).forEach((c) => Object.keys(c).forEach((k) => set.add(k)));
    return [...set];
  }, [cases]);

  const run = React.useCallback(async () => {
    if (!cases.length) { setError("No cases to run — add data first."); return; }
    setError(null); setSelected(null); setDetail(null); setRunning(true);
    const t0 = performance.now();
    try {
      const res = await apiJson<BatchResponse>(
        apiBaseUrl, "/api/v1/decide/batch",
        { method: "POST", body: JSON.stringify({ targetType: "decide", targetId: policyId, payloads: cases }) },
        apiKey
      );
      setRows(res.rows);
      setWallMs(performance.now() - t0);
      setPerf(res.performance ?? null);
      // pure-compute analytic: which predictor drives the most rejections
      try {
        const da = await apiJson<{ drivers: typeof drivers }>(apiBaseUrl, "/api/v1/analytics/rejection-drivers", { method: "POST", body: JSON.stringify({ policy_id: policyId, limit: 1000 }) }, apiKey);
        setDrivers((da.drivers || []).filter((d) => d.fail_count > 0).slice(0, 8));
      } catch { setDrivers(null); }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Simulation failed.");
    } finally {
      setRunning(false);
    }
  }, [apiBaseUrl, apiKey, policyId, cases]);

  const reviewCase = async (index: number) => {
    setSelected(index); setDetail(null);
    const payload = cases[index];
    if (!payload) return;
    setDetailBusy(true);
    try {
      const res = await apiJson<DecideResult>(apiBaseUrl, "/api/v1/decide", { method: "POST", body: JSON.stringify({ policyId, payload }) }, apiKey);
      setDetail(res);
    } catch (e) {
      setDetail({ outcome: "error", explanation: e instanceof Error ? e.message : "decide failed" });
    } finally {
      setDetailBusy(false);
    }
  };

  const dist = (rows ?? []).reduce<Record<string, number>>((acc, r) => { acc[r.result.outcome] = (acc[r.result.outcome] ?? 0) + 1; return acc; }, {});
  const total = rows?.length ?? 0;
  const latencies = (rows ?? []).map((r) => r.result.latency_ms ?? 0).filter((x) => x > 0);
  const errors = (rows ?? []).filter((r) => r.result.error || r.result.outcome === "error").length;
  const successRate = total ? ((total - errors) / total) * 100 : 0;
  // Prefer the server-measured compute throughput; the client wall time also
  // includes network transfer + JSON parsing of the whole batch, so it understates.
  const serverTps = perf?.throughput_tps ?? null;
  const wallTps = wallMs && wallMs > 0 ? (total / wallMs) * 1000 : 0;
  const tps = serverTps ?? wallTps;

  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="Simulation & backtest" subtitle="Backtest a policy against your own book — upload CSV/Excel, generate synthetic cases, or paste JSON — then measure latency, success rate, and drill into any decision." />

      <div style={{ display: "grid", gridTemplateColumns: "minmax(360px, 420px) minmax(0,1fr)", gap: 18, alignItems: "start" }}>
        {/* ---- controls ---- */}
        <Card>
          <Field label="Policy" style={{ marginBottom: 16 }}>
            <Select value={policyId} onChange={(e) => setPolicyId(e.target.value)}>
              {policies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          </Field>

          {/* mode switch */}
          <div style={{ display: "flex", gap: 4, padding: 4, background: "var(--rm-hover)", borderRadius: 12, marginBottom: 16 }}>
            {([["upload", "Upload", Upload], ["synthetic", "Synthetic", Sparkles], ["json", "JSON", Braces]] as const).map(([m, label, Icon]) => (
              <button key={m} onClick={() => setMode(m)}
                style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "8px 6px", borderRadius: 9, border: "none", cursor: "pointer",
                  fontSize: 12.5, fontWeight: 600, background: mode === m ? "var(--rm-card)" : "transparent", color: mode === m ? "var(--rm-text)" : "var(--rm-muted)", boxShadow: mode === m ? "var(--rm-shadow-sm)" : "none" }}>
                <Icon size={14} /> {label}
              </button>
            ))}
          </div>

          {mode === "upload" ? (
            <div
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => { e.preventDefault(); setDragOver(false); const f = e.dataTransfer.files[0]; if (f) ingestFile(f); }}
              style={{ border: `1.5px dashed ${dragOver ? "var(--rm-accent)" : "var(--rm-border-strong)"}`, borderRadius: 12, padding: "28px 16px", textAlign: "center", background: dragOver ? "var(--rm-accent-bg)" : "var(--rm-editor)", transition: "all .12s" }}>
              <FileSpreadsheet size={26} style={{ color: "var(--rm-accent)", marginBottom: 8 }} />
              <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--rm-text)" }}>Drop a CSV or Excel file</div>
              <div style={{ fontSize: 12, color: "var(--rm-dim)", margin: "4px 0 12px" }}>Each row becomes a decision payload. .csv · .xls · .xlsx</div>
              <label>
                <input type="file" accept=".csv,.xls,.xlsx" style={{ display: "none" }} onChange={(e) => { const f = e.target.files?.[0]; if (f) ingestFile(f); }} />
                <span className="rm-btn rm-btn-secondary rm-btn-sm" style={{ cursor: "pointer" }}>Browse files</span>
              </label>
              {fileName ? <div style={{ marginTop: 12, fontSize: 12, color: "var(--rm-muted)" }}>Loaded <strong>{fileName}</strong></div> : null}
            </div>
          ) : mode === "synthetic" ? (
            <Field label="Number of synthetic cases (customer_id + 20 vars)">
              <div style={{ display: "flex", gap: 8 }}>
                <input className="rm-input" type="number" min={1} max={5000} value={count} onChange={(e) => setCount(parseInt(e.target.value || "1", 10))} style={{ maxWidth: 140 }} />
                <span style={{ alignSelf: "center", fontSize: 12, color: "var(--rm-dim)" }}>up to 5,000</span>
              </div>
            </Field>
          ) : (
            <Field label="Payloads (JSON array)">
              <Textarea mono rows={9} value={jsonText} onChange={(e) => setJsonText(e.target.value)} style={{ fontSize: 12 }} />
            </Field>
          )}

          {parseError ? <div style={{ marginTop: 10, fontSize: 12.5, color: "var(--rm-danger)" }}>{parseError}</div> : null}

          {/* dataset preview */}
          {cases.length > 0 ? (
            <div style={{ marginTop: 16 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                <span className="rm-label">Preview</span>
                <Badge tone="accent">{cases.length.toLocaleString()} cases · {columns.length} fields</Badge>
              </div>
              <div style={{ overflow: "auto", maxHeight: 180, border: "1px solid var(--rm-border)", borderRadius: 10 }}>
                <table style={{ borderCollapse: "collapse", fontSize: 11.5, width: "100%" }}>
                  <thead>
                    <tr>{columns.slice(0, 6).map((c) => <th key={c} style={{ textAlign: "left", padding: "7px 10px", position: "sticky", top: 0, background: "var(--rm-card-alt)", color: "var(--rm-muted)", fontWeight: 600, borderBottom: "1px solid var(--rm-border)", whiteSpace: "nowrap" }}>{c}</th>)}</tr>
                  </thead>
                  <tbody>
                    {cases.slice(0, 6).map((row, i) => (
                      <tr key={i}>{columns.slice(0, 6).map((c) => <td key={c} className="rm-mono" style={{ padding: "6px 10px", color: "var(--rm-muted)", borderBottom: "1px solid var(--rm-border)", whiteSpace: "nowrap", maxWidth: 120, overflow: "hidden", textOverflow: "ellipsis" }}>{String((row as Record<string, unknown>)[c] ?? "")}</td>)}</tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}

          <Button variant="primary" size="lg" onClick={run} disabled={running || !policyId || !cases.length} style={{ width: "100%", marginTop: 16 }}>
            {running ? "Running…" : `Run simulation${cases.length ? ` · ${cases.length.toLocaleString()} cases` : ""}`}
          </Button>
          {error ? <div style={{ marginTop: 12, color: "var(--rm-danger)", fontSize: 13 }}>{error}</div> : null}
        </Card>

        {/* ---- results ---- */}
        <div style={{ display: "grid", gap: 18 }}>
          {rows ? (
            <>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(118px, 1fr))", gap: 12 }}>
                <Stat label="Cases" value={total.toLocaleString()} />
                <Stat label="Success" value={`${successRate.toFixed(successRate === 100 ? 0 : 1)}%`} tone={errors ? "warning" : "success"} />
                <Stat label="Throughput" value={`${Math.round(tps)} TPS`} tone="accent" />
                <Stat label="p50" value={`${Math.round(pct(latencies, 0.5))}ms`} />
                <Stat label="p95" value={`${Math.round(pct(latencies, 0.95))}ms`} />
                <Stat label="p99" value={`${Math.round(pct(latencies, 0.99))}ms`} />
              </div>
              {perf ? (
                <div style={{ fontSize: 12, color: "var(--rm-muted)", lineHeight: 1.5 }}>
                  {Math.round(tps).toLocaleString()} decisions/sec measured server-side ({perf.avg_ms}ms avg · {perf.workers} workers ·{" "}
                  <Badge tone={perf.path === "fast" ? "success" : "accent"}>{perf.path === "fast" ? "fast path (cached bundle + Rust core)" : "full executor"}</Badge>).
                  {perf.path === "full_executor"
                    ? " This policy runs the full workflow engine (connector callbacks, scorecard, review gate, sandboxed features), so per-node cost is higher; throughput scales linearly with replicas (K8s HPA). Lean rules-only policies use the fast path at much higher TPS."
                    : " Rules-only policies serve from the cached bundle via the stateless core."}
                </div>
              ) : null}
            </>
          ) : null}

          {rows && drivers && drivers.length > 0 ? (
            <Card>
              <SectionTitle>Top rejection drivers</SectionTitle>
              <div style={{ fontSize: 12.5, color: "var(--rm-dim)", marginBottom: 12 }}>
                Conditions failing most often on non-approved decisions — exact counts from the decision log (no AI).
              </div>
              <div style={{ display: "grid", gap: 8 }}>
                {drivers.map((d, i) => (
                  <div key={i} style={{ display: "flex", alignItems: "center", gap: 12 }}>
                    <span style={{ width: 20, color: "var(--rm-dim)", fontSize: 12, fontFamily: "var(--font-mono)" }}>{i + 1}</span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 4 }}>
                        <span style={{ fontWeight: 600, color: "var(--rm-text)" }}>{d.variable_name} <span style={{ color: "var(--rm-dim)", fontFamily: "var(--font-mono)" }}>{d.operator} {String(d.threshold ?? "")}</span></span>
                        <span className="rm-mono" style={{ color: "var(--rm-muted)" }}>{d.fail_count} fails · {Math.round(d.share_of_focus * 100)}% of rejects</span>
                      </div>
                      <div style={{ height: 7, borderRadius: 999, background: "var(--rm-hover)", overflow: "hidden" }}>
                        <div style={{ height: "100%", width: `${Math.round(d.share_of_focus * 100)}%`, background: "var(--rm-danger)", borderRadius: 999 }} />
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          ) : null}

          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 18, alignItems: "start" }}>
            <Card>
              <SectionTitle>Cases {total ? `· ${total.toLocaleString()}` : ""}</SectionTitle>
              {!rows ? (
                <EmptyState icon={<Sparkles size={22} />} title="No run yet" hint="Add a dataset on the left and run the simulation to see the outcome mix and latency." />
              ) : (
                <>
                  <div style={{ display: "grid", gap: 10, marginBottom: 16 }}>
                    {["approve", "review", "reject"].map((o) => {
                      const n = dist[o] ?? 0; const p = total ? Math.round((n / total) * 100) : 0;
                      return (
                        <div key={o}>
                          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 5 }}>
                            <span style={{ textTransform: "capitalize", fontWeight: 600, color: "var(--rm-text)" }}>{o}</span>
                            <span className="rm-mono" style={{ color: "var(--rm-dim)" }}>{n} · {p}%</span>
                          </div>
                          <div style={{ height: 8, borderRadius: 999, background: "var(--rm-hover)", overflow: "hidden" }}>
                            <div style={{ height: "100%", width: p + "%", background: `var(--rm-${OUTCOME_TONE[o]})`, borderRadius: 999 }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                  <div style={{ maxHeight: 340, overflowY: "auto", border: "1px solid var(--rm-border)", borderRadius: 10 }}>
                    {rows.map((r) => {
                      const cust = (cases[r.index] as { customer_id?: string } | undefined)?.customer_id;
                      const tone: "success" | "warning" | "danger" | "neutral" = OUTCOME_TONE[r.result.outcome] ?? "neutral";
                      return (
                        <div key={r.index} onClick={() => reviewCase(r.index)} role="button"
                          style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center", padding: "9px 12px", borderBottom: "1px solid var(--rm-border)", fontSize: 12.5, cursor: "pointer", background: selected === r.index ? "var(--rm-hover)" : "transparent" }}>
                          <span className="rm-mono" style={{ color: "var(--rm-muted)", flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{cust ?? `#${r.index}`}</span>
                          <Badge tone={tone}>{r.result.outcome}</Badge>
                          <span className="rm-mono" style={{ color: "var(--rm-dim)", width: 56, textAlign: "right" }}>{r.result.latency_ms ?? "—"}ms</span>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </Card>

            <Card>
              <SectionTitle>Case review</SectionTitle>
              {selected === null ? (
                <EmptyState icon={<Braces size={22} />} title="Pick a case" hint="Click any case to inspect its inputs and the full decision — outcome, score, trace, and explanation." />
              ) : (
                <div style={{ display: "grid", gap: 14 }}>
                  <div>
                    <div className="rm-label" style={{ marginBottom: 6 }}>Inputs</div>
                    <pre style={preStyle}>{JSON.stringify(cases[selected], null, 2)}</pre>
                  </div>
                  <div>
                    <div className="rm-label" style={{ marginBottom: 6 }}>Decision {detailBusy ? "· loading…" : ""}</div>
                    {detail ? (
                      <div>
                        <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 8 }}>
                          <Badge tone={OUTCOME_TONE[detail.outcome ?? ""] ?? "neutral"}>{detail.outcome ?? "—"}</Badge>
                          {typeof detail.score === "number" ? <span style={{ fontSize: 12.5, color: "var(--rm-muted)" }}>score {detail.score}</span> : null}
                          {detail.trace_id ? <span className="rm-mono" style={{ fontSize: 11, color: "var(--rm-dim)" }}>{String(detail.trace_id).slice(0, 12)}</span> : null}
                        </div>
                        <pre style={preStyle}>{JSON.stringify(detail.explanation ?? detail, null, 2).slice(0, 4000)}</pre>
                      </div>
                    ) : <div style={{ color: "var(--rm-dim)", fontSize: 12.5, padding: "8px 0" }}>{detailBusy ? "Fetching full decision…" : "—"}</div>}
                  </div>
                </div>
              )}
            </Card>
          </div>
        </div>
      </div>
    </div>
  );
}

const preStyle: React.CSSProperties = {
  margin: 0, padding: 12, background: "var(--rm-editor)", border: "1px solid var(--rm-border)", borderRadius: 10,
  fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--rm-code-text)", whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 240, overflow: "auto",
};
