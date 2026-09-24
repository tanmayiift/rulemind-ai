"use client";

import * as React from "react";

import { apiJson } from "../../lib/api";
import { useRuleMindStore } from "../../lib/store";
import { Button,useTheme } from "../kit";
import { Dialog } from "../ui";

/**
 * Release & impact for one policy (plan 6.5): what-if, shadow, releases + rollback, history.
 *
 * Every one of these had a working, tested API and no screen. They are the capabilities that
 * answer a risk team's actual questions before and after a change -- "what happens to MY approval
 * rate if I ship this", "run it on live traffic without acting on it", "put back last Tuesday's
 * version", "what did this policy look like when that decision was made" -- and a prospect could
 * not see any of them.
 */

type Tab = "whatif" | "shadow" | "releases" | "history";
type PolicyOption = { id: string; name: string };

type Kpi = { name: string; type: string; outcome?: string; field?: string; op?: string; value?: number };
type WhatIfResult = {
  policy_id: string;
  bundle_version: number | null;
  mode: string;
  scanned: number;
  kpis: Array<{ name: string; type: string; baseline: number | null; candidate: number | null; delta: number | null }>;
};
type BundleVersion = { version: number; compiled_at?: string; superseded?: boolean };
type ShadowReport = {
  live_policy_id: string;
  active: boolean;
  candidate_policy_id?: string;
  shadow_count: number;
  diverged?: number;
  divergence_rate_pct?: number;
  transitions?: Record<string, number>;
};
type Release = {
  promotion_id: number;
  from_status: string;
  to_status: string;
  promoted_by: string;
  reason?: string | null;
  created_at: string;
};
type HistoryRow = { version: number; created_at: string; snapshot: Record<string, unknown> };

const PRESET_KPIS: Kpi[] = [
  { name: "approval_rate", type: "outcome_rate", outcome: "approve" },
  { name: "rejection_rate", type: "outcome_rate", outcome: "reject" },
  { name: "review_rate", type: "outcome_rate", outcome: "review" },
];
const RATE_TYPES = new Set(["outcome_rate"]);
const MIN_ROLLBACK_REASON = 10;

function fmt(value: number | null | undefined, type: string): string {
  if (value === null || value === undefined) return "—";
  if (RATE_TYPES.has(type)) return (value * 100).toFixed(2) + "%";
  return Number.isInteger(value) ? String(value) : value.toFixed(4);
}

function fmtDelta(value: number | null, type: string): string {
  if (value === null) return "—";
  const sign = value > 0 ? "+" : "";
  return RATE_TYPES.has(type) ? sign + (value * 100).toFixed(2) + " pts" : sign + (Number.isInteger(value) ? value : value.toFixed(4));
}

/** Top-level fields whose JSON differs between two snapshots, with both values. */
export function snapshotChanges(older: Record<string, unknown>, newer: Record<string, unknown>) {
  const keys = Array.from(new Set([...Object.keys(older), ...Object.keys(newer)])).sort();
  return keys
    .filter((key) => !["updated_at", "created_at", "version"].includes(key))
    .filter((key) => JSON.stringify(older[key]) !== JSON.stringify(newer[key]))
    .map((key) => ({ key, before: older[key], after: newer[key] }));
}

