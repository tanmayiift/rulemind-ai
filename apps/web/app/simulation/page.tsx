"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES, type ThemeTokens } from "../../src/v3/theme";

type Policy = { id: string; name: string };
type BatchRow = { index: number; result: { outcome: string; latency_ms?: number; error?: string } };
type BatchResponse = { rows: BatchRow[]; count: number };
type DecideResult = { outcome?: string; score?: number; explanation?: unknown; trace_id?: string; [k: string]: unknown };

const OUTCOME_TONE: Record<string, "success" | "warning" | "danger"> = { approve: "success", review: "warning", reject: "danger" };

// Deterministic-ish synthetic case with a customer_id + ~20 decision variables,
// matching the 10k-customer simulation spec so the tab exercises real payloads.
function makeCase(i: number): Record<string, unknown> {
  const r = (n: number) => Math.abs(Math.sin(i * 12.9898 + n * 78.233) * 43758.5453) % 1;
  return {
    customer_id: `SIM-${String(i).padStart(6, "0")}`,
    credit_score: 500 + Math.floor(r(1) * 350),
    annual_income: 20000 + Math.floor(r(2) * 180000),
    age: 21 + Math.floor(r(3) * 45),
    employment_years: Math.floor(r(4) * 25),
    existing_loans: Math.floor(r(5) * 6),
    dti_ratio: Math.round(r(6) * 60) / 100,
    delinquencies_2y: Math.floor(r(7) * 4),
    credit_utilization: Math.round(r(8) * 100) / 100,
    num_inquiries: Math.floor(r(9) * 8),
    oldest_account_months: Math.floor(r(10) * 240),
    loan_amount: 1000 + Math.floor(r(11) * 49000),
    home_owner: r(12) > 0.5,
    kyc_verified: r(13) > 0.1,
    device_risk_score: Math.round(r(14) * 100) / 100,
    bank_balance: Math.floor(r(15) * 40000),
    monthly_expenses: 500 + Math.floor(r(16) * 6000),
    savings_rate: Math.round(r(17) * 40) / 100,
    prior_defaults: Math.floor(r(18) * 2),
    region_risk: ["low", "medium", "high"][Math.floor(r(19) * 3)],
    channel: ["web", "mobile", "branch"][Math.floor(r(20) * 3)],
  };
}

function pct(values: number[], p: number): number {
  if (!values.length) return 0;
  const s = [...values].sort((a, b) => a - b);
  const idx = Math.min(s.length - 1, Math.floor(p * s.length));
  return s[idx];
}

