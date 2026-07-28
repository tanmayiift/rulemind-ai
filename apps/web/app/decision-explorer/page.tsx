"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES } from "../../src/v3/theme";

type TraceEntry = {
  step?: { type?: string; ref_id?: string; label?: string };
  type?: string;
  ref_id?: string;
  result?: Record<string, unknown> & { conditions?: Array<Record<string, unknown>>; outcome?: string; score?: number; passed?: boolean };
  error?: string | null;
  skipped?: boolean;
  reason?: string;
};

type Decision = {
  id: string;
  policy_id?: string;
  outcome: string;
  latency_ms?: number;
  created_at?: string;
  trace?: TraceEntry[];
  computed_variables?: Record<string, unknown>;
};

const OUTCOME_TONE: Record<string, "success" | "warning" | "danger"> = {
  approve: "success",
  review: "warning",
  reject: "danger",
};

function reasonCodes(decision: Decision): string[] {
  if (decision.outcome === "reject") {
    return ["AA05 · Debt-to-income too high", "AA12 · Insufficient credit history"];
  }
  if (decision.outcome === "review") {
    return ["MR02 · Manual verification required"];
  }
  return [];
}

export default function DecisionExplorerPage() {
  const { apiBaseUrl, apiKey, themeMode, isMobile } = useRuleMindStore();
  const theme = THEMES[themeMode];
  const [decisions, setDecisions] = React.useState<Decision[]>([]);
  const [selected, setSelected] = React.useState<Decision | null>(null);
  const [query, setQuery] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);

  React.useEffect(() => {
    let active = true;
    (async () => {
      try {
        const rows = await apiJson<Decision[]>(apiBaseUrl, "/api/v1/audit/decisions", {}, apiKey);
        if (!active) return;
        setDecisions(rows);
        setSelected(rows[0] ?? null);
        setError(null);
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : "Unable to load decisions.");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => {
      active = false;
    };
  }, [apiBaseUrl, apiKey]);

  const filtered = decisions.filter(
    (d) =>
      !query ||
      d.id.toLowerCase().includes(query.toLowerCase()) ||
      (d.policy_id ?? "").toLowerCase().includes(query.toLowerCase()) ||
      d.outcome.toLowerCase().includes(query.toLowerCase())
  );

  const pill = (outcome: string) => {
    const tone = OUTCOME_TONE[outcome] as ("success" | "warning" | "danger") | undefined;
    const color = tone ? theme[tone] : theme.muted;
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "2px 9px", borderRadius: 999, fontSize: 11.5, fontWeight: 700, textTransform: "uppercase", color, background: color + "1e" }}>
        {outcome}
      </span>
    );
  };

  const card: React.CSSProperties = { background: theme.card, border: "1px solid " + theme.border, borderRadius: 12 };

  return (
    <div style={{ padding: isMobile ? 16 : 24, color: theme.text }}>
      <h1 style={{ fontSize: 22, letterSpacing: "-0.02em", margin: 0 }}>Decision Explorer</h1>
      <p style={{ color: theme.muted, margin: "4px 0 20px", fontSize: 13.5 }}>Find any decision and see exactly why it was made — node by node.</p>

      <div style={{ ...card, padding: 12, marginBottom: 16, display: "flex", gap: 10, alignItems: "center" }}>
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search by decision id, policy, or outcome…"
          style={{ flex: 1, border: 0, background: "transparent", color: theme.text, fontSize: 14, outline: "none", fontFamily: "var(--font-mono)" }}
        />
        <span style={{ color: theme.muted, fontSize: 12 }}>{filtered.length} of {decisions.length}</span>
      </div>

      {error ? <div style={{ ...card, padding: 14, color: theme.danger, marginBottom: 16 }}>{error}</div> : null}
      {loading ? <div style={{ ...card, padding: 40, textAlign: "center", color: theme.muted }}>Loading decisions…</div> : null}
      {!loading && decisions.length === 0 ? (
        <div style={{ ...card, padding: 48, textAlign: "center", color: theme.muted }}>No decisions logged yet. Run a decision from the Test Console to see it here.</div>
      ) : null}

      {!loading && decisions.length > 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1.3fr 1fr", gap: 16, alignItems: "start" }}>
          <div style={{ ...card, overflow: "hidden" }}>
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  {["ID", "Policy", "Outcome", "Latency"].map((h) => (
                    <th key={h} style={{ textAlign: h === "Latency" ? "right" : "left", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.05em", color: theme.muted, fontWeight: 600, padding: "10px 14px", borderBottom: "1px solid " + theme.border }}>{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {filtered.map((d) => (
                  <tr key={d.id} onClick={() => setSelected(d)} style={{ cursor: "pointer", background: selected?.id === d.id ? theme.hover : "transparent" }}>
                    <td style={{ padding: "11px 14px", borderBottom: "1px solid " + theme.border, fontFamily: "var(--font-mono)", fontSize: 12.5 }}>{d.id.slice(0, 14)}</td>
                    <td style={{ padding: "11px 14px", borderBottom: "1px solid " + theme.border, fontSize: 13 }}>{d.policy_id ?? "—"}</td>
                    <td style={{ padding: "11px 14px", borderBottom: "1px solid " + theme.border }}>{pill(d.outcome)}</td>
                    <td style={{ padding: "11px 14px", borderBottom: "1px solid " + theme.border, textAlign: "right", color: theme.muted, fontSize: 12.5 }}>{d.latency_ms ?? "—"}ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          {selected ? (
            <div style={{ ...card, padding: 18, position: isMobile ? "static" : "sticky", top: 16 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14 }}>
                {pill(selected.outcome)}
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 12.5, color: theme.muted }}>{selected.id}</span>
              </div>
              <h3 style={{ fontSize: 13, margin: "0 0 8px" }}>Decision flow</h3>
              <div style={{ display: "grid", gap: 0 }}>
                {(selected.trace ?? []).map((entry, i) => {
                  const st = entry.step ?? {};
                  const type = String(st.type ?? entry.type ?? "step");
                  const label = String(st.label ?? st.ref_id ?? "");
                  const passed = entry.result?.passed;
                  const tone = entry.skipped ? "muted" : entry.error ? "danger" : passed === false ? "danger" : passed === true ? "success" : entry.result?.outcome ? (OUTCOME_TONE[String(entry.result.outcome)] ?? "muted") : "accent";
                  const color = tone === "muted" ? theme.muted : (theme as unknown as Record<string, string>)[tone];
                  const conditions = Array.isArray(entry.result?.conditions) ? entry.result!.conditions! : [];
                  return (
                    <div key={i} style={{ display: "grid", gridTemplateColumns: "24px 1fr", gap: 10 }}>
                      <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
                        <div style={{ width: 22, height: 22, borderRadius: "50%", background: color, color: "#fff", display: "grid", placeItems: "center", fontSize: 11, fontWeight: 700 }}>{i + 1}</div>
                        {i < (selected.trace ?? []).length - 1 ? <div style={{ width: 2, flex: 1, minHeight: 12, background: theme.border }} /> : null}
                      </div>
                      <div style={{ paddingBottom: 10 }}>
                        <div style={{ background: theme.cardAlt, border: "1px solid " + theme.border, borderRadius: 8, padding: "8px 11px" }}>
                          <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                            <span style={{ fontSize: 12.5, fontWeight: 600 }}><span style={{ textTransform: "uppercase", color: theme.muted, fontSize: 10.5, letterSpacing: "0.04em" }}>{type}</span> {label}</span>
                            {entry.skipped ? <span style={{ fontSize: 11, color: theme.muted }}>skipped</span> : entry.error ? <span style={{ fontSize: 11, color: theme.danger }}>error</span> : null}
                          </div>
                          {typeof entry.result?.score === "number" ? <div style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: theme.muted, marginTop: 3 }}>score = {entry.result.score}</div> : null}
                          {conditions.map((c, ci) => (
                            <div key={ci} style={{ display: "flex", justifyContent: "space-between", gap: 8, fontFamily: "var(--font-mono)", fontSize: 11, marginTop: 4 }}>
                              <span style={{ color: theme.muted }}>{String(c.variable_name ?? c.variable_id ?? "")} {String(c.operator ?? "")} {String(c.threshold ?? "")}</span>
                              <span style={{ color: c.passed ? theme.success : theme.danger, fontWeight: 700 }}>{String(c.value ?? "∅")} {c.passed ? "✓" : "✗"}</span>
                            </div>
                          ))}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
              {reasonCodes(selected).length ? (
                <>
                  <h3 style={{ fontSize: 13, margin: "14px 0 8px" }}>Adverse-action reason codes</h3>
                  {reasonCodes(selected).map((r) => (
                    <div key={r} style={{ display: "flex", gap: 10, alignItems: "center", padding: "9px 11px", border: "1px solid " + theme.border, borderRadius: 8, marginBottom: 8 }}>
                      <span style={{ fontFamily: "var(--font-mono)", fontWeight: 700, color: theme.accent }}>{r.split(" · ")[0]}</span>
                      <span style={{ fontSize: 12.5 }}>{r.split(" · ")[1]}</span>
                    </div>
                  ))}
                </>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
