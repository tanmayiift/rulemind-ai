"use client";


import * as React from "react";
import { useRouter } from "next/navigation";
import { flushSync } from "react-dom";
import {
  ArrowDown,
  ArrowUp,
  CheckCircle2,
  CircleHelp,
  CircleSlash,
  Eye,
  FileSearch,
  GitBranch,
  Layers,
  Plus,
  Trash2,
  Workflow,
  XCircle,
  type LucideIcon,
} from "lucide-react";
import { apiJson, apiText, streamDecisions, type StreamedDecision } from "../../lib/api";
import { useRuleMindStore } from "../../lib/store";
import { ENVIRONMENT_ACCENT, THEMES, type ThemeTokens } from "../theme";
import { ConnectorIcon } from "../icons";
import type {
  AuditErrorRecord,
  BootstrapPayload,
  ConnectorRecord,
  DeployStatusPayload,
  ExecutionTraceStepRecord,
  PolicyExecuteResponse,
  PolicyRecord,
  PolicyStepRecord,
  PromotionRecord,
  RuleConditionResult,
  RuleNodeRecord,
  RuleRecord,
  RuleTreeNodeRecord,
  RuleTestResponse,
  ScorecardBinRecord,
  ScorecardRangeRecord,
  ScorecardRecord,
  SettingsRecord,
  DataProtection,
  SloConfig,
  SloStatus,
  VariableBatchTestResponse,
  VariableGraphResponse,
  VariableRecord,
} from "../types";



import {
  PageId,
  PAGE_META,
  NODE_TYPES,
  STATUS_ORDER,
  CATEGORY_ORDER,
  useTheme,
  useBootstrapData,
  statusColorKey,
  statusLabel,
  tone,
  toRuleNodeLabel,
  cloneNode,
  connectorLabel,
  sourceMark,
  useFilteredVariables,
  StatCard,
  Button,
  StatusBadge,
  InlineInput,
  InlineSelect,
  InlineTextarea,
  SectionHeader,
  InfoBanner,
  EmptyState,
} from "../kit";

/* EVERY FIELD GETS A LABEL, AND EVERY GROUP GETS A HEADING.
 *
 * The four cards below (API, Engine, Sources, Audit) used to render bare inputs whose only
 * identification was a `placeholder`. A placeholder is not a label: it disappears the moment
 * you type, so a filled-in form became eight unlabelled boxes and you could not tell which one
 * held the timeout and which held the memory limit without clearing it. Screen readers saw the
 * same thing -- textboxes with no accessible name.
 *
 * The group names ("API", "Engine", …) were bare <div>s styled to look like headings, so the
 * document outline was flat: one <h1> and then nothing, for a page of twelve settings.
 *
 * The sections further down this same file (Data protection, SLO) already did it properly, with
 * <label> wrapping a <span> caption. That is the pattern; these two helpers just make it the
 * cheap option so the next section added here does not drift back.
 */
function SettingField(props: { label: string; hint?: string; children: React.ReactNode; muted: string }) {
  return (
    <label style={{ display: "grid", gap: 4 }}>
      <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: props.muted, fontWeight: 700 }}>
        {props.label}
      </span>
      {props.children}
      {props.hint ? <span style={{ fontSize: 11, color: props.muted, lineHeight: 1.45 }}>{props.hint}</span> : null}
    </label>
  );
}

function SettingGroup(props: { title: string; description?: string; children: React.ReactNode; theme: ThemeTokens }) {
  const { theme } = props;
  return (
    <section style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 16, display: "grid", gap: 12, alignContent: "start" }}>
      <div style={{ display: "grid", gap: 3 }}>
        <h2 style={{ margin: 0, fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>
          {props.title}
        </h2>
        {props.description ? (
          <p style={{ margin: 0, fontSize: 11.5, color: theme.muted, lineHeight: 1.5 }}>{props.description}</p>
        ) : null}
      </div>
      {props.children}
    </section>
  );
}