export function PolicyLifecycle(props: {
  policyId: string;
  policies: PolicyOption[];
  refresh: () => void;
  onNotify: (message: string) => void;
}) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [tab, setTab] = React.useState<Tab>("whatif");
  const [error, setError] = React.useState<string | null>(null);

  // what-if
  const [selectedKpis, setSelectedKpis] = React.useState<string[]>(["approval_rate", "rejection_rate"]);
  const [customField, setCustomField] = React.useState("");
  const [customType, setCustomType] = React.useState("avg");
  const [bundles, setBundles] = React.useState<BundleVersion[]>([]);
  const [bundleVersion, setBundleVersion] = React.useState<string>("");
  const [sample, setSample] = React.useState(200);
  const [whatIf, setWhatIf] = React.useState<WhatIfResult | null>(null);
  const [running, setRunning] = React.useState(false);

  // shadow / releases / history
  const [shadow, setShadow] = React.useState<ShadowReport | null>(null);
  const [candidate, setCandidate] = React.useState("");
  const [releases, setReleases] = React.useState<Release[] | null>(null);
  const [rollbackTarget, setRollbackTarget] = React.useState<Release | null>(null);
  const [rollbackReason, setRollbackReason] = React.useState("");
  const [rollingBack, setRollingBack] = React.useState(false);
  const [history, setHistory] = React.useState<HistoryRow[] | null>(null);
  const [compare, setCompare] = React.useState<[number, number] | null>(null);

  const call = React.useCallback(
    <T,>(path: string, init: RequestInit = {}) => apiJson<T>(apiBaseUrl, path, init, apiKey),
    [apiBaseUrl, apiKey]
  );
  const base = "/api/v1/policies/" + encodeURIComponent(props.policyId);

  const loadTab = React.useCallback(async (which: Tab) => {
    setError(null);
    try {
      if (which === "whatif") {
        const versions = await call<BundleVersion[]>("/api/v1/bundles/versions");
        setBundles(versions);
      } else if (which === "shadow") {
        setShadow(await call<ShadowReport>(base + "/shadow"));
      } else if (which === "releases") {
        setReleases((await call<{ releases: Release[] }>(base + "/releases")).releases);
      } else {
        const rows = await call<HistoryRow[]>("/api/v1/history/policy/" + encodeURIComponent(props.policyId));
        setHistory(rows);
        setCompare(rows.length >= 2 ? [rows[1].version, rows[0].version] : null);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [base, call, props.policyId]);

  React.useEffect(() => {
    setWhatIf(null); setShadow(null); setReleases(null); setHistory(null);
    void loadTab(tab);
  }, [props.policyId, tab, loadTab]);

  const runWhatIf = async () => {
    const kpis: Kpi[] = PRESET_KPIS.filter((k) => selectedKpis.includes(k.name));
    if (customField.trim()) kpis.push({ name: customType + "_" + customField.trim(), type: customType, field: customField.trim() });
    if (!kpis.length) { setError("Choose at least one KPI."); return; }
    setRunning(true); setError(null);
    try {
      setWhatIf(await call<WhatIfResult>(base + "/whatif", {
        method: "POST",
        body: JSON.stringify({ kpis, sample, bundle_version: bundleVersion ? Number(bundleVersion) : null }),
      }));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRunning(false);
    }
  };

  const startShadow = async () => {
    if (!candidate) return;
    try {
      await call(base + "/shadow", { method: "POST", body: JSON.stringify({ candidate_policy_id: candidate }) });
      props.onNotify("Shadowing " + candidate + " on this policy's live traffic.");
      await loadTab("shadow");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const stopShadow = async () => {
    try {
      await call(base + "/shadow", { method: "DELETE" });
      props.onNotify("Shadow stopped.");
      await loadTab("shadow");
    } catch (e) { setError(e instanceof Error ? e.message : String(e)); }
  };

  const confirmRollback = async () => {
    if (!rollbackTarget || rollbackReason.trim().length < MIN_ROLLBACK_REASON) return;
    setRollingBack(true);
    try {
      // No actor field: the server records the authenticated caller.
      await call(base + "/rollback", {
        method: "POST",
        body: JSON.stringify({ promotion_id: rollbackTarget.promotion_id, reason: rollbackReason.trim() }),
      });
      setRollbackTarget(null);
      props.onNotify("Rolled back to release #" + rollbackTarget.promotion_id + ".");
      props.refresh();
      await loadTab("releases");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setRollingBack(false);
    }
  };

  // The page's own control look (kit InlineInput/InlineSelect use the same tokens).
  const control: React.CSSProperties = { height: 32, borderRadius: 8, border: "1px solid " + theme.border, padding: "0 8px",
                                         background: theme.card, color: theme.text, font: "inherit", fontSize: 13 };
  const cell: React.CSSProperties = { padding: "6px 10px", borderTop: "1px solid " + theme.border, textAlign: "left" };
  const num: React.CSSProperties = { ...cell, textAlign: "right", fontVariantNumeric: "tabular-nums" };
  const tabs: Array<[Tab, string]> = [["whatif", "What-if"], ["shadow", "Shadow"], ["releases", "Releases"], ["history", "History"]];
  const olderRow = history?.find((row) => row.version === compare?.[0]);
  const newerRow = history?.find((row) => row.version === compare?.[1]);
  const changes = olderRow && newerRow ? snapshotChanges(olderRow.snapshot, newerRow.snapshot) : [];

  return (
    <section aria-labelledby="lifecycle-title" data-testid="policy-lifecycle"
             style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 16, display: "grid", gap: 12 }}>
      <h2 id="lifecycle-title" style={{ margin: 0, fontSize: 15, fontWeight: 700, color: theme.text }}>Release &amp; impact</h2>
      <div role="tablist" aria-label="Release and impact" style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        {tabs.map(([id, label]) => (
          <button key={id} role="tab" aria-selected={tab === id} id={"lifecycle-tab-" + id} aria-controls="lifecycle-panel"
                  onClick={() => setTab(id)}
                  style={{ padding: "6px 12px", borderRadius: 8, cursor: "pointer", font: "inherit", fontSize: 13,
                           border: "1px solid " + (tab === id ? theme.accent : theme.border),
                           background: tab === id ? theme.accentBg : "transparent", color: tab === id ? theme.accent : theme.muted }}>
            {label}
          </button>
        ))}
      </div>
      {error ? <div role="alert" style={{ color: theme.danger, fontSize: 13 }}>{error}</div> : null}

      <div role="tabpanel" id="lifecycle-panel" aria-labelledby={"lifecycle-tab-" + tab} style={{ fontSize: 13, color: theme.text }}>
        {tab === "whatif" ? (
          <div style={{ display: "grid", gap: 10 }}>
            <div style={{ color: theme.muted }}>
              Replays this policy&apos;s recorded decisions through a compiled bundle and compares your KPIs: what was decided vs what would be decided now.
            </div>
            <fieldset style={{ border: "none", padding: 0, margin: 0, display: "flex", gap: 14, flexWrap: "wrap" }}>
              <legend style={{ fontWeight: 600, marginBottom: 4 }}>KPIs</legend>
              {PRESET_KPIS.map((kpi) => (
                <label key={kpi.name} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  <input type="checkbox" checked={selectedKpis.includes(kpi.name)}
                         onChange={(e) => setSelectedKpis((prev) => e.target.checked ? [...prev, kpi.name] : prev.filter((k) => k !== kpi.name))} />
                  {kpi.name.replace("_", " ")}
                </label>
              ))}
            </fieldset>
            <div style={{ display: "flex", gap: 10, flexWrap: "wrap", alignItems: "end" }}>
              <label style={{ display: "grid", gap: 4 }}>
                <span>Also measure</span>
                <select style={control} value={customType} onChange={(e) => setCustomType(e.target.value)} aria-label="Custom KPI aggregate">
                  {["avg", "sum", "min", "max"].map((t) => <option key={t} value={t}>{t}</option>)}
                </select>
              </label>
              <label style={{ display: "grid", gap: 4 }}>
                <span>of variable</span>
                <input style={control} value={customField} onChange={(e) => setCustomField(e.target.value)} placeholder="e.g. bureau_score" />
              </label>
              <label style={{ display: "grid", gap: 4 }}>
                <span>Bundle version</span>
                <select style={control} value={bundleVersion} onChange={(e) => setBundleVersion(e.target.value)} data-testid="whatif-bundle">
                  <option value="">Latest</option>
                  {bundles.map((b) => (
                    <option key={b.version} value={b.version}>v{b.version}{b.superseded ? " (superseded)" : ""}</option>
                  ))}
                </select>
              </label>
              <label style={{ display: "grid", gap: 4 }}>
                <span>Recent decisions</span>
                <input type="number" min={1} max={2000} value={sample} onChange={(e) => setSample(Math.max(1, Math.min(2000, Number(e.target.value) || 1)))} style={{ ...control, width: 90 }} />
              </label>
              <Button variant="primary" onClick={() => void runWhatIf()} disabled={running} testId="whatif-run">
                {running ? "Replaying…" : "Run what-if"}
              </Button>
            </div>
            {whatIf ? (
              <div data-testid="whatif-result">
                <div style={{ color: theme.muted, marginBottom: 6 }}>
                  {whatIf.scanned} decision(s) replayed through bundle v{whatIf.bundle_version ?? "?"} ({whatIf.mode}).
                </div>
                <table style={{ borderCollapse: "collapse", width: "100%" }}>
                  <thead><tr><th style={cell}>KPI</th><th style={num}>Recorded</th><th style={num}>Would be</th><th style={num}>Change</th></tr></thead>
                  <tbody>
                    {whatIf.kpis.map((k) => (
                      <tr key={k.name}>
                        <td style={cell}>{k.name}</td>
                        <td style={num}>{fmt(k.baseline, k.type)}</td>
                        <td style={num}>{fmt(k.candidate, k.type)}</td>
                        <td style={{ ...num, color: k.delta ? (k.delta > 0 ? theme.success : theme.danger) : theme.muted }}>{fmtDelta(k.delta, k.type)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        ) : null}

        {tab === "shadow" ? (
          shadow === null ? <div role="status">Loading…</div> : shadow.active ? (
            <div style={{ display: "grid", gap: 8 }} data-testid="shadow-active">
              <div>
                Shadowing <strong>{shadow.candidate_policy_id}</strong> on live traffic: {shadow.shadow_count} decision(s) logged,{" "}
                <strong>{shadow.divergence_rate_pct ?? 0}%</strong> would have decided differently. The live decision is never changed.
              </div>
              {shadow.transitions && Object.keys(shadow.transitions).length ? (
                <table style={{ borderCollapse: "collapse" }}>
                  <thead><tr><th style={cell}>live → candidate</th><th style={num}>decisions</th></tr></thead>
                  <tbody>
                    {Object.entries(shadow.transitions).map(([key, count]) => (
                      <tr key={key}><td style={cell}>{key.replace("->", " → ")}</td><td style={num}>{count}</td></tr>
                    ))}
                  </tbody>
                </table>
              ) : null}
              <div><Button variant="danger" onClick={() => void stopShadow()} testId="shadow-stop">Stop shadow</Button></div>
            </div>
          ) : (
            <div style={{ display: "flex", gap: 10, alignItems: "end", flexWrap: "wrap" }}>
              <label style={{ display: "grid", gap: 4 }}>
                <span>Run a candidate policy alongside this one, logging what it would decide</span>
                <select style={control} value={candidate} onChange={(e) => setCandidate(e.target.value)} data-testid="shadow-candidate">
                  <option value="">Choose a candidate…</option>
                  {props.policies.filter((p) => p.id !== props.policyId).map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </select>
              </label>
              <Button variant="primary" onClick={() => void startShadow()} disabled={!candidate} testId="shadow-start">Start shadow</Button>
            </div>
          )
        ) : null}

        {tab === "releases" ? (
          releases === null ? <div role="status">Loading…</div> : releases.length === 0 ? (
            <div style={{ color: theme.muted }}>No releases yet — a release is recorded each time this policy is promoted.</div>
          ) : (
            <table style={{ borderCollapse: "collapse", width: "100%" }} data-testid="release-table">
              <thead><tr><th style={cell}>#</th><th style={cell}>Change</th><th style={cell}>By</th><th style={cell}>Reason</th><th style={cell}>When</th><th style={cell} /></tr></thead>
              <tbody>
                {releases.map((r) => (
                  <tr key={r.promotion_id}>
                    <td style={cell}>{r.promotion_id}</td>
                    <td style={cell}>{r.from_status.toUpperCase()} → {r.to_status.toUpperCase()}</td>
                    <td style={cell}>{r.promoted_by}</td>
                    <td style={cell}>{r.reason || "—"}</td>
                    <td style={cell}>{new Date(r.created_at).toLocaleString()}</td>
                    <td style={cell}>
                      <Button small variant="default" onClick={() => { setRollbackReason(""); setRollbackTarget(r); }} testId={"rollback-" + r.promotion_id}>
                        Roll back to this
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )
        ) : null}

        {tab === "history" ? (
          history === null ? <div role="status">Loading…</div> : history.length < 2 ? (
            <div style={{ color: theme.muted }}>{history.length ? "Only one version so far." : "No versions recorded."}</div>
          ) : (
            <div style={{ display: "grid", gap: 8 }} data-testid="history-compare">
              <div style={{ display: "flex", gap: 10, alignItems: "end", flexWrap: "wrap" }}>
                {(["Compare version", "with version"] as const).map((label, index) => (
                  <label key={label} style={{ display: "grid", gap: 4 }}>
                    <span>{label}</span>
                    <select style={control} value={compare?.[index] ?? ""} onChange={(e) => {
                      const next: [number, number] = [...(compare ?? [0, 0])] as [number, number];
                      next[index] = Number(e.target.value);
                      setCompare(next);
                    }}>
                      {history.map((row) => (
                        <option key={row.version} value={row.version}>v{row.version} · {new Date(row.created_at).toLocaleString()}</option>
                      ))}
                    </select>
                  </label>
                ))}
              </div>
              {changes.length === 0 ? <div style={{ color: theme.muted }}>No differences.</div> : (
                <table style={{ borderCollapse: "collapse", width: "100%" }}>
                  <thead><tr><th style={cell}>Field</th><th style={cell}>v{compare?.[0]}</th><th style={cell}>v{compare?.[1]}</th></tr></thead>
                  <tbody>
                    {changes.map((c) => (
                      <tr key={c.key}>
                        <td style={{ ...cell, fontWeight: 600 }}>{c.key}</td>
                        <td style={cell}><pre style={{ margin: 0, whiteSpace: "pre-wrap", maxHeight: 180, overflow: "auto", fontSize: 12 }}>{JSON.stringify(c.before, null, 1)}</pre></td>
                        <td style={cell}><pre style={{ margin: 0, whiteSpace: "pre-wrap", maxHeight: 180, overflow: "auto", fontSize: 12 }}>{JSON.stringify(c.after, null, 1)}</pre></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          )
        ) : null}
      </div>

      <Dialog
        open={rollbackTarget !== null}
        title={rollbackTarget ? "Roll back to release #" + rollbackTarget.promotion_id : ""}
        onClose={() => setRollbackTarget(null)}
        busy={rollingBack}
        footer={
          <>
            <Button variant="ghost" onClick={() => setRollbackTarget(null)} disabled={rollingBack}>Cancel</Button>
            <Button variant="danger" testId="confirm-rollback" onClick={() => void confirmRollback()}
                    disabled={rollingBack || rollbackReason.trim().length < MIN_ROLLBACK_REASON}>
              {rollingBack ? "Rolling back…" : "Roll back"}
            </Button>
          </>
        }
      >
        {rollbackTarget ? (
          <>
            <div>
              Restores this policy and the rules, scorecards and decision tables it references to the snapshot taken when release
              #{rollbackTarget.promotion_id} ({rollbackTarget.from_status.toUpperCase()} → {rollbackTarget.to_status.toUpperCase()}) was promoted.
              It is recorded as a new rollback entry; history is never rewritten.
            </div>
            <label style={{ display: "grid", gap: 6 }}>
              <span style={{ fontWeight: 600 }}>Why are you rolling back? (recorded)</span>
              <textarea value={rollbackReason} onChange={(e) => setRollbackReason(e.target.value)} rows={3} data-testid="rollback-reason"
                        style={{ width: "100%", borderRadius: 8, border: "1px solid " + theme.border, padding: 8, background: theme.card, color: theme.text, font: "inherit" }} />
              <span style={{ fontSize: 12, color: theme.dim }}>At least {MIN_ROLLBACK_REASON} characters.</span>
            </label>
          </>
        ) : null}
      </Dialog>
    </section>
  );
}
