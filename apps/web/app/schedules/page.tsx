"use client";

import * as React from "react";
import { Play, Pause, Trash2, CalendarClock } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Select, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type ScheduleRecord = {
  id: string;
  policy_id: string;
  cron_expression: string;
  is_active: boolean;
  last_run_at?: string | null;
  payload_source?: Record<string, unknown>;
  config?: Record<string, unknown>;
};
type Policy = { id: string; name: string };

const CRON_PRESETS: Array<{ label: string; value: string }> = [
  { label: "Every hour", value: "0 * * * *" },
  { label: "Daily 09:00", value: "0 9 * * *" },
  { label: "Weekdays 08:00", value: "0 8 * * 1-5" },
  { label: "Weekly (Mon 09:00)", value: "0 9 * * 1" },
  { label: "Monthly (1st 00:00)", value: "0 0 1 * *" },
];

export default function SchedulesPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [schedules, setSchedules] = React.useState<ScheduleRecord[]>([]);
  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [error, setError] = React.useState<string | null>(null);

  const [policyId, setPolicyId] = React.useState("");
  const [cron, setCron] = React.useState("0 9 * * *");
  const [creating, setCreating] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const [s, p] = await Promise.all([
        apiJson<ScheduleRecord[]>(apiBaseUrl, "/api/v1/schedules", {}, apiKey),
        apiJson<Policy[]>(apiBaseUrl, "/api/v1/policies", {}, apiKey),
      ]);
      setSchedules(s);
      setPolicies(p);
      if (!policyId && p[0]) setPolicyId(p[0].id);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load schedules.");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void load(); }, [load]);

  const create = async () => {
    if (!policyId || !cron.trim()) { setError("Pick a policy and a cron expression."); return; }
    setCreating(true);
    setError(null);
    try {
      await apiJson(apiBaseUrl, "/api/v1/schedules", { method: "POST", body: JSON.stringify({ policy_id: policyId, cron_expression: cron.trim(), is_active: true, payload_source: {} }) }, apiKey);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to create schedule.");
    } finally {
      setCreating(false);
    }
  };

  const act = async (path: string, method: string, body?: unknown) => {
    try {
      await apiJson(apiBaseUrl, path, { method, ...(body ? { body: JSON.stringify(body) } : {}) }, apiKey);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Action failed.");
    }
  };

  return (
    <div style={{ padding: 24, display: "grid", gap: 18 }}>
      <PageHeader title="Scheduled Jobs" subtitle="Cron-driven batch execution across tenant policies — create, pause, run, and remove schedules." />

      {error ? <div style={{ padding: 12, borderRadius: 10, background: "var(--rm-danger-bg)", color: "var(--rm-danger)", fontSize: 13 }}>{error}</div> : null}

      {/* create form */}
      <Card>
        <SectionTitle>New schedule</SectionTitle>
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) minmax(0,1fr) auto", gap: 12, alignItems: "end" }}>
          <Field label="Policy">
            <Select value={policyId} onChange={(e) => setPolicyId(e.target.value)}>
              {policies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
            </Select>
          </Field>
          <Field label="Cron expression">
            <div style={{ display: "flex", gap: 8 }}>
              <input className="rm-input rm-mono" value={cron} onChange={(e) => setCron(e.target.value)} placeholder="0 9 * * *" />
              <Select value="" onChange={(e) => e.target.value && setCron(e.target.value)} style={{ width: 150 }}>
                <option value="">Presets…</option>
                {CRON_PRESETS.map((c) => <option key={c.value} value={c.value}>{c.label}</option>)}
              </Select>
            </div>
          </Field>
          <Button variant="primary" onClick={create} disabled={creating || !policyId}>{creating ? "Creating…" : "Create schedule"}</Button>
        </div>
      </Card>

      {/* list */}
      <div style={{ display: "grid", gap: 12 }}>
        {schedules.length === 0 ? (
          <Card><EmptyState icon={<CalendarClock size={22} />} title="No schedules yet" hint="Create a schedule above to run a policy on a cron cadence." /></Card>
        ) : null}
        {schedules.map((s) => (
          <Card key={s.id} style={{ display: "grid", gap: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between", gap: 12, alignItems: "flex-start" }}>
              <div>
                <div style={{ fontSize: 15, fontWeight: 700, color: "var(--rm-text)" }}>{s.policy_id}</div>
                <div className="rm-mono" style={{ fontSize: 12.5, color: "var(--rm-muted)", marginTop: 2 }}>{s.cron_expression}</div>
              </div>
              <Badge tone={s.is_active ? "success" : "neutral"}>{s.is_active ? "active" : "paused"}</Badge>
            </div>
            <div style={{ fontSize: 12.5, color: "var(--rm-dim)" }}>
              Last run: {s.last_run_at ?? "never"} · Source: {String(s.payload_source?.type ?? "static_json")}
            </div>
            <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
              <Button variant="secondary" size="sm" onClick={() => act(`/api/v1/schedules/${s.id}/run-now`, "POST")}><Play size={13} /> Run now</Button>
              <Button variant="secondary" size="sm" onClick={() => act(`/api/v1/schedules/${s.id}`, "PUT", { policy_id: s.policy_id, cron_expression: s.cron_expression, is_active: !s.is_active, payload_source: s.payload_source ?? {} })}>
                {s.is_active ? <><Pause size={13} /> Pause</> : <><Play size={13} /> Resume</>}
              </Button>
              <Button variant="ghost" size="sm" onClick={() => act(`/api/v1/schedules/${s.id}`, "DELETE")}><Trash2 size={13} /> Delete</Button>
            </div>
          </Card>
        ))}
      </div>
    </div>
  );
}
