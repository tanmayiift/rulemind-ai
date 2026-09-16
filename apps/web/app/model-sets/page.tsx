"use client";

/**
 * Model Sets — group models, route between them by rule, compare candidates on one input.
 *
 * WHY THIS FILE EXISTS. The backend (app/routers/modelsets.py, 7 endpoints, well tested) shipped
 * with no page at all: `/model-sets` had a title and subtitle in app-shell.tsx's page-metadata
 * map and nothing else — no route, no sidebar entry, no link anywhere in the app. So the whole
 * capability was reachable only by calling the API by hand.
 *
 * The comparison is the point of the feature, and the reason it is the largest thing on this
 * page: `POST /{id}/compare` runs EVERY member on the same input and deliberately bypasses
 * routing, because the question an operator is asking is "what would each candidate have said",
 * including the ones this input would not have routed to. The response still reports which member
 * routing WOULD have chosen, so both readings are on screen together.
 *
 * Drift here is reported, never enforced — a member whose model was retrained since it was pinned
 * shows as out of sync, and that is all. Blocking a decision on it would make the feature a
 * liability.
 */

import * as React from "react";
import { Trash2, GitCompare, Layers, RefreshCw, Plus } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Textarea, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type Member = {
  model_id: string;
  label?: string;
  condition?: string | null;
  pinned_version?: string | number | null;
};
type ModelSet = {
  id: string;
  name: string;
  description?: string;
  members?: Member[];
  is_active?: boolean;
};
type Model = { id: string; name: string };
type CompareResult = {
  id: string;
  routed_to: string | null;
  routing_reason?: string;
  results: Array<{
    label: string;
    model_id: string;
    pinned_version?: string | number | null;
    current_version?: string | number | null;
    result?: unknown;
    error?: string;
  }>;
};
type Drift = { id: string; drifted: string[]; in_sync: boolean };

const BLANK_MEMBER: Member = { model_id: "", label: "", condition: "" };

