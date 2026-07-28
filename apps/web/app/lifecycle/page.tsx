"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES } from "../../src/v3/theme";

type Policy = { id: string; name: string; status?: string; lifecycle_status?: string };
type Lifecycle = { stage: string; label: string; allowedTransitions: string[]; stages: Array<{ id: string; label: string }> };

const STAGE_TONE: Record<string, "muted" | "warning" | "accent" | "success" | "danger"> = {
  draft: "muted",
  in_review: "warning",
  ready: "accent",
  live: "success",
  rejected: "danger",
  archived: "muted",
};
const STAGE_LABELS: Record<string, string> = {
  draft: "Draft",
  in_review: "In Review",
  ready: "Ready to Deploy",
  live: "Live",
  rejected: "Rejected",
  archived: "Archived",
};

export default function LifecyclePage() {
  const { apiBaseUrl, apiKey, themeMode, isMobile } = useRuleMindStore();
  const theme = THEMES[themeMode];
  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [lifecycle, setLifecycle] = React.useState<Lifecycle | null>(null);
  const [selected, setSelected] = React.useState<Policy | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [notice, setNotice] = React.useState<string | null>(null);

  const loadPolicies = React.useCallback(async () => {
    try {
      const p = await apiJson<Policy[]>(apiBaseUrl, "/api/v1/policies", {}, apiKey);
      setPolicies(p);
      setSelected((cur) => cur ?? p[0] ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load policies.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => {
    void loadPolicies();
  }, [loadPolicies]);

  React.useEffect(() => {
    if (!selected) return;
    let active = true;
    (async () => {
      try {
        const lc = await apiJson<Lifecycle>(apiBaseUrl, `/api/v1/policies/${selected.id}/lifecycle`, {}, apiKey);
        if (active) setLifecycle(lc);
      } catch {
        if (active) setLifecycle(null);
      }
    })();
    return () => {
      active = false;
    };
  }, [selected, apiBaseUrl, apiKey]);

  const transition = React.useCallback(
    async (target: string) => {
      if (!selected) return;
      setBusy(true);
      setError(null);
      try {
        const lc = await apiJson<Lifecycle>(
          apiBaseUrl,
          `/api/v1/policies/${selected.id}/lifecycle`,
          { method: "POST", body: JSON.stringify({ target, actor: "web" }) },
          apiKey
        );
        setLifecycle(lc);
        setNotice(`Moved “${selected.name}” to ${STAGE_LABELS[target] ?? target}`);
        setTimeout(() => setNotice(null), 2500);
        await loadPolicies();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Transition blocked.");
      } finally {
        setBusy(false);
      }
    },
    [apiBaseUrl, apiKey, selected, loadPolicies]
  );

  const card: React.CSSProperties = { background: theme.card, border: "1px solid " + theme.border, borderRadius: 12 };
  const stageBadge = (stage?: string) => {
    const s = stage ?? "draft";
    const color = theme[STAGE_TONE[s] === "muted" ? "muted" : STAGE_TONE[s]] ?? theme.muted;
    return (
      <span style={{ display: "inline-flex", alignItems: "center", gap: 5, padding: "2px 9px", borderRadius: 999, fontSize: 11.5, fontWeight: 700, color, background: color + "1e" }}>
        {STAGE_LABELS[s] ?? s}
      </span>
    );
  };
  const stageOrder = ["draft", "in_review", "ready", "live"];

  return (
    <div style={{ padding: isMobile ? 16 : 24, color: theme.text }}>
      <h1 style={{ fontSize: 22, letterSpacing: "-0.02em", margin: 0 }}>Policy lifecycle</h1>
      <p style={{ color: theme.muted, margin: "4px 0 20px", fontSize: 13.5 }}>Govern each policy through Draft → In Review → Ready → Live, alongside its environment.</p>
      {notice ? <div style={{ ...card, padding: "10px 14px", marginBottom: 14, color: theme.success, fontSize: 13 }}>{notice}</div> : null}
      {error ? <div style={{ ...card, padding: "10px 14px", marginBottom: 14, color: theme.danger, fontSize: 13 }}>{error}</div> : null}

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 340px", gap: 16, alignItems: "start" }}>
        <div style={{ ...card, overflow: "hidden" }}>
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr>
                {["Policy", "Lifecycle stage", "Environment"].map((h) => (
                  <th key={h} style={{ textAlign: "left", fontSize: 11, textTransform: "uppercase", letterSpacing: "0.05em", color: theme.muted, fontWeight: 600, padding: "10px 14px", borderBottom: "1px solid " + theme.border }}>{h}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {policies.map((p) => (
                <tr key={p.id} onClick={() => setSelected(p)} style={{ cursor: "pointer", background: selected?.id === p.id ? theme.hover : "transparent" }}>
                  <td style={{ padding: "11px 14px", borderBottom: "1px solid " + theme.border, fontSize: 13, fontWeight: 600 }}>{p.name}</td>
                  <td style={{ padding: "11px 14px", borderBottom: "1px solid " + theme.border }}>{stageBadge(p.lifecycle_status)}</td>
                  <td style={{ padding: "11px 14px", borderBottom: "1px solid " + theme.border, fontSize: 12.5, color: theme.muted, textTransform: "uppercase" }}>{p.status ?? "dev"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>

        {selected ? (
          <div style={{ ...card, padding: 18, position: isMobile ? "static" : "sticky", top: 16 }}>
            <h3 style={{ fontSize: 15, margin: "0 0 4px" }}>{selected.name}</h3>
            <div style={{ marginBottom: 16 }}>{stageBadge(lifecycle?.stage ?? selected.lifecycle_status)}</div>

            <div style={{ display: "flex", alignItems: "center", gap: 4, marginBottom: 18, flexWrap: "wrap" }}>
              {stageOrder.map((s, i) => {
                const current = (lifecycle?.stage ?? selected.lifecycle_status ?? "draft");
                const active = stageOrder.indexOf(current) >= i;
                return (
                  <React.Fragment key={s}>
                    <div style={{ textAlign: "center" }}>
                      <div style={{ width: 12, height: 12, borderRadius: "50%", background: active ? theme.accent : theme.border, margin: "0 auto" }} />
                      <div style={{ fontSize: 10, color: active ? theme.text : theme.muted, marginTop: 4 }}>{STAGE_LABELS[s]}</div>
                    </div>
                    {i < stageOrder.length - 1 ? <div style={{ flex: 1, height: 2, background: stageOrder.indexOf(current) > i ? theme.accent : theme.border }} /> : null}
                  </React.Fragment>
                );
              })}
            </div>

            <label style={{ fontSize: 12, fontWeight: 600, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.04em" }}>Move to</label>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap", marginTop: 8 }}>
              {(lifecycle?.allowedTransitions ?? []).length === 0 ? (
                <span style={{ color: theme.muted, fontSize: 13 }}>No further transitions.</span>
              ) : (
                (lifecycle?.allowedTransitions ?? []).map((t) => (
                  <button key={t} onClick={() => transition(t)} disabled={busy} style={{ padding: "8px 14px", borderRadius: 8, border: "1px solid " + (t === "rejected" ? theme.danger : theme.accent), background: t === "rejected" ? "transparent" : theme.accent, color: t === "rejected" ? theme.danger : "#fff", fontWeight: 600, fontSize: 13, cursor: busy ? "wait" : "pointer" }}>
                    {STAGE_LABELS[t] ?? t}
                  </button>
                ))
              )}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}
