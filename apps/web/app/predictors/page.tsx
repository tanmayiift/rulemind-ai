"use client";

/**
 * Predictors — track a number a decision already produces, and watch its population drift.
 *
 * WHY THIS FILE EXISTS. `app/routers/predictors.py` (8 endpoints) shipped with no page: the path
 * appeared only in app-shell.tsx's page-metadata map, with no route and no link anywhere, so the
 * capability was reachable only by calling the API by hand.
 *
 * TWO THINGS ON THIS PAGE ARE EASY TO GET WRONG, so they are stated in the UI rather than left
 * to be inferred:
 *
 *  1. **A new predictor has no history.** `predictors.observe` runs on the decision path and is
 *     forward-only, so with the default 30-day baseline the drift report has nothing to compare
 *     against for a month — on a feature whose entire job is comparison. That is what Backfill is
 *     for: it replays historical decisions, is idempotent by decision id, and stamps each value
 *     with the DECISION's timestamp rather than now. The page surfaces it prominently because a
 *     drift panel reading "insufficient_data" otherwise looks like a bug.
 *
 *  2. **The windows are adjacent, not overlapping.** The baseline ends where the current window
 *     begins. The response echoes both windows, so the panel shows the actual spans rather than
 *     the requested ones — a screenshot of this should be self-describing.
 *
 * PSI classification thresholds come from the backend (`app/drift.py`), not from this file. Two
 * copies of a threshold is how a dashboard ends up disagreeing with the alert that fired.
 */

import * as React from "react";
import { Trash2, Activity, History, TrendingUp } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Badge, EmptyState, PageHeader, SectionTitle, Stat } from "../../src/v3/ui";

type Predictor = {
  id: string;
  name: string;
  source_type: string;
  source_id?: string | null;
  description?: string;
  is_active?: boolean;
  baseline_days?: number;
  current_days?: number;
  bins?: number;
};
type DriftBin = { lower?: number | null; upper?: number | null; baseline?: number; current?: number; contribution?: number };
type DriftReport = {
  predictor_id: string;
  name?: string;
  psi: number | null;
  classification: string;
  bins: DriftBin[];
  baseline_count: number;
  current_count: number;
  baseline_days?: number;
  current_days?: number;
  baseline_window?: [string, string];
  current_window?: [string, string];
};
type BackfillResult = { written: number; scanned: number };

// The backend's own vocabulary (app/drift.py classify()). Mapped to a tone only — never
// re-derived from the psi number here, or this panel and the alert rules could disagree.
function toneFor(classification: string): "success" | "warning" | "danger" | "accent" {
  if (classification === "stable") return "success";
  if (classification === "moderate") return "warning";
  if (classification === "significant") return "danger";
  return "accent";
}

function windowLabel(w?: [string, string]): string {
  if (!w) return "—";
  const fmt = (s: string) => s.replace("T", " ").slice(0, 16);
  return `${fmt(w[0])} → ${fmt(w[1])}`;
}

