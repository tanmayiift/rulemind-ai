"use client";

import * as React from "react";
import { Plus, Trash2, Play, Download, Mail, FileBarChart, Send } from "lucide-react";
import { apiJson, apiText } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type Column = { key: string; label?: string; path: string; source_id?: string; sample?: unknown };
type Schedule = { enabled?: boolean; cron?: string; recipients?: string[] };
type Report = {
  id: string; name: string; description?: string; columns: Column[];
  filters: { days?: number; outcomes?: string[]; sources?: string[] };
  timezone: string; schedule?: Schedule | null; last_run?: { generated_at?: string; row_count?: number; delivery?: { transport?: string } } | null; version?: number;
};
type RunResult = { columns: Column[]; rows: Record<string, unknown>[]; row_count: number; timezone: string };
type EmailCfg = { host?: string; from_addr?: string; username?: string; configured?: boolean; password_set?: boolean };

const TIMEZONES = ["UTC", "Asia/Kolkata", "America/New_York", "America/Los_Angeles", "Europe/London", "Europe/Berlin", "Asia/Singapore", "Australia/Sydney"];
const OUTCOMES = ["approve", "review", "reject"];
const uid = () => Math.random().toString(36).slice(2, 8);

function emptyReport(): Report {
  return {
    id: "", name: "", columns: [
      { key: "id", label: "Decision ID", path: "id" },
      { key: "created_at", label: "Timestamp", path: "created_at" },
      { key: "outcome", label: "Outcome", path: "outcome" },
    ], filters: { days: 30 }, timezone: "UTC", schedule: { enabled: false, cron: "0 9 * * *", recipients: [] },
  };
}

