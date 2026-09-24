"use client";

import * as React from "react";
import Link from "next/link";
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

// Adverse-action reason codes come from the SERVER, derived from the conditions that actually
// failed on this decision -- GET /api/v1/decisions/{id}/reason-codes, which calls
// logic.adverse_reason_codes() and ranks failing conditions by relative miss magnitude.
//
// This function used to be:
//
//   if (decision.outcome === "reject")
//     return ["AA05 · Debt-to-income too high", "AA12 · Insufficient credit history"];
//
// -- the same two codes for every rejected decision, regardless of which rules fired, rendered
// under a heading reading "Adverse-action reason codes". These are the codes a lender puts in a
// FCRA-style decline notice. Inventing them is not a placeholder; it is a compliance
// fabrication, and the real endpoint had existed and been called by nothing.
type ReasonCode = {
  code: string;
  variable_name?: string;
  variable_id?: string;
  operator?: string;
  threshold?: unknown;
  value?: unknown;
  reason: string;
};

export default function DecisionExplorerPage() {
  const { apiBaseUrl, apiKey, themeMode, isMobile } = useRuleMindStore();
  const theme = THEMES[themeMode];
  const [decisions, setDecisions] = React.useState<Decision[]>([]);
  const [selected, setSelected] = React.useState<Decision | null>(null);
  const [query, setQuery] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [explain, setExplain] = React.useState<{ summary?: string; reason_codes?: string[]; error?: string } | null>(null);
  const [explaining, setExplaining] = React.useState(false);
  // AI-powered actions only surface when the tenant has enabled AI (key present).
  const [aiEnabled, setAiEnabled] = React.useState(false);

  React.useEffect(() => {
    let active = true;
    apiJson<{ enabled?: boolean }>(apiBaseUrl, "/api/v1/ai/config", {}, apiKey)
      .then((c) => { if (active) setAiEnabled(Boolean(c.enabled)); })
      .catch(() => { if (active) setAiEnabled(false); });
    return () => { active = false; };
  }, [apiBaseUrl, apiKey]);

  // Fetched per selected decision. Deliberately NOT derived from `selected.outcome`: the whole
  // point is that these come from the conditions that failed on THIS decision.
  const [reasonCodes, setReasonCodes] = React.useState<ReasonCode[] | null>(null);
  const [reasonError, setReasonError] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!selected) { setReasonCodes(null); setReasonError(null); return; }
    let active = true;
    setReasonCodes(null); setReasonError(null);
    (async () => {
      try {
        const r = await apiJson<{ reason_codes?: ReasonCode[] }>(
          apiBaseUrl, `/api/v1/decisions/${encodeURIComponent(selected.id)}/reason-codes`, {}, apiKey);
        if (active) setReasonCodes(r.reason_codes ?? []);
      } catch (e) {
        // Surfaced rather than swallowed: an empty list and a failed fetch mean different
        // things, and only one of them is "this decision declined for no stated reason".
        if (active) setReasonError(e instanceof Error ? e.message : "Could not load reason codes");
      }
    })();
    return () => { active = false; };
  }, [selected, apiBaseUrl, apiKey]);

  const runExplain = React.useCallback(async (id: string) => {
    setExplaining(true); setExplain(null);
    try {
      const r = await apiJson<{ summary?: string; reason_codes?: string[] }>(apiBaseUrl, "/api/v1/ai/explain-decision", { method: "POST", body: JSON.stringify({ decision_id: id }) }, apiKey);
      setExplain(r);
    } catch (e) {
      setExplain({ error: e instanceof Error ? e.message : "Explain failed — add an AI key in the AI Copilot page." });
    } finally { setExplaining(false); }
  }, [apiBaseUrl, apiKey]);

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
          aria-label="Search decisions" placeholder="Search by decision id, policy, or outcome…"
          style={{ flex: 1, border: 0, background: "transparent", color: theme.text, fontSize: 14, outline: "none", fontFamily: "var(--font-mono)" }}
        />
        <span style={{ color: theme.muted, fontSize: 12 }}>{filtered.length} of {decisions.length}</span>
      </div>

      {error ? <div style={{ ...card, padding: 14, color: theme.danger, marginBottom: 16 }}>{error}</div> : null}
      {loading ? <div style={{ ...card, padding: 40, textAlign: "center", color: theme.muted }}>Loading decisions…</div> : null}
      {!loading && decisions.length === 0 ? (
        <div style={{ ...card, padding: 48, textAlign: "center", color: theme.muted }}>
          <div style={{ marginBottom: 14 }}>No decisions logged yet.</div>
          {/* The old copy named the Test Console and left the user to find it. An empty state
              that tells you where to go should take you there -- otherwise it is a dead end
              wearing the costume of guidance. */}
          <Link
            href="/test-console"
            style={{
              display: "inline-block", padding: "9px 16px", borderRadius: 8,
              border: "1px solid " + theme.accent, background: theme.accentBg,
              color: theme.accent, textDecoration: "none",
              fontSize: "var(--rm-fs-body)", fontWeight: 600,
            }}
          >
            Run your first decision
          </Link>
        </div>
      ) : null}

      {!loading && decisions.length > 0 ? (
        <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1.3fr 1fr", gap: 16, alignItems: "start" }}>
          {/* overflow-x must be auto, not hidden: the table needs ~429px and the
              column is ~341px on a 375px screen, so `hidden` silently clipped the
              Latency column with no way to scroll to it. Wide content scrolls inside
              its own container; the page itself still must not scroll sideways. */}
          <div style={{ ...card, overflowY: "hidden", overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", minWidth: 420 }}>
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
              <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 14, flexWrap: "wrap" }}>
                {pill(selected.outcome)}
                <span style={{ fontFamily: "var(--font-mono)", fontSize: 12.5, color: theme.muted, flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis" }}>{selected.id}</span>
                {aiEnabled ? (
                  <button onClick={() => runExplain(selected.id)} disabled={explaining}
                    style={{ border: "1px solid " + theme.border, background: theme.accentBg, color: theme.accent, borderRadius: 8, padding: "6px 11px", fontSize: 12, fontWeight: 600, cursor: "pointer", display: "inline-flex", alignItems: "center", gap: 6 }}>
                    ✦ {explaining ? "Explaining…" : "Explain (AI)"}
                  </button>
                ) : null}
                {/* The other half of the parity P04 flow: this pane explains what HAPPENED, and
                    for a decision you disagree with the next question is always "where did it go
                    wrong" -- which needs the run stopped at each step, not a finished trace. The
                    debugger opens with this decision's own payload already loaded. */}
                <Link
                  href={{ pathname: "/debugger", query: { decision: selected.id } }}
                  data-testid="step-through-decision"
                  style={{ border: "1px solid " + theme.border, background: "transparent", color: theme.text, borderRadius: 8, padding: "6px 11px", fontSize: 12, fontWeight: 600, textDecoration: "none", display: "inline-flex", alignItems: "center", gap: 6 }}
                >
                  ⏯ Step through
                </Link>
              </div>
              {explain ? (
                <div style={{ marginBottom: 14, padding: 12, borderRadius: 10, background: theme.accentBg, border: "1px solid " + theme.border }}>
                  {explain.error ? (
                    <div style={{ fontSize: 12.5, color: theme.danger }}>{explain.error}</div>
                  ) : (
                    <>
                      <div style={{ fontSize: 13, color: theme.text, lineHeight: 1.5 }}>{explain.summary}</div>
                      {explain.reason_codes && explain.reason_codes.length ? (
                        <div style={{ marginTop: 8, display: "flex", flexWrap: "wrap", gap: 6 }}>
                          {explain.reason_codes.map((rc, i) => (
                            <span key={i} style={{ fontSize: 11, fontWeight: 600, color: theme.warning, background: theme.warningBg, borderRadius: 999, padding: "3px 9px" }}>{rc}</span>
                          ))}
                        </div>
                      ) : null}
                    </>
                  )}
                </div>
              ) : null}
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
              {reasonError ? (
                <>
                  <h3 style={{ fontSize: 13, margin: "14px 0 8px" }}>Adverse-action reason codes</h3>
                  <div style={{ padding: "9px 11px", border: "1px solid " + theme.border, borderRadius: 8, fontSize: 12.5, color: theme.muted }}>
                    {reasonError}
                  </div>
                </>
              ) : reasonCodes && reasonCodes.length ? (
                <>
                  <h3 style={{ fontSize: 13, margin: "14px 0 8px" }}>Adverse-action reason codes</h3>
                  {reasonCodes.map((r) => (
                    <div key={r.code} style={{ display: "flex", gap: 10, alignItems: "baseline", padding: "9px 11px", border: "1px solid " + theme.border, borderRadius: 8, marginBottom: 8 }}>
                      <span style={{ fontFamily: "var(--font-mono)", fontWeight: 700, color: theme.accent, whiteSpace: "nowrap" }}>{r.code}</span>
                      <span style={{ fontSize: 12.5 }}>{r.reason}</span>
                    </div>
                  ))}
                </>
              ) : null}
              <ReplayAgainstBundle decisionId={selected.id} theme={theme} />
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

type ReplayResult = { bundle_version: number | null; original_outcome: string; replayed_outcome: string; changed: boolean };

/**
 * Replay THIS decision through a retained bundle version (plan 6.5). The regulator's question is
 * not "what does the policy say today" but "would this applicant have been decided the same way
 * under the version that was live then -- or under the one live now". The endpoint existed
 * (POST /decisions/{id}/replay?bundleVersion=N) and no screen called it.
 */
function ReplayAgainstBundle(props: { decisionId: string; theme: (typeof THEMES)[keyof typeof THEMES] }) {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const { theme } = props;
  const [versions, setVersions] = React.useState<Array<{ version: number; superseded?: boolean }>>([]);
  const [version, setVersion] = React.useState("");
  const [result, setResult] = React.useState<ReplayResult | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    apiJson<Array<{ version: number; superseded?: boolean }>>(apiBaseUrl, "/api/v1/bundles/versions", {}, apiKey)
      .then(setVersions).catch(() => setVersions([]));
  }, [apiBaseUrl, apiKey]);
  React.useEffect(() => { setResult(null); setError(null); }, [props.decisionId]);

  const replay = async () => {
    setBusy(true); setError(null); setResult(null);
    try {
      const query = version ? "?bundleVersion=" + encodeURIComponent(version) : "";
      setResult(await apiJson<ReplayResult>(apiBaseUrl,
        "/api/v1/decisions/" + encodeURIComponent(props.decisionId) + "/replay" + query, { method: "POST" }, apiKey));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Replay failed.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div data-testid="decision-replay" style={{ marginTop: 14 }}>
      <h3 style={{ fontSize: 13, margin: "0 0 8px" }}>Replay against a bundle version</h3>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <select aria-label="Bundle version" value={version} onChange={(e) => setVersion(e.target.value)}
                style={{ height: 30, borderRadius: 8, border: "1px solid " + theme.border, background: theme.card, color: theme.text, padding: "0 8px" }}>
          <option value="">Latest bundle</option>
          {versions.map((v) => <option key={v.version} value={v.version}>v{v.version}{v.superseded ? " (superseded)" : ""}</option>)}
        </select>
        <button onClick={() => void replay()} disabled={busy}
                style={{ height: 30, padding: "0 12px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.hover, color: theme.text, cursor: "pointer" }}>
          {busy ? "Replaying…" : "Replay"}
        </button>
      </div>
      {error ? <div role="alert" style={{ marginTop: 8, fontSize: 12.5, color: theme.danger }}>{error}</div> : null}
      {result ? (
        <div data-testid="decision-replay-result" style={{ marginTop: 8, fontSize: 12.5 }}>
          Recorded <strong>{result.original_outcome}</strong>; bundle v{result.bundle_version ?? "?"} decides{" "}
          <strong>{result.replayed_outcome}</strong> —{" "}
          <span style={{ color: result.changed ? theme.danger : theme.success, fontWeight: 600 }}>
            {result.changed ? "a different outcome" : "the same outcome"}
          </span>.
        </div>
      ) : null}
    </div>
  );
}