export function SettingsPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, themeMode, setThemeMode, isMobile } = useRuleMindStore();
  const [settings, setSettings] = React.useState<SettingsRecord>(props.data.settings);
  const [busy, setBusy] = React.useState(false);
  const [dp, setDp] = React.useState<DataProtection | null>(null);
  const [dpKeys, setDpKeys] = React.useState("");
  const [dpBusy, setDpBusy] = React.useState(false);

  React.useEffect(() => {
    setSettings(props.data.settings);
  }, [props.data.settings]);

  const loadDp = React.useCallback(async () => {
    try {
      const view = await apiJson<DataProtection>(apiBaseUrl, "/api/v1/settings/data-protection", {}, apiKey);
      setDp(view);
      setDpKeys((view.pii_redact_keys ?? []).join(", "));
    } catch {
      /* read may be limited for this role */
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void loadDp(); }, [loadDp]);

  const saveDp = React.useCallback(async () => {
    if (!dp) return;
    setDpBusy(true);
    try {
      const keys = dpKeys.split(",").map((k) => k.trim()).filter(Boolean);
      const view = await apiJson<DataProtection>(apiBaseUrl, "/api/v1/settings/data-protection", {
        method: "PUT",
        body: JSON.stringify({ retention_days: dp.retention_days, pii_redact_keys: keys }),
      }, apiKey);
      setDp(view);
      setDpKeys((view.pii_redact_keys ?? []).join(", "));
      props.onNotify("Data-protection settings saved.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Data-protection save failed.");
    } finally {
      setDpBusy(false);
    }
  }, [apiBaseUrl, apiKey, dp, dpKeys, props]);

  const [slo, setSlo] = React.useState<SloConfig | null>(null);
  const [sloStatus, setSloStatus] = React.useState<SloStatus | null>(null);
  const [sloBusy, setSloBusy] = React.useState(false);

  const loadSlo = React.useCallback(async () => {
    try {
      const [cfg, status] = await Promise.all([
        apiJson<SloConfig>(apiBaseUrl, "/api/v1/settings/slo", {}, apiKey),
        apiJson<SloStatus>(apiBaseUrl, "/api/v1/slo/status", {}, apiKey),
      ]);
      setSlo(cfg);
      setSloStatus(status);
    } catch {
      /* read may be limited for this role */
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void loadSlo(); }, [loadSlo]);

  const saveSlo = React.useCallback(async () => {
    if (!slo) return;
    setSloBusy(true);
    try {
      const cfg = await apiJson<SloConfig>(apiBaseUrl, "/api/v1/settings/slo", { method: "PUT", body: JSON.stringify(slo) }, apiKey);
      setSlo(cfg);
      try { setSloStatus(await apiJson<SloStatus>(apiBaseUrl, "/api/v1/slo/status", {}, apiKey)); } catch { /* status best-effort */ }
      props.onNotify("SLO objective saved.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "SLO save failed.");
    } finally {
      setSloBusy(false);
    }
  }, [apiBaseUrl, apiKey, slo, props]);

  const saveSettings = React.useCallback(async () => {
    setBusy(true);
    try {
      const response = await apiJson<SettingsRecord>(apiBaseUrl, "/api/v1/settings", { method: "PUT", body: JSON.stringify(settings) }, apiKey);
      setSettings(response);
      if (response.theme_mode && response.theme_mode !== themeMode) {
        setThemeMode(response.theme_mode);
      }
      props.refresh();
      props.onNotify("Settings saved.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Settings save failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, setThemeMode, settings, themeMode]);

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <SectionHeader title={PAGE_META.settings.title} subtitle={PAGE_META.settings.subtitle} actions={<Button variant="primary" onClick={saveSettings} disabled={busy} testId="settings-save">Save settings</Button>} />
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "repeat(2, minmax(0, 1fr))", gap: 14, alignItems: "start" }}>
        <SettingGroup theme={theme} title="API" description="Where this workspace sends decisions, and how it authenticates.">
          <SettingField muted={theme.muted} label="Base URL" hint="The API this dashboard calls. Changing it here does not move the backend.">
            <InlineInput value={String(settings.api_base_url ?? "")} onChange={(event) => setSettings((current) => ({ ...current, api_base_url: event.target.value }))} placeholder="https://api.example.com" />
          </SettingField>
          <SettingField muted={theme.muted} label="Authentication" hint="AUTH_MODE unset is not the same as none — leaving it unset leaves auth ON.">
            <InlineSelect value={String(settings.auth_config?.method ?? "none")} onChange={(event) => setSettings((current) => ({ ...current, auth_config: { ...current.auth_config, method: event.target.value } }))}>
              <option value="none">No auth</option>
              <option value="apikey">API key</option>
              <option value="jwt">JWT</option>
            </InlineSelect>
          </SettingField>
          <SettingField muted={theme.muted} label="Decision endpoint">
            <InlineInput value={String(settings.auth_config?.decision_endpoint ?? "/api/v1/decide")} onChange={(event) => setSettings((current) => ({ ...current, auth_config: { ...current.auth_config, decision_endpoint: event.target.value } }))} placeholder="/api/v1/decide" />
          </SettingField>
        </SettingGroup>

        <SettingGroup theme={theme} title="Engine" description="Limits applied to every Python variable this workspace evaluates.">
          <SettingField muted={theme.muted} label="Python version">
            <InlineInput value={String(settings.engine_config?.python_version ?? "3.9")} onChange={(event) => setSettings((current) => ({ ...current, engine_config: { ...current.engine_config, python_version: event.target.value } }))} placeholder="3.11" />
          </SettingField>
          <SettingField muted={theme.muted} label="Timeout (ms)" hint="Per VARIABLE, not per decision. A policy reading 50 variables can take far longer than this.">
            <InlineInput type="number" value={String(settings.engine_config?.timeout_ms ?? 2000)} onChange={(event) => setSettings((current) => ({ ...current, engine_config: { ...current.engine_config, timeout_ms: Number(event.target.value || 0) } }))} placeholder="2000" />
          </SettingField>
          <SettingField muted={theme.muted} label="Memory (MB)" hint="Applied per sandbox worker at spawn, uniformly across tenants.">
            <InlineInput type="number" value={String(settings.engine_config?.memory_mb ?? 128)} onChange={(event) => setSettings((current) => ({ ...current, engine_config: { ...current.engine_config, memory_mb: Number(event.target.value || 0) } }))} placeholder="128" />
          </SettingField>
        </SettingGroup>

        <SettingGroup theme={theme} title="Sources" description="Defaults applied to new data connectors.">
          <SettingField muted={theme.muted} label="Default format">
            <InlineInput value={String(settings.source_defaults?.default_format ?? "json")} onChange={(event) => setSettings((current) => ({ ...current, source_defaults: { ...current.source_defaults, default_format: event.target.value } }))} placeholder="json" />
          </SettingField>
          <SettingField muted={theme.muted} label="Batch support">
            <InlineSelect value={String(settings.source_defaults?.batch_support ?? "enabled")} onChange={(event) => setSettings((current) => ({ ...current, source_defaults: { ...current.source_defaults, batch_support: event.target.value } }))}>
              <option value="enabled">Batch enabled</option>
              <option value="disabled">Batch disabled</option>
            </InlineSelect>
          </SettingField>
          <SettingField muted={theme.muted} label="Webhook URL" hint="Where connector callbacks are delivered. Leave blank if unused.">
            <InlineInput value={String(settings.source_defaults?.webhook_url ?? "")} onChange={(event) => setSettings((current) => ({ ...current, source_defaults: { ...current.source_defaults, webhook_url: event.target.value } }))} placeholder="https://example.com/hooks/rulemind" />
          </SettingField>
        </SettingGroup>

        <SettingGroup theme={theme} title="Audit" description="What is recorded about each decision, and for how long.">
          <SettingField muted={theme.muted} label="Decision logging">
            <InlineSelect value={String(settings.source_defaults?.logging ?? "enabled")} onChange={(event) => setSettings((current) => ({ ...current, source_defaults: { ...current.source_defaults, logging: event.target.value } }))}>
              <option value="enabled">Logging enabled</option>
              <option value="disabled">Logging disabled</option>
            </InlineSelect>
          </SettingField>
          <SettingField muted={theme.muted} label="Audit retention (days)" hint="Separate from the decision retention window under Data protection below.">
            <InlineInput type="number" value={String(settings.audit_retention_days ?? 90)} onChange={(event) => setSettings((current) => ({ ...current, audit_retention_days: Number(event.target.value || 0) }))} placeholder="90" />
          </SettingField>
        </SettingGroup>

        {/* THE THEME CONTROL WAS INSIDE "AUDIT". It is not an audit setting, it is not even a
          * workspace setting in the same sense -- it is how this dashboard looks. Sitting third
          * in a card about decision logging and retention, unlabelled, it read as a third audit
          * option. Its own group, named for what it is. */}
        <SettingGroup theme={theme} title="Appearance" description="How this dashboard renders. Stored with the workspace, so it follows you between browsers.">
          <SettingField muted={theme.muted} label="Theme">
            <InlineSelect value={settings.theme_mode ?? "light"} onChange={(event) => setSettings((current) => ({ ...current, theme_mode: event.target.value as "light" | "dark" }))}>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
            </InlineSelect>
          </SettingField>
        </SettingGroup>
      </div>

      {dp ? (
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 16, display: "grid", gap: 14 }} data-testid="data-protection">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
            <div>
              <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Data protection</div>
              <div style={{ fontSize: 12.5, color: theme.muted, marginTop: 2 }}>Retention window, PII redaction, and at-rest encryption for this workspace&apos;s decision log.</div>
            </div>
            <Button variant="primary" onClick={saveDp} disabled={dpBusy} testId="data-protection-save">Save data protection</Button>
          </div>

          <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "repeat(3, minmax(0, 1fr))", gap: 12 }}>
            {(() => {
              const enc = dp.encryption_at_rest;
              const encTone = enc ? tone(theme, "success") : tone(theme, "warning");
              return (
                <div style={{ border: "1px solid " + theme.border, borderRadius: 10, padding: 12, display: "grid", gap: 6 }}>
                  <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Encryption at rest</div>
                  <span style={{ justifySelf: "start", fontSize: 12, fontWeight: 700, padding: "3px 10px", borderRadius: 999, background: encTone.bg, color: encTone.fg }}>
                    {enc ? "Enabled (Fernet)" : "Off"}
                  </span>
                  <div style={{ fontSize: 11.5, color: theme.muted }}>Set by server config (DECISION_ENCRYPT_AT_REST). Transit is TLS-enforced.</div>
                </div>
              );
            })()}

            <div style={{ border: "1px solid " + theme.border, borderRadius: 10, padding: 12, display: "grid", gap: 6 }}>
              <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Archive sink</div>
              <span style={{ justifySelf: "start", fontSize: 12, fontWeight: 700, padding: "3px 10px", borderRadius: 999, background: tone(theme, "accent").bg, color: tone(theme, "accent").fg }}>
                {dp.archive_sink || "none"}
              </span>
              <div style={{ fontSize: 11.5, color: theme.muted }}>Where expired decisions are archived before purge (ClickHouse / S3 / none).</div>
            </div>

            <div style={{ border: "1px solid " + theme.border, borderRadius: 10, padding: 12, display: "grid", gap: 6 }}>
              <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Retention window (days)</div>
              <InlineInput aria-label="Decision retention period in days" type="number" value={String(dp.retention_days)} onChange={(event) => setDp((current) => (current ? { ...current, retention_days: Math.max(1, Number(event.target.value || 0)) } : current))} testId="data-protection-retention" />
              <div style={{ fontSize: 11.5, color: theme.muted }}>Decisions older than this are archived (if a sink is set) then purged. Minimum 1 day.</div>
            </div>
          </div>

          <div style={{ display: "grid", gap: 6 }}>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Redacted PII fields (comma-separated)</div>
            {/* The caption above is a sibling <div>, so it labels this field visually and not
              * programmatically -- a screen reader announced an unnamed textbox. aria-label is
              * the minimal association that does not restructure the block around it. */}
            <InlineInput aria-label="Redacted PII fields, comma-separated" value={dpKeys} onChange={(event) => setDpKeys(event.target.value)} placeholder="account_no, member_id, tax_id" testId="data-protection-pii" />
            <div style={{ fontSize: 11.5, color: theme.muted }}>
              These payload keys are masked to <code style={{ fontFamily: "monospace" }}>***</code> in stored decision logs, on top of built-ins ({(dp.builtin_redact_keys ?? []).join(", ") || "none"}){dp.env_redact_keys && dp.env_redact_keys.length ? ` and server keys (${dp.env_redact_keys.join(", ")})` : ""}.
            </div>
          </div>
        </div>
      ) : null}

      {slo ? (
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 16, display: "grid", gap: 14 }} data-testid="slo">
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", flexWrap: "wrap", gap: 8 }}>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Service level & drift</div>
                {sloStatus ? (() => {
                  const t = !sloStatus.enabled ? tone(theme, "accent") : sloStatus.healthy ? tone(theme, "success") : tone(theme, "danger");
                  const label = !sloStatus.enabled ? "Disabled" : sloStatus.healthy ? "Meeting SLO" : `${sloStatus.breaches.length} breach${sloStatus.breaches.length === 1 ? "" : "es"}`;
                  return <span style={{ fontSize: 12, fontWeight: 700, padding: "3px 10px", borderRadius: 999, background: t.bg, color: t.fg }}>{label}</span>;
                })() : null}
              </div>
              <div style={{ fontSize: 12.5, color: theme.muted, marginTop: 2 }}>Latency/error objectives and an outcome-drift guard, evaluated on a schedule and exported to Prometheus for alerting.</div>
            </div>
            <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
              <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 13, cursor: "pointer", color: theme.text }}>
                <input type="checkbox" checked={slo.enabled} onChange={(event) => setSlo((current) => (current ? { ...current, enabled: event.target.checked } : current))} /> Enabled
              </label>
              <Button variant="primary" onClick={saveSlo} disabled={sloBusy} testId="slo-save">Save SLO</Button>
            </div>
          </div>

          {sloStatus && sloStatus.metrics.sample > 0 ? (
            <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr 1fr" : "repeat(4, minmax(0, 1fr))", gap: 10 }}>
              {[
                { label: "p95 latency", value: `${sloStatus.metrics.p95_latency_ms} ms`, breached: sloStatus.breaches.some((b) => b.type === "latency_p95") },
                { label: "Error rate", value: `${sloStatus.metrics.error_rate_pct}%`, breached: sloStatus.breaches.some((b) => b.type === "error_rate") },
                { label: "Approval rate", value: `${sloStatus.metrics.approval_rate_pct}%`, breached: sloStatus.breaches.some((b) => b.type.startsWith("approval_rate")) },
                { label: `Drift (of ${sloStatus.drift.threshold})`, value: sloStatus.drift.measurable ? String(sloStatus.drift.distance) : "n/a", breached: sloStatus.breaches.some((b) => b.type === "outcome_drift") },
              ].map((stat) => (
                <div key={stat.label} style={{ border: "1px solid " + (stat.breached ? theme.danger : theme.border), borderRadius: 10, padding: "10px 12px", display: "grid", gap: 3 }}>
                  <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>{stat.label}</div>
                  <div style={{ fontSize: 18, fontWeight: 700, color: stat.breached ? theme.danger : theme.text }}>{stat.value}</div>
                </div>
              ))}
            </div>
          ) : (
            <div style={{ fontSize: 12.5, color: theme.muted }}>No decisions in the recent {slo.recent_hours}h window yet — metrics appear once traffic flows.</div>
          )}

          <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "repeat(3, minmax(0, 1fr))", gap: 12 }}>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>p95 latency ceiling (ms)</span>
              <InlineInput type="number" value={String(slo.latency_p95_ms)} onChange={(event) => setSlo((current) => (current ? { ...current, latency_p95_ms: Number(event.target.value || 0) } : current))} />
            </label>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Error-rate ceiling (%)</span>
              <InlineInput type="number" value={String(slo.error_rate_pct)} onChange={(event) => setSlo((current) => (current ? { ...current, error_rate_pct: Number(event.target.value || 0) } : current))} />
            </label>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Outcome-drift limit (0–1)</span>
              <InlineInput type="number" value={String(slo.drift_threshold)} onChange={(event) => setSlo((current) => (current ? { ...current, drift_threshold: Number(event.target.value || 0) } : current))} />
            </label>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Min approval rate (%, blank = off)</span>
              <InlineInput type="number" value={slo.min_approval_rate_pct ?? ""} onChange={(event) => setSlo((current) => (current ? { ...current, min_approval_rate_pct: event.target.value === "" ? null : Number(event.target.value) } : current))} />
            </label>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Max approval rate (%, blank = off)</span>
              <InlineInput type="number" value={slo.max_approval_rate_pct ?? ""} onChange={(event) => setSlo((current) => (current ? { ...current, max_approval_rate_pct: event.target.value === "" ? null : Number(event.target.value) } : current))} />
            </label>
            <label style={{ display: "grid", gap: 4 }}>
              <span style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Recent window (hours)</span>
              <InlineInput type="number" value={String(slo.recent_hours)} onChange={(event) => setSlo((current) => (current ? { ...current, recent_hours: Math.max(1, Number(event.target.value || 1)) } : current))} />
            </label>
          </div>

          {sloStatus && sloStatus.breaches.length ? (
            <div style={{ display: "grid", gap: 6 }}>
              {sloStatus.breaches.map((b) => (
                <div key={b.type} style={{ fontSize: 12.5, color: theme.danger, display: "flex", alignItems: "center", gap: 8 }}>
                  <span style={{ width: 6, height: 6, borderRadius: 999, background: theme.danger, display: "inline-block" }} /> {b.message}
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

