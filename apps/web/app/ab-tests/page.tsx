"use client";

import * as React from "react";
import { FlaskConical, Trophy, Play, Square, Trash2, Plus } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Select, Badge, Stat, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type Policy = { id: string; name: string };
type Variant = { id: string; role: string; weight: number };
type Experiment = { id: string; name: string; description?: string; status: string; variants: Variant[]; target_policy_id?: string; hash_key?: string };
type VariantRow = { id: string; role: string; users: number; approved: number; rejected: number; reviewed: number; approvalRate: number; rejectRate: number; avgLatencyMs: number };
type ChallengerRow = { id: string; liftPct: number; pValue: number; significant: boolean; recommendation?: { action?: string; reason?: string } };
type Analytics = {
  experiment: { id: string; name: string; status: string };
  variants: VariantRow[];
  significance: { pValue: number; significant: boolean };
  champion_challenger?: { champion?: { id: string } | null; challengers: ChallengerRow[] };
};

const STATUS_TONE: Record<string, "success" | "warning" | "danger" | "neutral" | "accent"> = {
  running: "success", draft: "neutral", completed: "accent", paused: "warning",
};

export default function AbTestsPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();

  const [experiments, setExperiments] = React.useState<Experiment[]>([]);
  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [selectedId, setSelectedId] = React.useState<string>("");
  const [analytics, setAnalytics] = React.useState<Analytics | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  // create form
  const [name, setName] = React.useState("");
  const [targetPolicy, setTargetPolicy] = React.useState("");
  const [championWeight, setChampionWeight] = React.useState(50);

  const loadExperiments = React.useCallback(async () => {
    try {
      const list = await apiJson<Experiment[]>(apiBaseUrl, "/api/v1/experiments", {}, apiKey);
      setExperiments(list);
      setSelectedId((current) => current || list[0]?.id || "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load experiments.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => {
    loadExperiments();
    apiJson<Policy[]>(apiBaseUrl, "/api/v1/policies", {}, apiKey)
      .then((p) => { setPolicies(p); setTargetPolicy((c) => c || p[0]?.id || ""); })
      .catch(() => setPolicies([]));
  }, [apiBaseUrl, apiKey, loadExperiments]);

  // load analytics for the selected experiment
  React.useEffect(() => {
    if (!selectedId) { setAnalytics(null); return; }
    let active = true;
    apiJson<Analytics>(apiBaseUrl, `/api/v1/experiments/${selectedId}/results`, {}, apiKey)
      .then((a) => { if (active) setAnalytics(a); })
      .catch(() => { if (active) setAnalytics(null); });
    return () => { active = false; };
  }, [apiBaseUrl, apiKey, selectedId, experiments]);

  const selected = experiments.find((e) => e.id === selectedId) ?? null;

  const create = async () => {
    if (!name.trim()) { setError("Name the experiment first."); return; }
    setBusy(true); setError(null);
    try {
      const clamped = Math.max(0, Math.min(100, championWeight));
      const created = await apiJson<Experiment>(
        apiBaseUrl, "/api/v1/experiments",
        {
          method: "POST",
          body: JSON.stringify({
            name: name.trim(),
            status: "draft",
            target_policy_id: targetPolicy,
            hash_key: "user_id",
            variants: [
              { id: "champion", role: "champion", weight: clamped },
              { id: "challenger", role: "challenger", weight: 100 - clamped },
            ],
          }),
        },
        apiKey
      );
      setName("");
      await loadExperiments();
      setSelectedId(created.id);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the experiment.");
    } finally {
      setBusy(false);
    }
  };

  const setStatus = async (status: string) => {
    if (!selected) return;
    setBusy(true); setError(null);
    try {
      await apiJson(apiBaseUrl, `/api/v1/experiments/${selected.id}/status`, { method: "PATCH", body: JSON.stringify({ status }) }, apiKey);
      await loadExperiments();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not change status.");
    } finally {
      setBusy(false);
    }
  };

  const promote = async (variantId: string) => {
    if (!selected) return;
    setBusy(true); setError(null);
    try {
      await apiJson(apiBaseUrl, `/api/v1/experiments/${selected.id}/promote`, { method: "POST", body: JSON.stringify({ variant_id: variantId, force: true }) }, apiKey);
      await loadExperiments();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not promote the variant.");
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!selected) return;
    setBusy(true); setError(null);
    try {
      await apiJson(apiBaseUrl, `/api/v1/experiments/${selected.id}`, { method: "DELETE" }, apiKey);
      setSelectedId("");
      await loadExperiments();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not delete the experiment.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ padding: 24 }}>
      <PageHeader title="A/B experiments" subtitle="Run champion vs. challenger tests on a live policy — split traffic by a stable hash, watch approval lift and significance, then promote the winner." />

      <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 380px) minmax(0,1fr)", gap: 18, alignItems: "start" }}>
        {/* ---- left: list + create ---- */}
        <div style={{ display: "grid", gap: 18 }}>
          <Card>
            <SectionTitle>Experiments</SectionTitle>
            {experiments.length === 0 ? (
              <EmptyState icon={<FlaskConical size={22} />} title="No experiments yet" hint="Create your first champion/challenger test below." />
            ) : (
              <div style={{ display: "grid", gap: 8, marginTop: 8 }}>
                {experiments.map((exp) => (
                  <button key={exp.id} onClick={() => setSelectedId(exp.id)} type="button"
                    style={{ textAlign: "left", border: "1px solid var(--rm-border)", background: selectedId === exp.id ? "var(--rm-hover)" : "var(--rm-card)", borderRadius: 10, padding: "10px 12px", cursor: "pointer", display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                    <span style={{ fontSize: 13, fontWeight: 600, color: "var(--rm-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{exp.name}</span>
                    <Badge tone={STATUS_TONE[exp.status] ?? "neutral"}>{exp.status}</Badge>
                  </button>
                ))}
              </div>
            )}
          </Card>

          <Card>
            <SectionTitle>New experiment</SectionTitle>
            <div style={{ display: "grid", gap: 12, marginTop: 8 }}>
              <Field label="Name">
                <input className="rm-input" value={name} onChange={(e) => setName(e.target.value)} placeholder="Bureau cutoff 700 vs 680" />
              </Field>
              <Field label="Target policy">
                <Select value={targetPolicy} onChange={(e) => setTargetPolicy(e.target.value)}>
                  {policies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </Select>
              </Field>
              <Field label={`Traffic split — champion ${championWeight}% / challenger ${100 - championWeight}%`}>
                <input type="range" min={0} max={100} step={5} value={championWeight} onChange={(e) => setChampionWeight(parseInt(e.target.value, 10))} style={{ width: "100%" }} />
              </Field>
              <Button variant="primary" onClick={create} disabled={busy || !name.trim() || !targetPolicy}>
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Plus size={15} /> Create draft</span>
              </Button>
            </div>
          </Card>
        </div>

        {/* ---- right: detail + analytics ---- */}
        <div style={{ display: "grid", gap: 18 }}>
          {error ? <div style={{ color: "var(--rm-danger)", fontSize: 13 }}>{error}</div> : null}
          {!selected ? (
            <Card><EmptyState icon={<FlaskConical size={22} />} title="Select an experiment" hint="Pick one on the left, or create a new champion/challenger test." /></Card>
          ) : (
            <>
              <Card>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
                  <div>
                    <div style={{ fontSize: 16, fontWeight: 700, color: "var(--rm-text)" }}>{selected.name}</div>
                    <div style={{ fontSize: 12.5, color: "var(--rm-dim)", marginTop: 2 }}>
                      Target: <span className="rm-mono">{selected.target_policy_id ?? "—"}</span> · split by <span className="rm-mono">{selected.hash_key ?? "user_id"}</span>
                    </div>
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <Badge tone={STATUS_TONE[selected.status] ?? "neutral"}>{selected.status}</Badge>
                    {selected.status === "draft" || selected.status === "paused" ? (
                      <Button variant="primary" size="sm" onClick={() => setStatus("running")} disabled={busy}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Play size={14} /> Start</span>
                      </Button>
                    ) : null}
                    {selected.status === "running" ? (
                      <Button variant="secondary" size="sm" onClick={() => setStatus("paused")} disabled={busy}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Square size={14} /> Pause</span>
                      </Button>
                    ) : null}
                    {selected.status === "draft" ? (
                      <Button variant="danger" size="sm" onClick={remove} disabled={busy}>
                        <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Trash2 size={14} /> Delete</span>
                      </Button>
                    ) : null}
                  </div>
                </div>
              </Card>

              {analytics ? (
                <>
                  <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(130px, 1fr))", gap: 12 }}>
                    <Stat label="Assigned users" value={analytics.variants.reduce((n, v) => n + v.users, 0).toLocaleString()} />
                    <Stat label="p-value" value={analytics.significance.pValue.toFixed(4)} tone={analytics.significance.significant ? "success" : undefined} />
                    <Stat label="Significant" value={analytics.significance.significant ? "Yes" : "No"} tone={analytics.significance.significant ? "success" : "warning"} />
                  </div>

                  <Card>
                    <SectionTitle>Variants</SectionTitle>
                    <div style={{ overflowX: "auto", marginTop: 8 }}>
                      <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 12.5 }}>
                        <thead>
                          <tr>
                            {["Variant", "Role", "Users", "Approval", "Reject", "Avg latency", ""].map((h) => (
                              <th key={h} style={{ textAlign: "left", padding: "8px 10px", color: "var(--rm-muted)", fontWeight: 600, borderBottom: "1px solid var(--rm-border)", whiteSpace: "nowrap" }}>{h}</th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {analytics.variants.map((v) => {
                            const challenger = analytics.champion_challenger?.challengers.find((c) => c.id === v.id);
                            return (
                              <tr key={v.id}>
                                <td style={{ padding: "8px 10px", fontWeight: 600, color: "var(--rm-text)" }}>{v.id}</td>
                                <td style={{ padding: "8px 10px" }}><Badge tone={v.role === "champion" ? "accent" : "neutral"}>{v.role}</Badge></td>
                                <td className="rm-mono" style={{ padding: "8px 10px", color: "var(--rm-muted)" }}>{v.users.toLocaleString()}</td>
                                <td className="rm-mono" style={{ padding: "8px 10px", color: "var(--rm-success)" }}>{v.approvalRate}%{challenger ? <span style={{ color: challenger.liftPct >= 0 ? "var(--rm-success)" : "var(--rm-danger)", marginLeft: 6 }}>({challenger.liftPct >= 0 ? "+" : ""}{challenger.liftPct})</span> : null}</td>
                                <td className="rm-mono" style={{ padding: "8px 10px", color: "var(--rm-muted)" }}>{v.rejectRate}%</td>
                                <td className="rm-mono" style={{ padding: "8px 10px", color: "var(--rm-muted)" }}>{v.avgLatencyMs}ms</td>
                                <td style={{ padding: "8px 10px" }}>
                                  {selected.status !== "completed" ? (
                                    <Button variant="secondary" size="sm" onClick={() => promote(v.id)} disabled={busy}>
                                      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}><Trophy size={13} /> Promote</span>
                                    </Button>
                                  ) : null}
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                    {analytics.champion_challenger?.challengers.some((c) => c.recommendation?.action) ? (
                      <div style={{ marginTop: 14, display: "grid", gap: 8 }}>
                        {analytics.champion_challenger.challengers.map((c) => c.recommendation?.action ? (
                          <div key={c.id} style={{ fontSize: 12.5, color: "var(--rm-muted)" }}>
                            <strong style={{ color: "var(--rm-text)" }}>{c.id}</strong>: {c.recommendation.action}{c.recommendation.reason ? ` — ${c.recommendation.reason}` : ""}
                          </div>
                        ) : null)}
                      </div>
                    ) : null}
                    <div style={{ marginTop: 12, fontSize: 12, color: "var(--rm-dim)" }}>
                      Lift and significance are computed from the decision log (two-proportion z-test). Run traffic through the target policy with a <span className="rm-mono">userId</span> to populate these.
                    </div>
                  </Card>
                </>
              ) : (
                <Card><EmptyState icon={<FlaskConical size={22} />} title="No results yet" hint="Start the experiment and route decisions through the target policy to see variant performance." /></Card>
              )}
            </>
          )}
        </div>
      </div>
    </div>
  );
}