export default function ReportsPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [reports, setReports] = React.useState<Report[]>([]);
  const [suggestions, setSuggestions] = React.useState<Column[]>([]);
  const [draft, setDraft] = React.useState<Report>(emptyReport());
  const [preview, setPreview] = React.useState<RunResult | null>(null);
  const [email, setEmail] = React.useState<EmailCfg | null>(null);
  const [emailPw, setEmailPw] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    try {
      const [r, s, e] = await Promise.all([
        apiJson<Report[]>(apiBaseUrl, "/api/v1/reports", {}, apiKey),
        apiJson<{ columns: Column[] }>(apiBaseUrl, "/api/v1/reports/column-suggestions", {}, apiKey).then((x) => x.columns).catch(() => []),
        apiJson<EmailCfg>(apiBaseUrl, "/api/v1/reports/email-config", {}, apiKey).catch(() => null),
      ]);
      setReports(r); setSuggestions(s); setEmail(e); setError(null);
    } catch (err) { setError(err instanceof Error ? err.message : "Unable to load reports."); }
  }, [apiBaseUrl, apiKey]);
  React.useEffect(() => { void load(); }, [load]);

  const patch = (p: Partial<Report>) => setDraft((d) => ({ ...d, ...p }));
  const hasColumn = (path: string) => draft.columns.some((c) => c.path === path);
  const toggleColumn = (col: Column) => patch({
    columns: hasColumn(col.path) ? draft.columns.filter((c) => c.path !== col.path)
      : [...draft.columns, { key: col.key || col.path.replace(/\./g, "_"), label: col.label, path: col.path }],
  });
  const toggleOutcome = (o: string) => {
    const cur = new Set(draft.filters.outcomes ?? []);
    cur.has(o) ? cur.delete(o) : cur.add(o);
    patch({ filters: { ...draft.filters, outcomes: [...cur] } });
  };

  const runPreview = async () => {
    setError(null);
    try {
      setPreview(await apiJson<RunResult>(apiBaseUrl, "/api/v1/reports/preview", {
        method: "POST", body: JSON.stringify({ columns: draft.columns, filters: draft.filters, timezone: draft.timezone }),
      }, apiKey));
    } catch (e) { setError(e instanceof Error ? e.message : "Preview failed."); }
  };

  const save = async () => {
    setError(null); setStatus(null);
    if (!draft.name.trim()) { setError("Name the report."); return; }
    try {
      const body = JSON.stringify({ name: draft.name, description: draft.description, columns: draft.columns, filters: draft.filters, timezone: draft.timezone, schedule: draft.schedule });
      const saved = draft.id
        ? await apiJson<Report>(apiBaseUrl, `/api/v1/reports/${draft.id}`, { method: "PUT", body }, apiKey)
        : await apiJson<Report>(apiBaseUrl, "/api/v1/reports", { method: "POST", body }, apiKey);
      setDraft(saved); setStatus(`Saved "${saved.name}".`); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Save failed."); }
  };

  const exportCsv = async () => {
    if (!draft.id) { setError("Save the report before exporting."); return; }
    try {
      const { text } = await apiText(apiBaseUrl, `/api/v1/reports/${draft.id}/export.csv`, {}, apiKey);
      const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
      const a = document.createElement("a"); a.href = url; a.download = `${draft.id}.csv`; a.click(); URL.revokeObjectURL(url);
    } catch (e) { setError(e instanceof Error ? e.message : "Export failed."); }
  };

  const sendNow = async () => {
    if (!draft.id) { setError("Save the report first."); return; }
    setStatus(null); setError(null);
    try {
      const res = await apiJson<{ row_count: number; delivery: { transport?: string; note?: string } }>(apiBaseUrl, `/api/v1/reports/${draft.id}/send`, { method: "POST" }, apiKey);
      setStatus(res.delivery.transport === "smtp" ? `Emailed ${res.row_count} rows.` : (res.delivery.note || "Report generated (no SMTP configured yet)."));
    } catch (e) { setError(e instanceof Error ? e.message : "Send failed."); }
  };

  const saveEmail = async () => {
    setStatus(null); setError(null);
    try {
      await apiJson(apiBaseUrl, "/api/v1/reports/email-config", { method: "PUT", body: JSON.stringify({ ...email, password: emailPw || undefined }) }, apiKey);
      setEmailPw(""); setStatus("Email settings saved."); await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save email settings."); }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader title="Reports" subtitle="Build reports over your decisions — dynamic columns, filters, timezone, and scheduled email delivery."
        actions={<Button onClick={() => { setDraft(emptyReport()); setPreview(null); setStatus(null); }}><Plus size={15} /> New report</Button>} />
      {error ? <Card><div style={{ color: "var(--rm-danger)", fontSize: 13 }}>{error}</div></Card> : null}
      {status ? <Card><div style={{ color: "var(--rm-success)", fontSize: 13 }}>{status}</div></Card> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 320px", gap: 20, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
          <Card>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 200px", gap: 12 }}>
              <Field label="Report name"><Input value={draft.name} onChange={(e) => patch({ name: e.target.value })} placeholder="e.g. Daily rejections" /></Field>
              <Field label="Timezone"><Select value={draft.timezone} onChange={(e) => patch({ timezone: e.target.value })}>{TIMEZONES.map((t) => <option key={t} value={t}>{t}</option>)}</Select></Field>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "160px 1fr", gap: 12, marginTop: 12 }}>
              <Field label="Time window (days)"><Input type="number" value={draft.filters.days ?? 30} onChange={(e) => patch({ filters: { ...draft.filters, days: Number(e.target.value) } })} /></Field>
              <Field label="Outcomes (blank = all)">
                <div style={{ display: "flex", gap: 8, paddingTop: 6 }}>
                  {OUTCOMES.map((o) => (
                    <label key={o} style={{ display: "flex", gap: 5, alignItems: "center", fontSize: 13, cursor: "pointer" }}>
                      <input type="checkbox" checked={(draft.filters.outcomes ?? []).includes(o)} onChange={() => toggleOutcome(o)} />{o}
                    </label>
                  ))}
                </div>
              </Field>
            </div>
          </Card>

          <Card>
            <SectionTitle right={<span style={{ fontSize: 12, color: "var(--rm-muted)" }}>{draft.columns.length} selected</span>}>Columns</SectionTitle>
            <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
              {suggestions.map((c) => {
                const on = hasColumn(c.path);
                return (
                  <button key={c.path} onClick={() => toggleColumn(c)}
                    style={{ fontSize: 12, padding: "5px 10px", borderRadius: 999, cursor: "pointer", border: "1px solid var(--rm-border)",
                      background: on ? "var(--rm-accent)" : "transparent", color: on ? "var(--rm-inverse-text)" : "var(--rm-text)" }}>
                    {c.label || c.path}
                  </button>
                );
              })}
            </div>
          </Card>

          <Card>
            <SectionTitle right={<div style={{ display: "flex", gap: 8 }}>
              <Button variant="secondary" onClick={runPreview}><Play size={14} /> Preview</Button>
              <Button variant="secondary" onClick={exportCsv}><Download size={14} /> CSV</Button>
              <Button onClick={save}>{draft.id ? "Save" : "Create"}</Button>
            </div>}>Preview</SectionTitle>
            {preview ? (
              <div style={{ overflowX: "auto", maxHeight: 320 }}>
                <div style={{ fontSize: 12, color: "var(--rm-muted)", marginBottom: 8 }}>{preview.row_count} rows · {preview.timezone}</div>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 12.5 }}>
                  <thead><tr>{draft.columns.map((c) => <th key={c.path} style={{ textAlign: "left", padding: "6px 8px", borderBottom: "2px solid var(--rm-border)", fontSize: 11, textTransform: "uppercase", color: "var(--rm-muted)" }}>{c.label || c.key}</th>)}</tr></thead>
                  <tbody>
                    {preview.rows.slice(0, 50).map((row, i) => (
                      <tr key={i} style={{ borderBottom: "1px solid var(--rm-border)" }}>
                        {draft.columns.map((c) => <td key={c.path} style={{ padding: "6px 8px" }}>{String(row[c.key] ?? "—")}</td>)}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <div style={{ fontSize: 13, color: "var(--rm-muted)" }}>Run a preview to see rows.</div>}
          </Card>

          <Card>
            <SectionTitle right={<label style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 13 }}>
              <input type="checkbox" checked={draft.schedule?.enabled ?? false} onChange={(e) => patch({ schedule: { ...draft.schedule, enabled: e.target.checked } })} /> Scheduled
            </label>}>Scheduled email delivery</SectionTitle>
            <div style={{ display: "grid", gridTemplateColumns: "160px 1fr auto", gap: 10, alignItems: "end" }}>
              <Field label="Cron (in report TZ)"><Input value={draft.schedule?.cron ?? ""} onChange={(e) => patch({ schedule: { ...draft.schedule, cron: e.target.value } })} placeholder="0 9 * * *" /></Field>
              <Field label="Recipients (comma-separated)"><Input value={(draft.schedule?.recipients ?? []).join(", ")} onChange={(e) => patch({ schedule: { ...draft.schedule, recipients: e.target.value.split(",").map((s) => s.trim()).filter(Boolean) } })} placeholder="ops@acme.com, risk@acme.com" /></Field>
              <Button variant="secondary" onClick={sendNow}><Send size={14} /> Send now</Button>
            </div>
            {email && !email.configured ? <div style={{ fontSize: 12, color: "var(--rm-warning)", marginTop: 10 }}>No SMTP configured — scheduled reports are generated and stored until email is set up (right).</div> : null}
          </Card>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card>
            <SectionTitle>Saved reports</SectionTitle>
            {reports.length === 0 ? <EmptyState icon={<FileBarChart size={20} />} title="No reports yet" hint="Build one and save it." /> : (
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                {reports.map((r) => (
                  <div key={r.id} style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, padding: "8px 10px", borderRadius: 8, border: "1px solid var(--rm-border)", background: draft.id === r.id ? "var(--rm-accent-bg)" : "transparent" }}>
                    <button onClick={() => { void apiJson<Report>(apiBaseUrl, `/api/v1/reports/${r.id}`, {}, apiKey).then((full) => { setDraft({ ...emptyReport(), ...full, schedule: full.schedule ?? { enabled: false, cron: "0 9 * * *", recipients: [] } }); setPreview(null); }); }}
                      style={{ background: "none", border: "none", textAlign: "left", cursor: "pointer", color: "var(--rm-text)", fontSize: 13, fontWeight: 600, flex: 1 }}>
                      {r.name}
                      <span style={{ display: "block", fontSize: 11, color: "var(--rm-muted)", fontWeight: 400 }}>{r.schedule?.enabled ? `⏱ ${r.schedule.cron}` : "manual"} · {r.timezone}</span>
                    </button>
                    <button onClick={() => { void apiJson(apiBaseUrl, `/api/v1/reports/${r.id}`, { method: "DELETE" }, apiKey).then(load); }} title="Delete" style={{ background: "none", border: "none", cursor: "pointer", color: "var(--rm-muted)" }}><Trash2 size={14} /></button>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card>
            <SectionTitle right={email?.configured ? <Badge tone="success">configured</Badge> : <Badge tone="warning">not set</Badge>}><span style={{ display: "flex", gap: 6, alignItems: "center" }}><Mail size={15} /> Email (SMTP)</span></SectionTitle>
            <Field label="SMTP host"><Input value={email?.host ?? ""} onChange={(e) => setEmail({ ...email, host: e.target.value })} placeholder="smtp.sendgrid.net" /></Field>
            <div style={{ height: 8 }} />
            <Field label="From address"><Input value={email?.from_addr ?? ""} onChange={(e) => setEmail({ ...email, from_addr: e.target.value })} placeholder="reports@acme.com" /></Field>
            <div style={{ height: 8 }} />
            <Field label="Username"><Input value={email?.username ?? ""} onChange={(e) => setEmail({ ...email, username: e.target.value })} placeholder="apikey" /></Field>
            <div style={{ height: 8 }} />
            <Field label={email?.password_set ? "Replace password" : "Password / API key"} hint={email?.password_set ? "A password is stored (encrypted)." : "Stored encrypted at rest."}>
              <Input type="password" value={emailPw} onChange={(e) => setEmailPw(e.target.value)} placeholder="••••••••" />
            </Field>
            <div style={{ height: 10 }} />
            <Button variant="secondary" onClick={saveEmail}>Save email settings</Button>
          </Card>
        </div>
      </div>
    </div>
  );
}