export default function SimulationPage() {
  const { apiBaseUrl, apiKey, themeMode, isMobile } = useRuleMindStore();
  const theme = THEMES[themeMode];
  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [policyId, setPolicyId] = React.useState("");
  const [count, setCount] = React.useState(200);
  const [payloadText, setPayloadText] = React.useState('[\n  { "customer_id": "sim-1" },\n  { "customer_id": "sim-2" }\n]');
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

  const generate = () => {
    const n = Math.max(1, Math.min(count, 5000));
    setPayloadText(JSON.stringify(Array.from({ length: n }, (_, i) => makeCase(i + 1)), null, 0));
  };

  const parsedCases = React.useMemo<Record<string, unknown>[]>(() => {
    try { const v = JSON.parse(payloadText); return Array.isArray(v) ? v : []; } catch { return []; }
  }, [payloadText]);

  const run = React.useCallback(async () => {
    setError(null);
    setSelected(null);
    setDetail(null);
    let payloads: unknown[];
    try {
      payloads = JSON.parse(payloadText);
      if (!Array.isArray(payloads)) throw new Error("Payload must be a JSON array of objects.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid JSON.");
      return;
    }
    setRunning(true);
    const t0 = performance.now();
    try {
      const res = await apiJson<BatchResponse>(
        apiBaseUrl,
        "/api/v1/decide/batch",
        { method: "POST", body: JSON.stringify({ targetType: "decide", targetId: policyId, payloads }) },
        apiKey
      );
      setRows(res.rows);
      setWallMs(performance.now() - t0);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Simulation failed.");
    } finally {
      setRunning(false);
    }
  }, [apiBaseUrl, apiKey, policyId, payloadText]);

  const reviewCase = async (index: number) => {
    setSelected(index);
    setDetail(null);
    const payload = parsedCases[index];
    if (!payload) return;
    setDetailBusy(true);
    try {
      const res = await apiJson<DecideResult>(
        apiBaseUrl, "/api/v1/decide",
        { method: "POST", body: JSON.stringify({ policyId, payload }) },
        apiKey
      );
      setDetail(res);
    } catch (e) {
      setDetail({ outcome: "error", explanation: e instanceof Error ? e.message : "decide failed" });
    } finally {
      setDetailBusy(false);
    }
  };

  const dist = (rows ?? []).reduce<Record<string, number>>((acc, r) => {
    const o = r.result.outcome;
    acc[o] = (acc[o] ?? 0) + 1;
    return acc;
  }, {});
  const total = rows?.length ?? 0;
  const latencies = (rows ?? []).map((r) => r.result.latency_ms ?? 0).filter((x) => x > 0);
  const errors = (rows ?? []).filter((r) => r.result.error || r.result.outcome === "error").length;
  const successRate = total ? ((total - errors) / total) * 100 : 0;
  const tps = wallMs && wallMs > 0 ? (total / wallMs) * 1000 : 0;
  const card: React.CSSProperties = { background: theme.card, border: "1px solid " + theme.border, borderRadius: 12 };

  return (
    <div style={{ padding: isMobile ? 16 : 24, color: theme.text }}>
      <h1 style={{ fontSize: 22, letterSpacing: "-0.02em", margin: 0 }}>Simulation &amp; backtest</h1>
      <p style={{ color: theme.muted, margin: "4px 0 20px", fontSize: 13.5 }}>
        Run a policy against a batch of payloads, measure latency &amp; success rate, and drill into any individual case.
      </p>

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "380px minmax(0,1fr)", gap: 16, alignItems: "start" }}>
        {/* controls */}
        <div style={{ ...card, padding: 16 }}>
          <label style={labelStyle(theme)}>Policy</label>
          <select value={policyId} onChange={(e) => setPolicyId(e.target.value)} style={{ ...fieldStyle(theme), margin: "6px 0 14px" }}>
            {policies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>

          <label style={labelStyle(theme)}>Synthetic dataset</label>
          <div style={{ display: "flex", gap: 8, margin: "6px 0 14px" }}>
            <input type="number" min={1} max={5000} value={count} onChange={(e) => setCount(parseInt(e.target.value || "1", 10))} style={{ ...fieldStyle(theme), width: 100 }} />
            <button onClick={generate} style={ghostStyle(theme)}>Generate cases</button>
          </div>

          <label style={labelStyle(theme)}>Dataset (JSON array · customer_id + 20 vars)</label>
          <textarea value={payloadText} onChange={(e) => setPayloadText(e.target.value)} rows={10}
            style={{ ...fieldStyle(theme), marginTop: 6, fontFamily: "var(--font-mono)", fontSize: 12, resize: "vertical" }} />
          <div style={{ fontSize: 11, color: theme.dim, margin: "6px 0 12px" }}>{parsedCases.length} case(s) parsed</div>

          <button onClick={run} disabled={running || !policyId} style={ctaStyle(theme, running || !policyId)}>
            {running ? "Running…" : `Run simulation${parsedCases.length ? ` · ${parsedCases.length} cases` : ""}`}
          </button>
          {error ? <div style={{ marginTop: 12, color: theme.danger, fontSize: 13 }}>{error}</div> : null}
        </div>

        {/* results */}
        <div style={{ display: "grid", gap: 16 }}>
          {/* perf stat strip */}
          {rows ? (
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(120px, 1fr))", gap: 10 }}>
              <Stat theme={theme} label="Cases" value={total.toLocaleString()} />
              <Stat theme={theme} label="Success rate" value={`${successRate.toFixed(successRate === 100 ? 0 : 1)}%`} tone={errors ? "warning" : "success"} />
              <Stat theme={theme} label="Throughput" value={`${Math.round(tps)} TPS`} />
              <Stat theme={theme} label="p50" value={`${Math.round(pct(latencies, 0.5))}ms`} />
              <Stat theme={theme} label="p95" value={`${Math.round(pct(latencies, 0.95))}ms`} />
              <Stat theme={theme} label="p99" value={`${Math.round(pct(latencies, 0.99))}ms`} />
            </div>
          ) : null}

          <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr", gap: 16, alignItems: "start" }}>
            {/* distribution + case list */}
            <div style={{ ...card, padding: 16, minHeight: 200 }}>
              <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>Cases {total ? `· ${total}` : ""}</h3>
              {!rows ? (
                <div style={{ color: theme.muted, fontSize: 13, padding: "40px 0", textAlign: "center" }}>Generate a dataset and run a simulation.</div>
              ) : (
                <>
                  <div style={{ display: "grid", gap: 8, marginBottom: 16 }}>
                    {["approve", "review", "reject"].map((o) => {
                      const n = dist[o] ?? 0;
                      const p = total ? Math.round((n / total) * 100) : 0;
                      return (
                        <div key={o}>
                          <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 4 }}>
                            <span style={{ textTransform: "capitalize", fontWeight: 600 }}>{o}</span>
                            <span style={{ color: theme.muted, fontFamily: "var(--font-mono)" }}>{n} · {p}%</span>
                          </div>
                          <div style={{ height: 8, borderRadius: 4, background: theme.hover, overflow: "hidden" }}>
                            <div style={{ height: "100%", width: p + "%", background: theme[OUTCOME_TONE[o]], borderRadius: 4 }} />
                          </div>
                        </div>
                      );
                    })}
                  </div>
                  <div style={{ maxHeight: 320, overflowY: "auto", border: "1px solid " + theme.border, borderRadius: 8 }}>
                    {rows.map((r) => {
                      const color = theme[OUTCOME_TONE[r.result.outcome] ?? "muted"] ?? theme.muted;
                      const cust = (parsedCases[r.index] as { customer_id?: string } | undefined)?.customer_id;
                      return (
                        <div key={r.index} onClick={() => reviewCase(r.index)}
                          style={{ display: "flex", justifyContent: "space-between", gap: 8, padding: "8px 12px", borderBottom: "1px solid " + theme.border, fontSize: 12.5, cursor: "pointer", background: selected === r.index ? theme.hover : "transparent" }}>
                          <span style={{ color: theme.muted, fontFamily: "var(--font-mono)", flex: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{cust ?? `#${r.index}`}</span>
                          <span style={{ color, fontWeight: 700, textTransform: "uppercase" }}>{r.result.outcome}</span>
                          <span style={{ color: theme.muted, width: 54, textAlign: "right" }}>{r.result.latency_ms ?? "—"}ms</span>
                        </div>
                      );
                    })}
                  </div>
                </>
              )}
            </div>

            {/* individual case review */}
            <div style={{ ...card, padding: 16, minHeight: 200 }}>
              <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>Case review</h3>
              {selected === null ? (
                <div style={{ color: theme.muted, fontSize: 13, padding: "40px 0", textAlign: "center" }}>Click a case to inspect its inputs and full decision.</div>
              ) : (
                <div style={{ display: "grid", gap: 12 }}>
                  <div>
                    <div style={labelStyle(theme)}>Inputs</div>
                    <pre style={preStyle(theme)}>{JSON.stringify(parsedCases[selected], null, 2)}</pre>
                  </div>
                  <div>
                    <div style={labelStyle(theme)}>Decision {detailBusy ? "· loading…" : ""}</div>
                    {detail ? (
                      <div>
                        <div style={{ display: "flex", gap: 12, alignItems: "center", margin: "6px 0 8px" }}>
                          <span style={{ fontWeight: 700, textTransform: "uppercase", color: theme[OUTCOME_TONE[detail.outcome ?? ""] ?? "muted"] ?? theme.text }}>{detail.outcome ?? "—"}</span>
                          {typeof detail.score === "number" ? <span style={{ fontSize: 12, color: theme.muted }}>score {detail.score}</span> : null}
                          {detail.trace_id ? <span style={{ fontSize: 11, color: theme.dim, fontFamily: "var(--font-mono)" }}>{String(detail.trace_id).slice(0, 12)}</span> : null}
                        </div>
                        <pre style={preStyle(theme)}>{JSON.stringify(detail.explanation ?? detail, null, 2).slice(0, 4000)}</pre>
                      </div>
                    ) : <div style={{ color: theme.dim, fontSize: 12, padding: "8px 0" }}>{detailBusy ? "Fetching full decision…" : "—"}</div>}
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

function Stat({ theme, label, value, tone }: { theme: ThemeTokens; label: string; value: string; tone?: "success" | "warning" }) {
  return (
    <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 10, padding: "12px 14px" }}>
      <div style={{ fontSize: 11, color: theme.dim, textTransform: "uppercase", letterSpacing: "0.04em" }}>{label}</div>
      <div style={{ fontSize: 20, fontWeight: 700, marginTop: 4, color: tone ? theme[tone] : theme.text }}>{value}</div>
    </div>
  );
}

function labelStyle(theme: ThemeTokens): React.CSSProperties {
  return { fontSize: 12, fontWeight: 600, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.04em" };
}
function fieldStyle(theme: ThemeTokens): React.CSSProperties {
  return { width: "100%", padding: "8px 10px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.cardAlt, color: theme.text, boxSizing: "border-box" };
}
function preStyle(theme: ThemeTokens): React.CSSProperties {
  return { margin: "6px 0 0", padding: 10, background: theme.editor, border: "1px solid " + theme.border, borderRadius: 8, fontFamily: "var(--font-mono)", fontSize: 11.5, color: theme.codeText, whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 220, overflow: "auto" };
}
function ctaStyle(theme: ThemeTokens, disabled: boolean): React.CSSProperties {
  return { marginTop: 4, padding: "9px 16px", borderRadius: 8, border: "1px solid " + theme.accent, background: theme.accent, color: theme.inverseText, fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.6 : 1, width: "100%" };
}
function ghostStyle(theme: ThemeTokens): React.CSSProperties {
  return { padding: "8px 14px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.card, color: theme.text, fontWeight: 600, cursor: "pointer", whiteSpace: "nowrap" };
}