export default function PredictorsPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [items, setItems] = React.useState<Predictor[]>([]);
  const [sourceTypes, setSourceTypes] = React.useState<string[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<string | null>(null);

  const [name, setName] = React.useState("");
  const [sourceType, setSourceType] = React.useState("");
  const [sourceId, setSourceId] = React.useState("");
  const [baselineDays, setBaselineDays] = React.useState("30");
  const [currentDays, setCurrentDays] = React.useState("7");
  const [bins, setBins] = React.useState("10");
  const [saving, setSaving] = React.useState(false);

  const [openId, setOpenId] = React.useState<string | null>(null);
  const [report, setReport] = React.useState<DriftReport | null>(null);
  const [loadingReport, setLoadingReport] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const [list, types] = await Promise.all([
        apiJson<Predictor[]>(apiBaseUrl, "/api/v1/predictors", {}, apiKey),
        apiJson<{ source_types: string[] }>(apiBaseUrl, "/api/v1/predictors/source-types", {}, apiKey)
      ]);
      setItems(list);
      setSourceTypes(types.source_types || []);
      // Do not hard-code the default: the list is the backend's, and a value this page invented
      // would 422 the moment the set changes.
      setSourceType((prev) => prev || (types.source_types || [])[0] || "");
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load predictors.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void load(); }, [load]);

  // `outcome` needs no source_id; scorecard and variable do, and the API 422s without one.
  const needsSourceId = sourceType === "scorecard" || sourceType === "variable";

  const create = async () => {
    if (!name.trim() || !sourceType) {
      setError("Give the predictor a name and a source type.");
      return;
    }
    if (needsSourceId && !sourceId.trim()) {
      setError(`A ${sourceType} predictor needs a source id — without it there is nothing to observe.`);
      return;
    }
    setSaving(true); setError(null); setStatus(null);
    try {
      await apiJson(apiBaseUrl, "/api/v1/predictors", {
        method: "POST",
        body: JSON.stringify({
          name: name.trim(),
          source_type: sourceType,
          source_id: needsSourceId ? sourceId.trim() : null,
          baseline_days: Number(baselineDays) || 30,
          current_days: Number(currentDays) || 7,
          bins: Number(bins) || 10,
          is_active: true
        })
      }, apiKey);
      setStatus(`Created “${name.trim()}”. It has no history yet — run Backfill to replay past decisions into it.`);
      setName(""); setSourceId("");
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the predictor.");
    } finally {
      setSaving(false);
    }
  };

  const del = async (id: string) => {
    try {
      await apiJson(apiBaseUrl, `/api/v1/predictors/${id}`, { method: "DELETE" }, apiKey);
      if (openId === id) { setOpenId(null); setReport(null); }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed.");
    }
  };

  const backfill = async (id: string) => {
    setError(null); setStatus(null);
    try {
      const r = await apiJson<BackfillResult>(apiBaseUrl, `/api/v1/predictors/${id}/backfill`, { method: "POST", body: "{}" }, apiKey);
      setStatus(`Backfilled ${r.written} value${r.written === 1 ? "" : "s"} from ${r.scanned} historical decision${r.scanned === 1 ? "" : "s"}.`);
      if (openId === id) await showDrift(id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Backfill failed.");
    }
  };

  const showDrift = React.useCallback(async (id: string) => {
    setLoadingReport(true); setReport(null);
    try {
      setReport(await apiJson<DriftReport>(apiBaseUrl, `/api/v1/predictors/${id}/drift`, {}, apiKey));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not read the drift report.");
    } finally {
      setLoadingReport(false);
    }
  }, [apiBaseUrl, apiKey]);

  return (
    <div style={{ padding: 24, display: "grid", gap: 18 }}>
      <PageHeader
        title="Predictors"
        subtitle="Track a number a decision already produces, and watch its population drift over time."
      />

      {error ? <div data-testid="pr-error" style={{ padding: 12, borderRadius: 10, background: "var(--rm-danger-bg)", color: "var(--rm-danger)", fontSize: 13 }}>{error}</div> : null}
      {status ? <div data-testid="pr-status" style={{ padding: 12, borderRadius: 10, background: "var(--rm-success-bg)", color: "var(--rm-success)", fontSize: 13 }}>{status}</div> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 400px) minmax(0,1fr)", gap: 18, alignItems: "start" }}>
        <Card>
          <SectionTitle>New predictor</SectionTitle>
          <div style={{ display: "grid", gap: 14 }}>
            <Field label="Name">
              <Input data-testid="pr-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Approval rate" />
            </Field>
            <Field label="Source" hint="What number to observe on every decision.">
              <Select data-testid="pr-source-type" value={sourceType} onChange={(e) => setSourceType(e.target.value)}>
                {sourceTypes.map((t) => <option key={t} value={t}>{t}</option>)}
              </Select>
            </Field>
            {needsSourceId ? (
              <Field label="Source id" hint={`Required for a ${sourceType} predictor.`}>
                <Input mono data-testid="pr-source-id" value={sourceId} onChange={(e) => setSourceId(e.target.value)} placeholder="scorecard or variable id" />
              </Field>
            ) : null}
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
              <Field label="Baseline days"><Input data-testid="pr-baseline-days" value={baselineDays} onChange={(e) => setBaselineDays(e.target.value)} /></Field>
              <Field label="Current days"><Input data-testid="pr-current-days" value={currentDays} onChange={(e) => setCurrentDays(e.target.value)} /></Field>
              <Field label="Bins"><Input data-testid="pr-bins" value={bins} onChange={(e) => setBins(e.target.value)} /></Field>
            </div>
            <Button data-testid="pr-create" variant="primary" onClick={create} disabled={saving}>
              {saving ? "Creating…" : "Create predictor"}
            </Button>
            <div style={{ fontSize: 11.5, color: "var(--rm-dim)" }}>
              Observation is forward-only, so a new predictor starts empty. Backfill replays past
              decisions into it; it is idempotent, so running it twice does not double the series.
            </div>
          </div>
        </Card>

        <div style={{ display: "grid", gap: 12 }}>
          {items.length === 0 ? (
            <Card>
              <EmptyState
                icon={<TrendingUp size={22} />}
                title="No predictors yet"
                hint="Point one at a scorecard, a variable, or the outcome itself to see how its distribution moves week over week."
              />
            </Card>
          ) : null}

          {items.map((p) => (
            <Card key={p.id} data-testid={`pr-card-${p.id}`} style={{ display: "grid", gap: 10 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                <div>
                  <div style={{ fontSize: 15, fontWeight: 700, color: "var(--rm-text)" }}>{p.name}</div>
                  <div style={{ fontSize: 12.5, color: "var(--rm-dim)", marginTop: 2 }}>
                    {p.source_type}{p.source_id ? ` · ${p.source_id}` : ""} · baseline {p.baseline_days ?? 30}d vs current {p.current_days ?? 7}d
                  </div>
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <Badge tone="accent">{p.source_type}</Badge>
                  <Badge tone={p.is_active === false ? "neutral" : "success"}>{p.is_active === false ? "inactive" : "active"}</Badge>
                </div>
              </div>

              <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                <Button data-testid={`pr-drift-${p.id}`} variant="secondary" size="sm"
                  onClick={() => { const next = openId === p.id ? null : p.id; setOpenId(next); setReport(null); if (next) void showDrift(p.id); }}>
                  <Activity size={13} /> Drift report
                </Button>
                <Button data-testid={`pr-backfill-${p.id}`} variant="secondary" size="sm" onClick={() => backfill(p.id)}>
                  <History size={13} /> Backfill
                </Button>
                <Button variant="ghost" size="sm" onClick={() => del(p.id)}>
                  <Trash2 size={13} /> Delete
                </Button>
              </div>

              {openId === p.id ? (
                <div data-testid="pr-drift-panel" style={{ display: "grid", gap: 10, borderTop: "1px solid var(--rm-border)", paddingTop: 10 }}>
                  {loadingReport ? <div style={{ fontSize: 12.5, color: "var(--rm-dim)" }}>Reading the report…</div> : null}
                  {report ? (
                    <>
                      <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 10 }}>
                        <Stat label="PSI" value={report.psi === null ? "—" : report.psi.toFixed(4)} tone={toneFor(report.classification)} />
                        <Stat label="Classification" value={report.classification} tone={toneFor(report.classification)} />
                        <Stat label="Baseline points" value={report.baseline_count} />
                        <Stat label="Current points" value={report.current_count} />
                      </div>

                      {report.classification === "insufficient_data" ? (
                        <div style={{ fontSize: 12.5, color: "var(--rm-dim)" }}>
                          Not enough observations in one of the windows yet. Observation is
                          forward-only — <strong style={{ color: "var(--rm-text)" }}>Backfill</strong> replays
                          historical decisions so the comparison has something to compare against.
                        </div>
                      ) : null}

                      <div style={{ fontSize: 11.5, color: "var(--rm-dim)", fontFamily: "var(--font-mono)" }}>
                        baseline {windowLabel(report.baseline_window)}<br />
                        current&nbsp; {windowLabel(report.current_window)}
                      </div>

                      {report.bins.length ? (
                        <div style={{ overflowX: "auto" }}>
                          <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 12.5 }}>
                            <thead>
                              <tr>
                                {["Bin", "Baseline", "Current", "Contribution"].map((h) => (
                                  <th key={h} style={{ textAlign: "left", padding: "6px 8px", borderBottom: "1px solid var(--rm-border)", color: "var(--rm-dim)", fontWeight: 600 }}>{h}</th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {report.bins.map((b, i) => (
                                <tr key={i}>
                                  <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--rm-dim)" }}>
                                    {b.lower ?? "-∞"} … {b.upper ?? "∞"}
                                  </td>
                                  <td style={{ padding: "6px 8px", fontVariantNumeric: "tabular-nums" }}>{b.baseline ?? 0}</td>
                                  <td style={{ padding: "6px 8px", fontVariantNumeric: "tabular-nums" }}>{b.current ?? 0}</td>
                                  <td style={{ padding: "6px 8px", fontVariantNumeric: "tabular-nums" }}>{(b.contribution ?? 0).toFixed(4)}</td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      ) : null}
                    </>
                  ) : null}
                </div>
              ) : null}
            </Card>
          ))}
        </div>
      </div>
    </div>
  );
}
