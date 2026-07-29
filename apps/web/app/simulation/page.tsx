"use client";

import * as React from "react";
import { Upload, Sparkles, Braces, FileSpreadsheet } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Select, Textarea, Stat, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type Policy = { id: string; name: string };
type BatchRow = { index: number; result: { outcome: string; latency_ms?: number; error?: string } };
type BatchResponse = { rows: BatchRow[]; count: number };
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

// customer_id + 20 decision variables — matches the 10k-customer sim spec.
function makeCase(i: number): Record<string, unknown> {
  const r = (n: number) => Math.abs(Math.sin(i * 12.9898 + n * 78.233) * 43758.5453) % 1;
  return {
    customer_id: `SIM-${String(i).padStart(6, "0")}`,
    credit_score: 500 + Math.floor(r(1) * 350), annual_income: 20000 + Math.floor(r(2) * 180000),
    age: 21 + Math.floor(r(3) * 45), employment_years: Math.floor(r(4) * 25), existing_loans: Math.floor(r(5) * 6),
    dti_ratio: Math.round(r(6) * 60) / 100, delinquencies_2y: Math.floor(r(7) * 4), credit_utilization: Math.round(r(8) * 100) / 100,
    num_inquiries: Math.floor(r(9) * 8), oldest_account_months: Math.floor(r(10) * 240), loan_amount: 1000 + Math.floor(r(11) * 49000),
    home_owner: r(12) > 0.5, kyc_verified: r(13) > 0.1, device_risk_score: Math.round(r(14) * 100) / 100,
    bank_balance: Math.floor(r(15) * 40000), monthly_expenses: 500 + Math.floor(r(16) * 6000), savings_rate: Math.round(r(17) * 40) / 100,
    prior_defaults: Math.floor(r(18) * 2), region_risk: ["low", "medium", "high"][Math.floor(r(19) * 3)], channel: ["web", "mobile", "branch"][Math.floor(r(20) * 3)],
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
  const [mode, setMode] = React.useState<Mode>("synthetic");
  const [count, setCount] = React.useState(500);
  const [jsonText, setJsonText] = React.useState('[\n  { "customer_id": "sim-1", "credit_score": 720 }\n]');
  const [cases, setCases] = React.useState<Record<string, unknown>[]>([]);
  const [fileName, setFileName] = React.useState<string | null>(null);
  const [parseError, setParseError] = React.useState<string | null>(null);
  const [dragOver, setDragOver] = React.useState(false);

  const [rows, setRows] = React.useState<BatchRow[] | null>(null);
  const [wallMs, setWallMs] = React.useState<number | null>(null);
  const [running, setRunning] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [selected, setSelected] = React.useState<number | null>(null);
  const [detail, setDetail] = React.useState<DecideResult | null>(null);
  const [detailBusy, setDetailBusy] = React.useState(false);

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

  // keep `cases` in sync with the active input mode
  React.useEffect(() => {
    if (mode === "synthetic") {
      setCases(Array.from({ length: Math.max(1, Math.min(count, 5000)) }, (_, i) => makeCase(i + 1)));
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
  }, [mode, count, jsonText]);

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
  const tps = wallMs && wallMs > 0 ? (total / wallMs) * 1000 : 0;

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
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(118px, 1fr))", gap: 12 }}>
              <Stat label="Cases" value={total.toLocaleString()} />
              <Stat label="Success" value={`${successRate.toFixed(successRate === 100 ? 0 : 1)}%`} tone={errors ? "warning" : "success"} />
              <Stat label="Throughput" value={`${Math.round(tps)} TPS`} />
              <Stat label="p50" value={`${Math.round(pct(latencies, 0.5))}ms`} />
              <Stat label="p95" value={`${Math.round(pct(latencies, 0.95))}ms`} />
              <Stat label="p99" value={`${Math.round(pct(latencies, 0.99))}ms`} />
            </div>
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
