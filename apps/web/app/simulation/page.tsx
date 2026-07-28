"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES } from "../../src/v3/theme";

type Policy = { id: string; name: string };
type BatchRow = { index: number; result: { outcome: string; latency_ms?: number } };
type BatchResponse = { rows: BatchRow[]; count: number };

const OUTCOME_TONE: Record<string, "success" | "warning" | "danger"> = { approve: "success", review: "warning", reject: "danger" };

export default function SimulationPage() {
  const { apiBaseUrl, apiKey, themeMode, isMobile } = useRuleMindStore();
  const theme = THEMES[themeMode];
  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [policyId, setPolicyId] = React.useState("");
  const [payloadText, setPayloadText] = React.useState('[\n  { "user_id": "sim-1" },\n  { "user_id": "sim-2" }\n]');
  const [rows, setRows] = React.useState<BatchRow[] | null>(null);
  const [running, setRunning] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

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

  const run = React.useCallback(async () => {
    setError(null);
    let payloads: unknown[];
    try {
      payloads = JSON.parse(payloadText);
      if (!Array.isArray(payloads)) throw new Error("Payload must be a JSON array of objects.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Invalid JSON.");
      return;
    }
    setRunning(true);
    try {
      const res = await apiJson<BatchResponse>(
        apiBaseUrl,
        "/api/v1/decide/batch",
        { method: "POST", body: JSON.stringify({ targetType: "decide", targetId: policyId, payloads }) },
        apiKey
      );
      setRows(res.rows);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Simulation failed.");
    } finally {
      setRunning(false);
    }
  }, [apiBaseUrl, apiKey, policyId, payloadText]);

  const dist = (rows ?? []).reduce<Record<string, number>>((acc, r) => {
    const o = r.result.outcome;
    acc[o] = (acc[o] ?? 0) + 1;
    return acc;
  }, {});
  const total = rows?.length ?? 0;
  const card: React.CSSProperties = { background: theme.card, border: "1px solid " + theme.border, borderRadius: 12 };

  return (
    <div style={{ padding: isMobile ? 16 : 24, color: theme.text }}>
      <h1 style={{ fontSize: 22, letterSpacing: "-0.02em", margin: 0 }}>Simulation &amp; backtest</h1>
      <p style={{ color: theme.muted, margin: "4px 0 20px", fontSize: 13.5 }}>Run a policy against a batch of payloads and inspect the outcome distribution.</p>

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr", gap: 16, alignItems: "start" }}>
        <div style={{ ...card, padding: 16 }}>
          <label style={{ fontSize: 12, fontWeight: 600, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.04em" }}>Policy</label>
          <select value={policyId} onChange={(e) => setPolicyId(e.target.value)} style={{ width: "100%", padding: "8px 10px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.cardAlt, color: theme.text, margin: "6px 0 14px" }}>
            {policies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select>
          <label style={{ fontSize: 12, fontWeight: 600, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.04em" }}>Dataset (JSON array of payloads)</label>
          <textarea value={payloadText} onChange={(e) => setPayloadText(e.target.value)} rows={12} style={{ width: "100%", marginTop: 6, padding: 12, borderRadius: 8, border: "1px solid " + theme.border, background: theme.cardAlt, color: theme.text, fontFamily: "var(--font-mono)", fontSize: 12.5, resize: "vertical" }} />
          <button onClick={run} disabled={running || !policyId} style={{ marginTop: 12, padding: "9px 16px", borderRadius: 8, border: "1px solid " + theme.accent, background: theme.accent, color: "#fff", fontWeight: 600, cursor: running ? "wait" : "pointer", opacity: running || !policyId ? 0.6 : 1 }}>
            {running ? "Running…" : "Run simulation"}
          </button>
          {error ? <div style={{ marginTop: 12, color: theme.danger, fontSize: 13 }}>{error}</div> : null}
        </div>

        <div style={{ ...card, padding: 16, minHeight: 200 }}>
          <h3 style={{ fontSize: 15, margin: "0 0 12px" }}>Results {total ? `· ${total} cases` : ""}</h3>
          {!rows ? (
            <div style={{ color: theme.muted, fontSize: 13, padding: "40px 0", textAlign: "center" }}>Run a simulation to see the outcome distribution.</div>
          ) : (
            <>
              <div style={{ display: "grid", gap: 8, marginBottom: 16 }}>
                {["approve", "review", "reject"].map((o) => {
                  const n = dist[o] ?? 0;
                  const pct = total ? Math.round((n / total) * 100) : 0;
                  const color = theme[OUTCOME_TONE[o]];
                  return (
                    <div key={o}>
                      <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, marginBottom: 4 }}>
                        <span style={{ textTransform: "capitalize", fontWeight: 600 }}>{o}</span>
                        <span style={{ color: theme.muted, fontFamily: "var(--font-mono)" }}>{n} · {pct}%</span>
                      </div>
                      <div style={{ height: 8, borderRadius: 4, background: theme.hover, overflow: "hidden" }}>
                        <div style={{ height: "100%", width: pct + "%", background: color, borderRadius: 4 }} />
                      </div>
                    </div>
                  );
                })}
              </div>
              <div style={{ maxHeight: 260, overflowY: "auto", border: "1px solid " + theme.border, borderRadius: 8 }}>
                {rows.map((r) => {
                  const color = theme[OUTCOME_TONE[r.result.outcome] ?? "muted"] ?? theme.muted;
                  return (
                    <div key={r.index} style={{ display: "flex", justifyContent: "space-between", padding: "8px 12px", borderBottom: "1px solid " + theme.border, fontSize: 12.5 }}>
                      <span style={{ color: theme.muted, fontFamily: "var(--font-mono)" }}>#{r.index}</span>
                      <span style={{ color, fontWeight: 700, textTransform: "uppercase" }}>{r.result.outcome}</span>
                      <span style={{ color: theme.muted }}>{r.result.latency_ms ?? "—"}ms</span>
                    </div>
                  );
                })}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