export default function ModelSetsPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [sets, setSets] = React.useState<ModelSet[]>([]);
  const [models, setModels] = React.useState<Model[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<string | null>(null);

  const [name, setName] = React.useState("");
  const [description, setDescription] = React.useState("");
  const [members, setMembers] = React.useState<Member[]>([{ ...BLANK_MEMBER }]);
  const [saving, setSaving] = React.useState(false);

  const [openId, setOpenId] = React.useState<string | null>(null);
  const [features, setFeatures] = React.useState('{\n  "feature_0": 0.9,\n  "feature_1": 0.1\n}');
  const [compare, setCompare] = React.useState<CompareResult | null>(null);
  const [comparing, setComparing] = React.useState(false);
  const [drift, setDrift] = React.useState<Record<string, Drift>>({});

  const load = React.useCallback(async () => {
    try {
      const [s, m] = await Promise.all([
        apiJson<ModelSet[]>(apiBaseUrl, "/api/v1/model-sets", {}, apiKey),
        apiJson<Model[]>(apiBaseUrl, "/api/v1/models", {}, apiKey)
      ]);
      setSets(s);
      setModels(m);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load model sets.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void load(); }, [load]);

  const setMember = (i: number, patch: Partial<Member>) =>
    setMembers((prev) => prev.map((m, idx) => (idx === i ? { ...m, ...patch } : m)));

  const create = async () => {
    // The API rejects an empty member list with 422; catching it here names the missing field
    // instead of surfacing a validation blob.
    const cleaned = members
      .filter((m) => m.model_id.trim())
      .map((m) => ({
        model_id: m.model_id.trim(),
        label: (m.label || "").trim() || undefined,
        condition: (m.condition || "").trim() || undefined
      }));
    if (!name.trim() || cleaned.length === 0) {
      setError("Give the set a name and choose at least one model.");
      return;
    }
    setSaving(true); setError(null); setStatus(null);
    try {
      await apiJson(apiBaseUrl, "/api/v1/model-sets", {
        method: "POST",
        body: JSON.stringify({ name: name.trim(), description, members: cleaned, is_active: true })
      }, apiKey);
      setStatus(`Created “${name.trim()}”.`);
      setName(""); setDescription(""); setMembers([{ ...BLANK_MEMBER }]);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not create the set.");
    } finally {
      setSaving(false);
    }
  };

  const del = async (id: string) => {
    try {
      await apiJson(apiBaseUrl, `/api/v1/model-sets/${id}`, { method: "DELETE" }, apiKey);
      if (openId === id) { setOpenId(null); setCompare(null); }
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Delete failed.");
    }
  };

  const runCompare = async (id: string) => {
    setCompare(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(features);
    } catch {
      setError("The feature input is not valid JSON.");
      return;
    }
    setComparing(true); setError(null);
    try {
      setCompare(await apiJson<CompareResult>(apiBaseUrl, `/api/v1/model-sets/${id}/compare`, {
        method: "POST",
        body: JSON.stringify({ features: parsed })
      }, apiKey));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Compare failed.");
    } finally {
      setComparing(false);
    }
  };

  const checkDrift = async (id: string) => {
    try {
      const d = await apiJson<Drift>(apiBaseUrl, `/api/v1/model-sets/${id}/drift`, {}, apiKey);
      setDrift((prev) => ({ ...prev, [id]: d }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Drift check failed.");
    }
  };

  return (
    <div style={{ padding: 24, display: "grid", gap: 18 }}>
      <PageHeader
        title="Model Sets"
        subtitle="Group models into a versioned set, route between them by rule, and compare candidates on one input."
      />

      {error ? <div data-testid="ms-error" style={{ padding: 12, borderRadius: 10, background: "var(--rm-danger-bg)", color: "var(--rm-danger)", fontSize: 13 }}>{error}</div> : null}
      {status ? <div data-testid="ms-status" style={{ padding: 12, borderRadius: 10, background: "var(--rm-success-bg)", color: "var(--rm-success)", fontSize: 13 }}>{status}</div> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 400px) minmax(0,1fr)", gap: 18, alignItems: "start" }}>
        <Card>
          <SectionTitle>New set</SectionTitle>
          <div style={{ display: "grid", gap: 14 }}>
            <Field label="Name">
              <Input data-testid="ms-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="Champion vs challenger" />
            </Field>
            <Field label="Description">
              <Input value={description} onChange={(e) => setDescription(e.target.value)} placeholder="Optional" />
            </Field>

            <Field
              label="Members"
              hint="A member with no condition is the fallback. Conditions are branch-style expressions, evaluated in order."
            >
              <div style={{ display: "grid", gap: 10 }}>
                {members.map((m, i) => (
                  <div key={i} style={{ display: "grid", gap: 6, padding: 10, border: "1px solid var(--rm-border)", borderRadius: 8 }}>
                    <Select
                      data-testid={`ms-member-model-${i}`}
                      value={m.model_id}
                      onChange={(e) => setMember(i, { model_id: e.target.value })}
                    >
                      <option value="">Choose a model…</option>
                      {models.map((mm) => <option key={mm.id} value={mm.id}>{mm.name}</option>)}
                    </Select>
                    <Input
                      data-testid={`ms-member-label-${i}`}
                      value={m.label || ""}
                      onChange={(e) => setMember(i, { label: e.target.value })}
                      placeholder="Label (defaults to the model id)"
                    />
                    <Input
                      mono
                      data-testid={`ms-member-condition-${i}`}
                      value={m.condition || ""}
                      onChange={(e) => setMember(i, { condition: e.target.value })}
                      placeholder='Condition, e.g. score > 700 — blank means fallback'
                      style={{ fontSize: 12 }}
                    />
                    {members.length > 1 ? (
                      <Button variant="ghost" size="sm" style={{ justifySelf: "start" }}
                        onClick={() => setMembers((prev) => prev.filter((_, idx) => idx !== i))}>
                        <Trash2 size={12} /> Remove
                      </Button>
                    ) : null}
                  </div>
                ))}
                <Button variant="secondary" size="sm" style={{ justifySelf: "start" }}
                  onClick={() => setMembers((prev) => [...prev, { ...BLANK_MEMBER }])}>
                  <Plus size={13} /> Add member
                </Button>
              </div>
            </Field>

            <Button data-testid="ms-create" variant="primary" onClick={create} disabled={saving}>
              {saving ? "Creating…" : "Create set"}
            </Button>
          </div>
        </Card>

        <div style={{ display: "grid", gap: 12 }}>
          {sets.length === 0 ? (
            <Card>
              <EmptyState
                icon={<Layers size={22} />}
                title="No model sets yet"
                hint="Group two or more hosted models to route between them by rule, and to score a candidate against the champion on the same input."
              />
            </Card>
          ) : null}

          {sets.map((s) => {
            const d = drift[s.id];
            return (
              <Card key={s.id} data-testid={`ms-card-${s.id}`} style={{ display: "grid", gap: 10 }}>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12 }}>
                  <div>
                    <div style={{ fontSize: 15, fontWeight: 700, color: "var(--rm-text)" }}>{s.name}</div>
                    <div style={{ fontSize: 12.5, color: "var(--rm-dim)", marginTop: 2 }}>{s.description || "—"}</div>
                  </div>
                  <div style={{ display: "flex", gap: 6, flexWrap: "wrap", justifyContent: "flex-end" }}>
                    <Badge tone="accent">{(s.members || []).length} member{(s.members || []).length === 1 ? "" : "s"}</Badge>
                    <Badge tone={s.is_active === false ? "neutral" : "success"}>{s.is_active === false ? "inactive" : "active"}</Badge>
                    {d ? <Badge tone={d.in_sync ? "success" : "warning"}>{d.in_sync ? "in sync" : `${d.drifted.length} drifted`}</Badge> : null}
                  </div>
                </div>

                <div style={{ display: "grid", gap: 4 }}>
                  {(s.members || []).map((m) => (
                    <div key={m.label || m.model_id} style={{ display: "flex", gap: 8, alignItems: "baseline", fontSize: 12.5 }}>
                      <span style={{ fontWeight: 600, color: "var(--rm-text)" }}>{m.label || m.model_id}</span>
                      <span style={{ fontFamily: "var(--font-mono)", color: "var(--rm-dim)", fontSize: 11.5 }}>
                        {m.condition ? m.condition : "fallback"}
                      </span>
                      {d && d.drifted.includes(m.label || m.model_id)
                        ? <Badge tone="warning">retrained since pin</Badge>
                        : null}
                    </div>
                  ))}
                </div>

                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  <Button data-testid={`ms-compare-${s.id}`} variant="secondary" size="sm"
                    onClick={() => { setOpenId(openId === s.id ? null : s.id); setCompare(null); }}>
                    <GitCompare size={13} /> Compare
                  </Button>
                  <Button variant="secondary" size="sm" onClick={() => checkDrift(s.id)}>
                    <RefreshCw size={13} /> Check drift
                  </Button>
                  <Button variant="ghost" size="sm" onClick={() => del(s.id)}>
                    <Trash2 size={13} /> Delete
                  </Button>
                </div>

                {openId === s.id ? (
                  <div style={{ display: "grid", gap: 8, borderTop: "1px solid var(--rm-border)", paddingTop: 10 }}>
                    <div style={{ fontSize: 11.5, color: "var(--rm-dim)" }}>
                      Every member runs on this input, including the ones routing would skip.
                    </div>
                    <Textarea
                      mono
                      data-testid="ms-features"
                      rows={4}
                      value={features}
                      onChange={(e) => setFeatures(e.target.value)}
                      style={{ fontSize: 12 }}
                    />
                    <Button data-testid="ms-run-compare" variant="primary" size="sm" style={{ justifySelf: "start" }}
                      disabled={comparing} onClick={() => runCompare(s.id)}>
                      {comparing ? "Running…" : "Run comparison"}
                    </Button>

                    {compare ? (
                      <div data-testid="ms-compare-out" style={{ display: "grid", gap: 8 }}>
                        <div style={{ fontSize: 12.5, color: "var(--rm-dim)" }}>
                          Routing would have chosen{" "}
                          <strong style={{ color: "var(--rm-text)" }}>{compare.routed_to ?? "no member"}</strong>
                          {compare.routing_reason ? ` — ${compare.routing_reason}` : ""}
                        </div>
                        <div style={{ overflowX: "auto" }}>
                          <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 12.5 }}>
                            <thead>
                              <tr>
                                {["Member", "Pinned", "Current", "Result"].map((h) => (
                                  <th key={h} style={{ textAlign: "left", padding: "6px 8px", borderBottom: "1px solid var(--rm-border)", color: "var(--rm-dim)", fontWeight: 600 }}>{h}</th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {compare.results.map((r) => (
                                <tr key={r.label} data-testid={`ms-result-${r.label}`}
                                  style={{ background: r.label === compare.routed_to ? "var(--rm-accent-bg)" : undefined }}>
                                  <td style={{ padding: "6px 8px", fontWeight: 600, color: "var(--rm-text)" }}>{r.label}</td>
                                  <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--rm-dim)" }}>{String(r.pinned_version ?? "—")}</td>
                                  <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)", fontSize: 11.5, color: "var(--rm-dim)" }}>{String(r.current_version ?? "—")}</td>
                                  <td style={{ padding: "6px 8px", fontFamily: "var(--font-mono)", fontSize: 11.5 }}>
                                    {/* A member that errors is a RESULT, not an outage — the API returns the
                                        others alongside it, and hiding it would defeat the comparison. */}
                                    {r.error
                                      ? <span style={{ color: "var(--rm-danger)" }}>{r.error}</span>
                                      : <span style={{ color: "var(--rm-code-text)" }}>{JSON.stringify(r.result)}</span>}
                                  </td>
                                </tr>
                              ))}
                            </tbody>
                          </table>
                        </div>
                      </div>
                    ) : null}
                  </div>
                ) : null}
              </Card>
            );
          })}
        </div>
      </div>
    </div>
  );
}
