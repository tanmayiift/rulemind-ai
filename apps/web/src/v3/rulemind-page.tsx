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
import { apiJson, apiText, streamDecisions, type StreamedDecision } from "../lib/api";
import { useRuleMindStore } from "../lib/store";
import { ENVIRONMENT_ACCENT, THEMES, type ThemeTokens } from "./theme";
import { ConnectorIcon } from "./icons";
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
  VariableBatchTestResponse,
  VariableGraphResponse,
  VariableRecord,
} from "./types";


type PageId =
  | "dashboard"
  | "connectors"
  | "variables"
  | "rules"
  | "scorecards"
  | "policies"
  | "testing"
  | "deploy"
  | "audit"
  | "exports"
  | "settings";

const PAGE_META: Record<PageId, { title: string; subtitle: string }> = {
  dashboard: { title: "Dashboard", subtitle: "System overview, source health, and underwriting flow coverage." },
  connectors: { title: "Connectors", subtitle: "Toggle data sources, inspect schemas, and manage sample payloads." },
  variables: { title: "Variables", subtitle: "Create sandboxed Python features from any connected JSON payload." },
  rules: { title: "Rules", subtitle: "Build click-to-add decisions across sources with inline expression preview." },
  scorecards: { title: "Scorecards", subtitle: "Assign points to variable ranges and preview live sample outcomes." },
  policies: { title: "Policies", subtitle: "Chain connectors, rules, and scorecards into a full decision flow." },
  testing: { title: "Test Console", subtitle: "Run variables, rules, and policies against the active sample payloads." },
  deploy: { title: "Deploy", subtitle: "Promote assets through DEV, UAT, and PROD with test-gated controls." },
  audit: { title: "Audit Logs", subtitle: "Inspect decision history, promotion history, and operational error events." },
  exports: { title: "Exports", subtitle: "Preview, download, and import full RuleMind configurations." },
  settings: { title: "Settings", subtitle: "Persist API, engine, source, and audit configuration." },
};

const NODE_TYPES: ReadonlyArray<{ type: RuleNodeRecord["type"]; label: string; icon: LucideIcon; colorKey: "accent" | "success" | "warning" | "danger" }> =
  [
    { type: "condition", label: "Condition", icon: CircleHelp, colorKey: "accent" },
    { type: "and", label: "AND", icon: GitBranch, colorKey: "success" },
    { type: "or", label: "OR", icon: Layers, colorKey: "warning" },
    { type: "approve", label: "Approve", icon: CheckCircle2, colorKey: "success" },
    { type: "review", label: "Review", icon: Eye, colorKey: "warning" },
    { type: "reject", label: "Reject", icon: XCircle, colorKey: "danger" },
  ];

const STATUS_ORDER = ["dev", "uat", "prod"] as const;
const CATEGORY_ORDER = ["Bureau", "Banking", "Business", "Device", "Identity", "Custom"] as const;

function useTheme(): ThemeTokens {
  const themeMode = useRuleMindStore((state) => state.themeMode);
  return THEMES[themeMode];
}

function useBootstrapData() {
  const apiBaseUrl = useRuleMindStore((state) => state.apiBaseUrl);
  const apiKey = useRuleMindStore((state) => state.apiKey);
  const [refreshKey, setRefreshKey] = React.useState(0);
  const [data, setData] = React.useState<BootstrapPayload | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const dataRef = React.useRef<BootstrapPayload | null>(null);

  React.useEffect(() => {
    let mounted = true;
    if (!dataRef.current) {
      setLoading(true);
    }
    setError(null);
    apiJson<BootstrapPayload>(apiBaseUrl, "/api/v1/bootstrap", {}, apiKey)
      .then((payload) => {
        if (mounted) {
          dataRef.current = payload;
          setData(payload);
        }
      })
      .catch((reason: unknown) => {
        if (mounted) {
          setError(reason instanceof Error ? reason.message : "Unable to load RuleMind data.");
        }
      })
      .finally(() => {
        if (mounted) {
          setLoading(false);
        }
      });
    return () => {
      mounted = false;
    };
  }, [apiBaseUrl, apiKey, refreshKey]);

  const refresh = React.useCallback(() => setRefreshKey((value) => value + 1), []);
  return { apiBaseUrl, apiKey, data, loading, error, refresh };
}

function statusColorKey(status: string): "purple" | "warning" | "success" {
  if (status === "prod") {
    return "success";
  }
  if (status === "uat") {
    return "warning";
  }
  return "purple";
}

function statusLabel(status: string): string {
  return status.toUpperCase();
}

function tone(theme: ThemeTokens, toneKey: "accent" | "success" | "warning" | "danger" | "purple") {
  const mapping = {
    accent: { fg: theme.accent, bg: theme.accentBg },
    success: { fg: theme.success, bg: theme.successBg },
    warning: { fg: theme.warning, bg: theme.warningBg },
    danger: { fg: theme.danger, bg: theme.dangerBg },
    purple: { fg: theme.purple, bg: theme.purpleBg },
  };
  return mapping[toneKey];
}

function toRuleNodeLabel(type: RuleNodeRecord["type"]): string {
  return NODE_TYPES.find((item) => item.type === type)?.label ?? type;
}

function cloneNode(type: RuleNodeRecord["type"], defaultVariable?: VariableRecord): RuleNodeRecord {
  return {
    id: "node_" + Date.now() + "_" + Math.random().toString(16).slice(2, 8),
    type,
    label: toRuleNodeLabel(type),
    variable: type === "condition" ? defaultVariable?.id : undefined,
    operator: type === "condition" ? ">=" : undefined,
    value: type === "condition" ? "" : undefined,
  };
}

function connectorLabel(connector: ConnectorRecord | undefined): string {
  return connector ? connector.name : "Unknown source";
}

function sourceMark(connector: ConnectorRecord | undefined, label?: string) {
  if (!connector) {
    return label ?? "Unknown";
  }
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
      <ConnectorIcon connectorId={connector.id} color={connector.color} />
      <span>{label ?? connector.name}</span>
    </span>
  );
}

function useFilteredVariables(variables: VariableRecord[], connectors: ConnectorRecord[]) {
  const environment = useRuleMindStore((state) => state.environment);
  const activeConnectorFilter = useRuleMindStore((state) => state.activeConnectorFilter);
  const connectorMap = React.useMemo(
    () => Object.fromEntries(connectors.map((connector) => [connector.id, connector])),
    [connectors]
  );

  return React.useMemo(() => {
    return variables.filter((variable) => {
      if (variable.status !== environment) {
        return false;
      }
      if (activeConnectorFilter !== "all" && variable.source_id !== activeConnectorFilter) {
        return false;
      }
      return Boolean(connectorMap[variable.source_id]);
    });
  }, [activeConnectorFilter, connectorMap, environment, variables]);
}

function StatCard(props: { label: string; value: string; hint: string; accent: string; onClick?: () => void; testId?: string }) {
  const theme = useTheme();
  return (
    <button
      type="button"
      onClick={props.onClick}
      data-testid={props.testId}
      style={{
        background: theme.card,
        border: "1px solid " + theme.border,
        borderRadius: 12,
        padding: 16,
        display: "grid",
        gap: 6,
        textAlign: "left",
        cursor: props.onClick ? "pointer" : "default",
      }}
    >
      <span style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted, fontWeight: "var(--rm-fw-semibold)" as unknown as number, letterSpacing: 0.4, textTransform: "uppercase" }}>{props.label}</span>
      <span style={{ fontSize: "var(--rm-fs-hero)", color: props.accent, fontWeight: "var(--rm-fw-bold)" as unknown as number }}>{props.value}</span>
      <span style={{ fontSize: "var(--rm-fs-small)", color: theme.dim }}>{props.hint}</span>
    </button>
  );
}

function Button(props: {
  children: React.ReactNode;
  onClick?: () => void;
  variant?: "primary" | "default" | "ghost" | "danger" | "success";
  small?: boolean;
  disabled?: boolean;
  testId?: string;
  type?: "button" | "submit";
}) {
  const theme = useTheme();
  const palette = {
    primary: { background: theme.accent, color: theme.inverseText, border: "1px solid " + theme.accent },
    default: { background: theme.card, color: theme.text, border: "1px solid " + theme.border },
    ghost: { background: "transparent", color: theme.muted, border: "1px solid transparent" },
    danger: { background: theme.dangerBg, color: theme.danger, border: "1px solid transparent" },
    success: { background: theme.successBg, color: theme.success, border: "1px solid transparent" },
  }[props.variant ?? "default"];

  return (
    <button
      type={props.type ?? "button"}
      onClick={props.onClick}
      data-testid={props.testId}
      disabled={props.disabled}
      style={{
        background: palette.background,
        color: palette.color,
        border: palette.border,
        borderRadius: 8,
        padding: props.small ? "6px 10px" : "8px 14px",
        fontFamily: "inherit",
        fontSize: props.small ? "var(--rm-fs-small)" : "var(--rm-fs-body)",
        fontWeight: "var(--rm-fw-bold)" as unknown as number,
        cursor: props.disabled ? "not-allowed" : "pointer",
        opacity: props.disabled ? 0.45 : 1,
        whiteSpace: "nowrap",
      }}
    >
      {props.children}
    </button>
  );
}

function StatusBadge(props: { status: string; testId?: string }) {
  const theme = useTheme();
  const style = tone(theme, statusColorKey(props.status));
  return (
    <span
      data-testid={props.testId}
      style={{
        display: "inline-flex",
        alignItems: "center",
        padding: "2px 8px",
        borderRadius: 999,
        background: style.bg,
        color: style.fg,
        fontSize: "var(--rm-fs-caption)",
        fontWeight: "var(--rm-fw-bold)" as unknown as number,
        textTransform: "uppercase",
        letterSpacing: 0.6,
      }}
    >
      {statusLabel(props.status)}
    </span>
  );
}

function InlineInput(props: React.InputHTMLAttributes<HTMLInputElement> & { testId?: string; "data-testid"?: string }) {
  const theme = useTheme();
  return (
    <input
      {...props}
      data-testid={props.testId ?? props["data-testid"]}
      style={{
        ...(props.style ?? {}),
        background: theme.input,
        border: "1px solid " + theme.border,
        color: theme.text,
        borderRadius: 8,
        padding: "8px 10px",
        outline: "none",
        fontFamily: "inherit",
      }}
    />
  );
}

function InlineSelect(props: React.SelectHTMLAttributes<HTMLSelectElement> & { testId?: string; "data-testid"?: string }) {
  const theme = useTheme();
  return (
    <select
      {...props}
      data-testid={props.testId ?? props["data-testid"]}
      style={{
        ...(props.style ?? {}),
        background: theme.input,
        border: "1px solid " + theme.border,
        color: theme.text,
        borderRadius: 8,
        padding: "8px 10px",
        outline: "none",
        fontFamily: "inherit",
      }}
    />
  );
}

function InlineTextarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement> & { testId?: string; "data-testid"?: string; code?: boolean }) {
  const theme = useTheme();
  return (
    <textarea
      {...props}
      data-testid={props.testId ?? props["data-testid"]}
      style={{
        ...(props.style ?? {}),
        background: props.code ? theme.editor : theme.input,
        border: "1px solid " + theme.border,
        color: props.code ? theme.codeText : theme.text,
        borderRadius: 10,
        padding: "10px 12px",
        outline: "none",
        fontFamily: props.code ? "var(--font-mono)" : "inherit",
      }}
    />
  );
}

function SectionHeader(props: { title: string; subtitle?: string; actions?: React.ReactNode }) {
  const theme = useTheme();
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", gap: 12, marginBottom: 16 }}>
      <div>
        <h2 style={{ margin: 0, color: theme.text, fontSize: "var(--rm-fs-title)", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>{props.title}</h2>
        {props.subtitle ? <p style={{ margin: "6px 0 0", color: theme.muted, fontSize: "var(--rm-fs-body)" }}>{props.subtitle}</p> : null}
      </div>
      {props.actions}
    </div>
  );
}

function InfoBanner(props: { message: string; toneKey?: "accent" | "warning" | "danger" | "success" }) {
  const theme = useTheme();
  const variant = tone(theme, props.toneKey ?? "accent");
  return (
    <div style={{ marginBottom: 16, padding: "10px 12px", borderRadius: 10, background: variant.bg, color: variant.fg, fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-semibold)" as unknown as number }}>
      {props.message}
    </div>
  );
}

function EmptyState(props: { icon: React.ReactNode; title: string; description: string }) {
  const theme = useTheme();
  return (
    <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 36, textAlign: "center" }}>
      <div style={{ fontSize: "var(--rm-fs-hero)", marginBottom: 6, display: "grid", placeItems: "center", color: theme.dim }}>{props.icon}</div>
      <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>{props.title}</div>
      <div style={{ fontSize: "var(--rm-fs-body)", color: theme.muted, marginTop: 6 }}>{props.description}</div>
    </div>
  );
}

function DashboardPage(props: { data: BootstrapPayload }) {
  const theme = useTheme();
  const router = useRouter();
  const isMobile = useRuleMindStore((state) => state.isMobile);
  const activeSources = props.data.connectors.filter((connector) => connector.is_active).length;
  const prodVariables = props.data.variables.filter((variable) => variable.status === "prod").length;
  const prodRules = props.data.rules.filter((rule) => rule.status === "prod").length;

  return (
    <div style={{ padding: isMobile ? 12 : 20 }}>
      <SectionHeader title={PAGE_META.dashboard.title} subtitle={PAGE_META.dashboard.subtitle} />
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "repeat(2, minmax(0, 1fr))" : "repeat(4, minmax(0, 1fr))", gap: 12, marginBottom: 18 }}>
        <StatCard label="Sources" value={activeSources + "/" + props.data.connectors.length} hint="Active / total connectors" accent={theme.accent} onClick={() => router.push("/connectors")} />
        <StatCard label="Variables" value={String(props.data.variables.length)} hint={prodVariables + " in PROD"} accent={theme.purple} onClick={() => router.push("/variables")} />
        <StatCard label="Rules" value={String(props.data.rules.length)} hint={prodRules + " in PROD"} accent={theme.warning} onClick={() => router.push("/rules")} />
        <StatCard label="Policies" value={String(props.data.policies.length)} hint="Decision flows configured" accent={theme.success} onClick={() => router.push("/policies")} />
      </div>

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1.2fr 1fr", gap: 14 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text, marginBottom: 12 }}>Connected Sources</div>
          <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "repeat(2, minmax(0, 1fr))", gap: 8 }}>
            {props.data.connectors.map((connector) => (
              <div key={connector.id} style={{ background: theme.hover, borderRadius: 10, padding: 10, opacity: connector.is_active ? 1 : 0.5 }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                  <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                    <ConnectorIcon connectorId={connector.id} color={connector.color} size={18} />
                    <div>
                      <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-semibold)" as unknown as number, color: theme.text }}>{connector.name}</div>
                      <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted }}>{connector.schema_paths.length} fields</div>
                    </div>
                  </div>
                  <StatusBadge status={connector.is_active ? "prod" : "uat"} />
                </div>
              </div>
            ))}
          </div>
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 16 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text, marginBottom: 12 }}>Decision Flow</div>
          <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
            {["Ingest", "Variables", "Rules", "Score", "Policy", "Decision"].map((item, index, list) => (
              <React.Fragment key={item}>
                <div
                  style={{
                    padding: "6px 12px",
                    borderRadius: 999,
                    background:
                      index < 3 ? theme.successBg : index < 5 ? theme.warningBg : theme.accentBg,
                    color: index < 3 ? theme.success : index < 5 ? theme.warning : theme.accent,
                    fontSize: "var(--rm-fs-small)",
                    fontWeight: "var(--rm-fw-bold)" as unknown as number,
                  }}
                >
                  {item}
                </div>
                {index < list.length - 1 ? <span style={{ color: theme.dim }}>→</span> : null}
              </React.Fragment>
            ))}
          </div>
          <div style={{ marginTop: 14, fontSize: "var(--rm-fs-body)", color: theme.muted, lineHeight: 1.6 }}>
            Multi-source decisions combine bureau, bank, GST, device, KYC, and custom payloads through Python variables, click-built rules, scorecards, and policies.
          </div>
        </div>
      </div>
    </div>
  );
}

function ConnectorsPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, isMobile } = useRuleMindStore();
  const [expandedId, setExpandedId] = React.useState<string | null>(null);
  const [busyId, setBusyId] = React.useState<string | null>(null);
  const [configDrafts, setConfigDrafts] = React.useState<Record<string, Record<string, unknown>>>({});
  const [webhooks, setWebhooks] = React.useState<Array<Record<string, unknown>>>([]);

  React.useEffect(() => {
    setConfigDrafts(
      Object.fromEntries(props.data.connectors.map((connector) => [connector.id, { ...connector.config }]))
    );
  }, [props.data.connectors]);

  React.useEffect(() => {
    apiJson<Array<Record<string, unknown>>>(apiBaseUrl, "/api/v1/webhooks", {}, apiKey)
      .then(setWebhooks)
      .catch(() => setWebhooks([]));
  }, [apiBaseUrl, apiKey, props.data.policies]);

  const updateConnector = React.useCallback(
    async (connector: ConnectorRecord, patch: Partial<ConnectorRecord>) => {
      setBusyId(connector.id);
      try {
        await apiJson(apiBaseUrl, "/api/v1/connectors/" + connector.id, {
          method: "PUT",
          body: JSON.stringify({
            name: patch.name ?? connector.name,
            icon: patch.icon ?? connector.icon,
            color: patch.color ?? connector.color,
            description: patch.description ?? connector.description,
            schema_paths: patch.schema_paths ?? connector.schema_paths,
            sample_payload: patch.sample_payload ?? connector.sample_payload,
            is_active: patch.is_active ?? connector.is_active,
            config: patch.config ?? connector.config,
          }),
        }, apiKey);
        props.refresh();
        props.onNotify("Connector updated.");
      } catch (error) {
        props.onNotify(error instanceof Error ? error.message : "Connector update failed.");
      } finally {
        setBusyId(null);
      }
    },
    [apiBaseUrl, apiKey, props]
  );

  const testConnector = React.useCallback(
    async (connectorId: string) => {
      setBusyId(connectorId);
      try {
        await apiJson(apiBaseUrl, "/api/v1/connectors/" + connectorId + "/test", { method: "POST" }, apiKey);
        props.onNotify("Connector test completed.");
      } catch (error) {
        props.onNotify(error instanceof Error ? error.message : "Connector test failed.");
      } finally {
        setBusyId(null);
      }
    },
    [apiBaseUrl, apiKey, props]
  );

  return (
    <div style={{ padding: 20 }}>
      <SectionHeader title={PAGE_META.connectors.title} subtitle={PAGE_META.connectors.subtitle} />
      <div style={{ display: "grid", gap: 16 }}>
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "repeat(2, minmax(0, 1fr))", gap: 12 }}>
        {props.data.connectors.map((connector) => (
          <div key={connector.id} style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
            <div style={{ padding: "14px 16px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <div
                  style={{
                    width: 36,
                    height: 36,
                    borderRadius: 10,
                    background: connector.color + "14",
                    display: "grid",
                    placeItems: "center",
                  }}
                >
                  <ConnectorIcon connectorId={connector.id} color={connector.color} size={18} />
                </div>
                <div>
                  <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>{connector.name}</div>
                  <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{connector.description}</div>
                </div>
              </div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <StatusBadge status={connector.is_active ? "prod" : "uat"} />
                <button
                  type="button"
                  data-testid={"connector-toggle-" + connector.id}
                  disabled={busyId === connector.id}
                  onClick={() => updateConnector(connector, { is_active: !connector.is_active })}
                  style={{
                    width: 40,
                    height: 22,
                    borderRadius: 999,
                    background: connector.is_active ? theme.success : theme.border,
                    border: "none",
                    position: "relative",
                    cursor: "pointer",
                  }}
                >
                  <span
                    style={{
                      position: "absolute",
                      top: 3,
                      left: connector.is_active ? 20 : 3,
                      width: 16,
                      height: 16,
                      borderRadius: "50%",
                      background: theme.toggleKnob,
                      transition: "left 0.12s ease",
                    }}
                  />
                </button>
              </div>
            </div>
            <div style={{ padding: "0 16px 14px" }}>
              <Button small variant="ghost" onClick={() => setExpandedId(expandedId === connector.id ? null : connector.id)} testId={"connector-expand-" + connector.id}>
                {expandedId === connector.id ? "Hide details" : "Show details"}
              </Button>
              {expandedId === connector.id ? (
                <div style={{ marginTop: 10, display: "grid", gap: 10 }}>
                  <div style={{ background: theme.accentBg, color: theme.text, borderRadius: 10, padding: 10 }}>
                    <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, marginBottom: 6, color: theme.accent }}>SCHEMA PATHS</div>
                    <div style={{ fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-code)", color: theme.muted, lineHeight: 1.6 }}>
                      {connector.schema_paths.map((path) => (
                        <div key={path}>payload.{path}</div>
                      ))}
                    </div>
                  </div>
                  <div style={{ background: theme.hover, borderRadius: 10, padding: 10 }}>
                    <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, marginBottom: 6, color: theme.muted }}>SAMPLE JSON</div>
                    <pre style={{ margin: 0, whiteSpace: "pre-wrap", fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-code)", color: theme.codeText, background: theme.editor, border: "1px solid " + theme.border, borderRadius: 10, padding: 10, maxHeight: 180, overflow: "auto" }}>
                      {JSON.stringify(connector.sample_payload, null, 2)}
                    </pre>
                  </div>
                  <div style={{ background: theme.cardAlt, border: "1px solid " + theme.border, borderRadius: 10, padding: 12, display: "grid", gap: 10 }}>
                    <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.muted }}>CONNECTOR CONFIG</div>
                    <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "repeat(2, minmax(0, 1fr))", gap: 8 }}>
                      <InlineSelect
                        value={String(configDrafts[connector.id]?.auth_type ?? "api_key")}
                        onChange={(event) =>
                          setConfigDrafts((current) => ({
                            ...current,
                            [connector.id]: { ...current[connector.id], auth_type: event.target.value },
                          }))
                        }
                      >
                        <option value="api_key">API key</option>
                        <option value="bearer">Bearer</option>
                        <option value="basic">Basic</option>
                        <option value="oauth2">OAuth2</option>
                        <option value="mtls">mTLS</option>
                        <option value="signed_webhook">Signed webhook</option>
                      </InlineSelect>
                      <InlineInput
                        value={String(configDrafts[connector.id]?.base_url ?? "")}
                        onChange={(event) =>
                          setConfigDrafts((current) => ({
                            ...current,
                            [connector.id]: { ...current[connector.id], base_url: event.target.value },
                          }))
                        }
                        placeholder="Base URL"
                      />
                      <InlineInput
                        value={String(configDrafts[connector.id]?.api_key ?? "")}
                        onChange={(event) =>
                          setConfigDrafts((current) => ({
                            ...current,
                            [connector.id]: { ...current[connector.id], api_key: event.target.value },
                          }))
                        }
                        placeholder="API key / token"
                      />
                      <InlineInput
                        value={String(configDrafts[connector.id]?.webhook_url ?? "")}
                        onChange={(event) =>
                          setConfigDrafts((current) => ({
                            ...current,
                            [connector.id]: { ...current[connector.id], webhook_url: event.target.value },
                          }))
                        }
                        placeholder="Webhook URL"
                      />
                      <InlineInput
                        value={String(configDrafts[connector.id]?.request_mapping ?? "")}
                        onChange={(event) =>
                          setConfigDrafts((current) => ({
                            ...current,
                            [connector.id]: { ...current[connector.id], request_mapping: event.target.value },
                          }))
                        }
                        placeholder="Request mapping"
                      />
                      <InlineInput
                        value={String(configDrafts[connector.id]?.response_mapping ?? "")}
                        onChange={(event) =>
                          setConfigDrafts((current) => ({
                            ...current,
                            [connector.id]: { ...current[connector.id], response_mapping: event.target.value },
                          }))
                        }
                        placeholder="Response mapping"
                      />
                      <InlineInput
                        type="number"
                        value={String(configDrafts[connector.id]?.retries ?? 2)}
                        onChange={(event) =>
                          setConfigDrafts((current) => ({
                            ...current,
                            [connector.id]: { ...current[connector.id], retries: Number(event.target.value || 0) },
                          }))
                        }
                        placeholder="Retries"
                      />
                      <InlineInput
                        type="number"
                        value={String(configDrafts[connector.id]?.timeout_ms ?? 3000)}
                        onChange={(event) =>
                          setConfigDrafts((current) => ({
                            ...current,
                            [connector.id]: { ...current[connector.id], timeout_ms: Number(event.target.value || 0) },
                          }))
                        }
                        placeholder="Timeout (ms)"
                      />
                    </div>
                    <div style={{ display: "flex", gap: 8, justifyContent: "flex-end" }}>
                      <Button small onClick={() => testConnector(connector.id)} disabled={busyId === connector.id}>
                        Test
                      </Button>
                      <Button
                        small
                        variant="primary"
                        onClick={() => updateConnector(connector, { config: configDrafts[connector.id] as Record<string, unknown> })}
                        disabled={busyId === connector.id}
                      >
                        Save config
                      </Button>
                    </div>
                  </div>
                </div>
              ) : null}
            </div>
          </div>
        ))}
      </div>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
            <div>
              <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Webhook registrations</div>
              <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>External trigger URLs that can start policies without an API key.</div>
            </div>
            <Button
              small
              onClick={async () => {
                const targetPolicy = props.data.policies[0];
                if (!targetPolicy) {
                  props.onNotify("Create a policy before registering a webhook.");
                  return;
                }
                try {
                  await apiJson(apiBaseUrl, "/api/v1/webhooks", { method: "POST", body: JSON.stringify({ policy_id: targetPolicy.id, is_active: true, payload_mapping: {} }) }, apiKey);
                  const response = await apiJson<Array<Record<string, unknown>>>(apiBaseUrl, "/api/v1/webhooks", {}, apiKey);
                  setWebhooks(response);
                  props.onNotify("Webhook created.");
                } catch (error) {
                  props.onNotify(error instanceof Error ? error.message : "Unable to create webhook.");
                }
              }}
            >
              + Create webhook
            </Button>
          </div>
          <div style={{ display: "grid", gap: 10, padding: 12 }}>
            {webhooks.length === 0 ? <div style={{ fontSize: "var(--rm-fs-body)", color: theme.dim }}>No webhooks configured yet.</div> : null}
            {webhooks.map((webhook) => (
              <div key={String(webhook.id)} style={{ background: theme.hover, borderRadius: 12, padding: 12, display: "grid", gap: 6 }}>
                <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                  <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>{String(webhook.id)}</div>
                  <div style={{ fontSize: "var(--rm-fs-small)", color: webhook.is_active ? theme.success : theme.dim, fontWeight: "var(--rm-fw-bold)" as unknown as number }}>{webhook.is_active ? "ACTIVE" : "OFF"}</div>
                </div>
                <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{String(webhook.policy_id)} · {String(webhook.endpoint_path)}</div>
                <div style={{ display: "flex", gap: 8 }}>
                  <Button
                    small
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(String(webhook.endpoint_path ?? ""));
                        props.onNotify("Webhook path copied.");
                      } catch {
                        props.onNotify("Unable to copy webhook path.");
                      }
                    }}
                  >
                    Copy URL
                  </Button>
                  <Button
                    small
                    variant="danger"
                    onClick={async () => {
                      try {
                        await apiJson(apiBaseUrl, "/api/v1/webhooks/" + webhook.id, { method: "DELETE" }, apiKey);
                        setWebhooks((items) => items.filter((item) => item.id !== webhook.id));
                        props.onNotify("Webhook deactivated.");
                      } catch (error) {
                        props.onNotify(error instanceof Error ? error.message : "Unable to deactivate webhook.");
                      }
                    }}
                  >
                    Deactivate
                  </Button>
                </div>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

function VariablesPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, environment, activeConnectorFilter, setActiveConnectorFilter, isMobile } = useRuleMindStore();
  const [mobileTab, setMobileTab] = React.useState<"list" | "editor">("list");
  const filteredVariables = useFilteredVariables(props.data.variables, props.data.connectors);
  const activeConnectors = React.useMemo(
    () => props.data.connectors.filter((connector) => connector.is_active),
    [props.data.connectors]
  );
  const defaultSourceId = activeConnectors.find((c) => c.id === "bureau")?.id ?? activeConnectors[0]?.id ?? "custom";
  const templates = props.data.variables;
  const connectorMap = React.useMemo(
    () => Object.fromEntries(props.data.connectors.map((connector) => [connector.id, connector])),
    [props.data.connectors]
  );
  const [selectedId, setSelectedId] = React.useState<string | null>(filteredVariables[0]?.id ?? null);
  const [draftMode, setDraftMode] = React.useState(false);
  const [showTemplates, setShowTemplates] = React.useState(false);
  const [name, setName] = React.useState("");
  const [category, setCategory] = React.useState("Custom");
  const [sourceId, setSourceId] = React.useState(defaultSourceId);
  const makeTemplate = (sid: string) => `@variable(source="${sid}")\ndef my_variable(payload, variables, apis):\n    return 0\n`;
  const [code, setCode] = React.useState(makeTemplate(defaultSourceId));
  const [description, setDescription] = React.useState("");
  const [testResult, setTestResult] = React.useState<VariableRecord["last_test_result"] | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [graph, setGraph] = React.useState<VariableGraphResponse | null>(null);
  const [showGraph, setShowGraph] = React.useState(false);

  const beginDraft = React.useCallback(() => {
    if (!draftMode && selectedId) {
      setDraftMode(true);
      setSelectedId(null);
    }
  }, [draftMode, selectedId]);

  React.useEffect(() => {
    if (!draftMode && selectedId) {
      const current = props.data.variables.find((variable) => variable.id === selectedId);
      if (current) {
        setName(current.name);
        setCategory(current.category);
        setSourceId(current.source_id);
        setCode(current.code);
        setDescription(current.description ?? "");
        setTestResult(current.last_test_result ?? null);
        return;
      }
    }
    setName("");
    setCategory("Custom");
    setSourceId(defaultSourceId);
    setCode(makeTemplate(defaultSourceId));
    setDescription("");
    setTestResult(null);
  }, [defaultSourceId, draftMode, props.data.variables, selectedId]);

  const groupedVariables = React.useMemo(() => {
    return CATEGORY_ORDER.map((categoryName) => ({
      categoryName,
      items: filteredVariables.filter((variable) => variable.category === categoryName),
    })).filter((group) => group.items.length > 0);
  }, [filteredVariables]);

  const selectedVariable = !draftMode && selectedId ? props.data.variables.find((variable) => variable.id === selectedId) ?? null : null;
  const selectedVariableLocked =
    selectedVariable &&
    name === selectedVariable.name &&
    category === selectedVariable.category &&
    sourceId === selectedVariable.source_id &&
    code === selectedVariable.code &&
    description === (selectedVariable.description ?? "")
      ? selectedVariable
      : null;

  const loadGraph = React.useCallback(async () => {
    try {
      const response = await apiJson<VariableGraphResponse>(apiBaseUrl, "/api/v1/variables/graph", {}, apiKey);
      setGraph(response);
      setShowGraph(true);
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Unable to load variable dependency graph.");
    }
  }, [apiBaseUrl, apiKey, props]);

  const runTest = React.useCallback(async () => {
    setBusy(true);
    try {
      if (selectedVariableLocked) {
        const response = await apiJson<{ variable: VariableRecord; result: VariableRecord["last_test_result"] }>(
          apiBaseUrl,
          "/api/v1/variables/" + selectedVariableLocked.id + "/test",
          { method: "POST", body: JSON.stringify({ payload: {} }) },
          apiKey
        );
        setTestResult(response.result ?? null);
        props.refresh();
        props.onNotify("Variable test completed.");
      } else {
        const previewResponse = await apiJson<{ result: VariableRecord["last_test_result"] }>(
          apiBaseUrl,
          "/api/v1/variables/test-draft",
          {
            method: "POST",
            body: JSON.stringify({
              source_id: sourceId,
              code,
              payload: {},
            }),
          },
          apiKey
        );
        setTestResult(previewResponse.result ?? null);
        setShowTemplates(false);
        props.onNotify("Draft variable test completed.");
      }
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Variable test failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, code, props, selectedVariableLocked, sourceId]);

  const saveVariable = React.useCallback(async () => {
    setBusy(true);
    try {
      const payload = {
        name: name || "Untitled Variable",
        category,
        source_id: sourceId,
        code,
        description,
        status: selectedVariableLocked?.status ?? environment,
      };
      const response = selectedVariableLocked
        ? await apiJson<VariableRecord>(apiBaseUrl, "/api/v1/variables/" + selectedVariableLocked.id, { method: "PUT", body: JSON.stringify(payload) }, apiKey)
        : await apiJson<VariableRecord>(apiBaseUrl, "/api/v1/variables", { method: "POST", body: JSON.stringify(payload) }, apiKey);
      if (testResult && !testResult.error) {
        const persisted = await apiJson<{ variable: VariableRecord; result: VariableRecord["last_test_result"] }>(
          apiBaseUrl,
          "/api/v1/variables/" + response.id + "/test",
          { method: "POST", body: JSON.stringify({ payload: {} }) },
          apiKey
        );
        setTestResult(persisted.result ?? null);
      }
      setDraftMode(false);
      setSelectedId(response.id);
      props.refresh();
      props.onNotify("Variable saved.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Variable save failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, category, code, description, environment, name, props, selectedVariableLocked, sourceId]);

  const promoteVariable = React.useCallback(async () => {
    if (!selectedVariableLocked) {
      return;
    }
    if (!window.confirm("Promote this variable to the next environment?")) {
      return;
    }
    setBusy(true);
    try {
      await apiJson<VariableRecord>(
        apiBaseUrl,
        "/api/v1/variables/" + selectedVariableLocked.id + "/promote",
        { method: "POST", body: JSON.stringify({ promoted_by: "web", reason: "Manual promotion from Variables page" }) },
        apiKey
      );
      props.refresh();
      props.onNotify("Variable promoted.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Variable promotion failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, selectedVariableLocked]);

  const deleteVariable = React.useCallback(async () => {
    if (!selectedVariableLocked) {
      return;
    }
    if (!window.confirm("Delete this DEV variable? This cannot be undone.")) {
      return;
    }
    setBusy(true);
    try {
      await apiJson(apiBaseUrl, "/api/v1/variables/" + selectedVariableLocked.id, { method: "DELETE" }, apiKey);
      setSelectedId(null);
      props.refresh();
      props.onNotify("Variable deleted.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Variable delete failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, selectedVariableLocked]);

  return (
    <div style={{ display: "flex", flexDirection: isMobile ? "column" : "row", height: isMobile ? "auto" : "calc(100vh - 120px)" }}>
      {isMobile && (
        <div style={{ display: "flex", borderBottom: "1px solid " + theme.border }}>
          <button type="button" onClick={() => setMobileTab("list")} style={{ flex: 1, padding: "12px", background: mobileTab === "list" ? theme.accentBg : "transparent", color: mobileTab === "list" ? theme.accent : theme.muted, border: "none", fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, cursor: "pointer" }}>Variables</button>
          <button type="button" onClick={() => setMobileTab("editor")} style={{ flex: 1, padding: "12px", background: mobileTab === "editor" ? theme.accentBg : "transparent", color: mobileTab === "editor" ? theme.accent : theme.muted, border: "none", fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, cursor: "pointer" }}>Editor</button>
        </div>
      )}
      {(!isMobile || mobileTab === "list") && <div style={{ width: isMobile ? "100%" : 270, borderRight: isMobile ? "none" : "1px solid " + theme.border, display: "flex", flexDirection: "column", flexShrink: 0 }}>
        <div style={{ padding: 14, borderBottom: "1px solid " + theme.border }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8, marginBottom: 10 }}>
            <div>
              <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Variables</div>
              <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>Status filter: {environment.toUpperCase()}</div>
            </div>
            <div style={{ display: "flex", gap: 6 }}>
              <Button small variant="ghost" onClick={() => setShowTemplates((value) => !value)} testId="variable-templates-toggle">
                Templates
              </Button>
              <Button
                small
                variant="primary"
                testId="variable-new"
                onClick={() => {
                  flushSync(() => {
                    setDraftMode(true);
                    setSelectedId(null);
                    setName("");
                    setCategory("Custom");
                    setSourceId(defaultSourceId);
                    setCode(makeTemplate(defaultSourceId));
                    setDescription("");
                    setTestResult(null);
                  });
                  if (isMobile) setMobileTab("editor");
                }}
              >
                + New
              </Button>
            </div>
          </div>
          <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
            <button
              type="button"
              onClick={() => setActiveConnectorFilter("all")}
              style={{
                padding: isMobile ? "8px 12px" : "4px 8px",
                borderRadius: 999,
                border: "none",
                background: activeConnectorFilter === "all" ? theme.accentBg : "transparent",
                color: activeConnectorFilter === "all" ? theme.accent : theme.dim,
                cursor: "pointer",
                fontSize: "var(--rm-fs-caption)",
                fontWeight: "var(--rm-fw-bold)" as unknown as number,
              }}
            >
              All
            </button>
            {activeConnectors.map((connector) => (
              <button
                key={connector.id}
                type="button"
                onClick={() => setActiveConnectorFilter(connector.id)}
                data-testid={"variable-filter-" + connector.id}
                style={{
                  padding: isMobile ? "8px 12px" : "4px 8px",
                  borderRadius: 999,
                  border: "none",
                  background: activeConnectorFilter === connector.id ? connector.color + "22" : "transparent",
                  color: activeConnectorFilter === connector.id ? connector.color : theme.dim,
                  cursor: "pointer",
                  fontSize: "var(--rm-fs-body)",
                  fontWeight: "var(--rm-fw-bold)" as unknown as number,
                }}
              >
                <ConnectorIcon connectorId={connector.id} color={activeConnectorFilter === connector.id ? connector.color : theme.dim} size={12} />
              </button>
            ))}
          </div>
          {showTemplates ? (
            <div style={{ marginTop: 10, background: theme.hover, borderRadius: 10, padding: 8, maxHeight: 220, overflow: "auto" }}>
              {props.data.connectors
                .filter((connector) => connector.id !== "custom" && connector.is_active)
                .map((connector) => (
                  <div key={connector.id} style={{ marginBottom: 8 }}>
                    <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.dim, letterSpacing: 1.1, textTransform: "uppercase", marginBottom: 4 }}>
                      {connector.name}
                    </div>
                    {templates
                      .filter((variable) => variable.source_id === connector.id)
                      .map((template) => (
                        <button
                          key={template.id}
                          type="button"
                          onClick={() => {
                            flushSync(() => {
                              setDraftMode(true);
                              setSelectedId(null);
                              setName(template.name);
                              setCategory(template.category);
                              setSourceId(template.source_id);
                              setCode(template.code);
                              setDescription(template.description ?? "");
                              setTestResult(null);
                              setShowTemplates(false);
                            });
                          }}
                          style={{
                            display: "block",
                            width: "100%",
                            textAlign: "left",
                            padding: "6px 8px",
                            background: "transparent",
                            border: "none",
                            color: theme.text,
                            borderRadius: 8,
                            cursor: "pointer",
                          }}
                        >
                          <div style={{ fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-semibold)" as unknown as number }}>{template.name}</div>
                          <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted }}>{template.description}</div>
                        </button>
                      ))}
                  </div>
                ))}
            </div>
          ) : null}
        </div>

        <div style={{ flex: 1, overflow: "auto", padding: 8 }}>
          {groupedVariables.length === 0 ? (
            <div style={{ fontSize: "var(--rm-fs-body)", color: theme.dim, textAlign: "center", padding: 16 }}>No variables in {environment.toUpperCase()}.</div>
          ) : (
            groupedVariables.map((group) => (
              <div key={group.categoryName} style={{ marginBottom: 12 }}>
                <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.dim, letterSpacing: 1.2, textTransform: "uppercase", padding: "0 8px 6px" }}>
                  {group.categoryName}
                </div>
                {group.items.map((variable) => {
                  const source = connectorMap[variable.source_id];
                  return (
                    <button
                      key={variable.id}
                      type="button"
                      data-testid={"variable-list-item-" + variable.id}
                      onClick={() => {
                        flushSync(() => {
                          setDraftMode(false);
                          setSelectedId(variable.id);
                        });
                        if (isMobile) setMobileTab("editor");
                      }}
                      style={{
                        width: "100%",
                        display: "flex",
                        alignItems: "center",
                        justifyContent: "space-between",
                        gap: 8,
                        background: selectedId === variable.id ? theme.accentBg : "transparent",
                        border: "none",
                        color: theme.text,
                        padding: "8px 10px",
                        borderRadius: 10,
                        cursor: "pointer",
                        textAlign: "left",
                        opacity: source?.is_active ? 1 : 0.55,
                      }}
                    >
                      <div style={{ display: "grid", gap: 2 }}>
                        <div style={{ fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-semibold)" as unknown as number, display: "inline-flex", alignItems: "center", gap: 6 }}>
                          <ConnectorIcon connectorId={variable.source_id} color={source?.color} size={13} />
                          <span>{variable.name}</span>
                        </div>
                        <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted }}>
                          {variable.description || variable.id}
                          {!source?.is_active ? " · source inactive" : ""}
                        </div>
                      </div>
                      <StatusBadge status={variable.status} />
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
      </div>}

      {(!isMobile || mobileTab === "editor") && <div style={{ flex: 1, display: "flex", flexDirection: "column" }}>
        <div style={{ padding: isMobile ? 12 : 16, borderBottom: "1px solid " + theme.border, display: "grid", gap: 12 }}>
          <SectionHeader
            title={PAGE_META.variables.title}
            subtitle={PAGE_META.variables.subtitle}
            actions={
              <div style={{ display: "flex", gap: 8 }}>
                <Button small onClick={runTest} disabled={busy} testId="variable-test">
                  Test
                </Button>
                <Button small variant="primary" onClick={saveVariable} disabled={busy} testId="variable-save">
                  Save
                </Button>
                {selectedVariableLocked ? (
                  <Button small variant="ghost" onClick={loadGraph}>
                    Graph
                  </Button>
                ) : null}
                {selectedVariableLocked && selectedVariableLocked.status !== "prod" ? (
                  <Button small variant="success" onClick={promoteVariable} disabled={busy} testId="variable-promote">
                    Promote
                  </Button>
                ) : null}
                {selectedVariableLocked ? (
                  <Button small variant="danger" onClick={deleteVariable} disabled={busy} testId="variable-delete">
                    Delete
                  </Button>
                ) : null}
              </div>
            }
          />

          <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1.2fr 0.8fr 0.8fr", gap: 10 }}>
            <InlineInput
              value={name}
              onChange={(event) => {
                beginDraft();
                setName(event.target.value);
              }}
              placeholder="Variable name"
              data-testid="variable-name"
            />
            <InlineSelect
              value={sourceId}
              onChange={(event) => {
                const newSourceId = event.target.value;
                beginDraft();
                setSourceId(newSourceId);
                if (code === makeTemplate(sourceId)) {
                  setCode(makeTemplate(newSourceId));
                }
              }}
              data-testid="variable-source"
            >
              {props.data.connectors.map((connector) => (
                <option key={connector.id} value={connector.id}>
                  {connector.name}
                </option>
              ))}
            </InlineSelect>
            <InlineInput
              value={category}
              onChange={(event) => {
                beginDraft();
                setCategory(event.target.value);
              }}
              placeholder="Category"
              data-testid="variable-category"
            />
          </div>
          <InlineInput
            value={description}
            onChange={(event) => {
              beginDraft();
              setDescription(event.target.value);
            }}
            placeholder="Description"
            data-testid="variable-description"
          />
        </div>

        <div style={{ flex: 1, display: "grid", gridTemplateRows: "1fr auto auto", gap: 12, padding: isMobile ? 12 : 16, overflow: "auto" }}>
          <div>
            <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.dim, letterSpacing: 1.2, textTransform: "uppercase", marginBottom: 6 }}>
              Python · {connectorMap[sourceId]?.name ?? sourceId}
            </div>
            <InlineTextarea
              code
              value={code}
              onChange={(event) => {
                beginDraft();
                setCode(event.target.value);
              }}
              rows={18}
              data-testid="variable-code"
              style={{ width: "100%", resize: "vertical" }}
            />
          </div>

          <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr", gap: 12 }}>
            <div style={{ background: theme.hover, borderRadius: 12, padding: 12 }}>
              <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.dim, marginBottom: 8 }}>INPUT PREVIEW</div>
              <pre data-testid="variable-input-preview" style={{ margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-code)", color: theme.muted, whiteSpace: "pre-wrap", height: 340, minHeight: 160, maxHeight: 640, overflow: "auto", resize: "vertical", background: theme.card, border: "1px solid " + theme.border, borderRadius: 8, padding: 10 }}>
                {JSON.stringify(connectorMap[sourceId]?.sample_payload ?? {}, null, 2)}
              </pre>
            </div>
            <div style={{ background: testResult?.passed ? theme.successBg : theme.hover, borderRadius: 12, padding: 12 }}>
              <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.dim, marginBottom: 8 }}>OUTPUT</div>
              {testResult ? (
                <div data-testid="variable-output">
                  <div style={{ fontSize: "var(--rm-fs-hero)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: testResult.error ? theme.danger : theme.success }}>
                    {testResult.error ? "Error" : String(testResult.value)}
                  </div>
                  <div style={{ fontSize: "var(--rm-fs-small)", color: testResult.error ? theme.danger : theme.muted, marginTop: 6 }}>
                    {testResult.error ?? "Latency: " + testResult.latency_ms + " ms"}
                  </div>
                </div>
              ) : (
                <div style={{ fontSize: "var(--rm-fs-body)", color: theme.dim }}>Run Test on Sample to see the computed value.</div>
              )}
            </div>
          </div>

          <div style={{ background: (connectorMap[sourceId]?.color ?? theme.accent) + "14", borderRadius: 12, padding: 12, border: "1px solid " + (connectorMap[sourceId]?.color ?? theme.accent) + "26" }}>
            <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: connectorMap[sourceId]?.color ?? theme.accent, marginBottom: 8 }}>
              SCHEMA HINTS · {connectorMap[sourceId]?.name ?? sourceId}
            </div>
            <div data-testid="variable-schema-hints" style={{ fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-code)", color: theme.muted, lineHeight: 1.7 }}>
              {(connectorMap[sourceId]?.schema_paths ?? []).map((fieldPath) => (
                <div key={fieldPath}>payload.{fieldPath}</div>
              ))}
            </div>
          </div>
          {showGraph && selectedVariableLocked && graph ? (
            <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 12 }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>DEPENDENCY GRAPH</div>
                <Button small variant="ghost" onClick={() => setShowGraph(false)}>
                  Hide
                </Button>
              </div>
              <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted, lineHeight: 1.7 }}>
                {graph.edges.filter((edge) => edge.from === selectedVariableLocked.id).length === 0 ? (
                  <div>No downstream rules, scorecards, or policies reference this variable yet.</div>
                ) : (
                  graph.edges
                    .filter((edge) => edge.from === selectedVariableLocked.id)
                    .map((edge) => {
                      const target = graph.nodes.find((node) => node.id === edge.to);
                      return (
                        <div key={edge.from + edge.to}>
                          {selectedVariableLocked.name} → {target?.type ?? "asset"} · {target?.label ?? edge.to}
                        </div>
                      );
                    })
                )}
              </div>
            </div>
          ) : null}
        </div>
      </div>}
    </div>
  );
}

type RuleBuilderMode = "simple" | "advanced";

function makeRuleTreeId(prefix: string): string {
  return prefix + "_" + Date.now() + "_" + Math.random().toString(16).slice(2, 8);
}

function parseDraftRuleValue(value: unknown): string | number | boolean | null {
  if (typeof value === "string") {
    const lowered = value.trim().toLowerCase();
    if (lowered === "true") {
      return true;
    }
    if (lowered === "false") {
      return false;
    }
    if (lowered === "null") {
      return null;
    }
    if (lowered !== "" && !Number.isNaN(Number(lowered))) {
      return lowered.includes(".") ? Number.parseFloat(lowered) : Number.parseInt(lowered, 10);
    }
  }
  return value as string | number | boolean | null;
}

const RULE_OPERATORS: Array<{ value: string; label: string }> = [
  { value: "==", label: "==" },
  { value: "!=", label: "≠" },
  { value: ">", label: ">" },
  { value: ">=", label: "≥" },
  { value: "<", label: "<" },
  { value: "<=", label: "≤" },
  { value: "between", label: "between" },
  { value: "in", label: "in" },
  { value: "not_in", label: "not in" },
  { value: "regex", label: "regex" },
  { value: "exists", label: "exists" },
  { value: "!exists", label: "!exists" }
];

function draftNumber(value: unknown): number | null {
  if (typeof value === "number") return Number.isNaN(value) ? null : value;
  if (typeof value === "string" && value.trim() !== "") {
    const parsed = Number(value);
    return Number.isNaN(parsed) ? null : parsed;
  }
  return null;
}

function draftOptionList(expected: unknown): string[] {
  if (Array.isArray(expected)) return expected.map((item) => String(item).trim()).filter(Boolean);
  if (expected === null || expected === undefined) return [];
  return String(expected)
    .split(",")
    .map((item) => item.trim())
    .filter(Boolean);
}

function draftLooseEqual(actual: unknown, expected: unknown): boolean {
  if (actual === expected) return true;
  const a = draftNumber(actual);
  const b = draftNumber(expected);
  if (a !== null && b !== null) return a === b;
  return String(actual) === String(expected);
}

// Mirrors packages/shared/operators.spec.json — kept in parity with the server,
// Kotlin, and Dart engines so the draft preview matches production.
function compareDraftRuleValue(
  actual: unknown,
  operator: string,
  expected: unknown,
  expected2?: unknown,
  fieldType?: string
): boolean {
  if (operator === "exists") return actual !== undefined && actual !== null && actual !== "";
  if (operator === "!exists") return actual === undefined || actual === null || actual === "";

  if (operator === "in" || operator === "not_in") {
    const options = draftOptionList(expected);
    const matched = options.some((option) => draftLooseEqual(actual, option));
    return operator === "in" ? matched : !matched;
  }

  if (operator === "regex") {
    if (actual === undefined || actual === null) return false;
    try {
      return new RegExp(String(expected)).test(String(actual));
    } catch {
      return false;
    }
  }

  if ((fieldType ?? "").toLowerCase() === "boolean" && (operator === "==" || operator === "!=")) {
    const toBool = (value: unknown) =>
      typeof value === "boolean" ? value : ["true", "1", "yes"].includes(String(value).trim().toLowerCase());
    const matched = toBool(actual) === toBool(expected);
    return operator === "==" ? matched : !matched;
  }

  if ([">=", "<=", ">", "<", "between"].includes(operator)) {
    const actualValue = draftNumber(actual);
    const expectedValue = draftNumber(expected);
    if (actualValue === null || expectedValue === null) return false;
    if (operator === ">=") return actualValue >= expectedValue;
    if (operator === "<=") return actualValue <= expectedValue;
    if (operator === ">") return actualValue > expectedValue;
    if (operator === "<") return actualValue < expectedValue;
    const upper = draftNumber(expected2);
    if (upper === null) return false;
    return actualValue >= expectedValue && actualValue <= upper;
  }

  if (operator === "==") return draftLooseEqual(actual, expected);
  if (operator === "!=") return !draftLooseEqual(actual, expected);
  return false;
}

function createTreeCondition(defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  return {
    id: makeRuleTreeId("condition"),
    type: "condition",
    variable: defaultVariable?.id ?? "",
    operator: ">=",
    value: "",
  };
}

function createTreeGroup(defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  return {
    id: makeRuleTreeId("group"),
    type: "group",
    logic: "AND",
    children: [createTreeCondition(defaultVariable)],
    onPass: "approve",
    onFail: "reject",
  };
}

function createTreeNot(defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  return {
    id: makeRuleTreeId("not"),
    type: "not",
    child: createTreeCondition(defaultVariable),
  };
}

function normalizeRuleTree(node: RuleTreeNodeRecord | null | undefined, defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  if (!node) {
    return createTreeGroup(defaultVariable);
  }
  if (node.type === "condition") {
    return {
      id: node.id ?? makeRuleTreeId("condition"),
      type: "condition",
      variable: node.variable ?? defaultVariable?.id ?? "",
      operator: node.operator ?? ">=",
      value: node.value ?? "",
      ...(node.value2 !== undefined ? { value2: node.value2 } : {}),
      ...(node.fieldType !== undefined ? { fieldType: node.fieldType } : {}),
    };
  }
  if (node.type === "not") {
    return {
      id: node.id ?? makeRuleTreeId("not"),
      type: "not",
      child: normalizeRuleTree(node.child, defaultVariable),
    };
  }
  return {
    id: node.id ?? makeRuleTreeId("group"),
    type: "group",
    logic: node.logic ?? "AND",
    children:
      node.children && node.children.length
        ? node.children.map((child) => normalizeRuleTree(child, defaultVariable))
        : [createTreeCondition(defaultVariable)],
    onPass: node.onPass ?? "approve",
    onFail: node.onFail ?? "reject",
  };
}

function simpleNodesToTree(nodes: RuleNodeRecord[], defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  if (!nodes.length) {
    return createTreeGroup(defaultVariable);
  }
  let logic: "AND" | "OR" = "AND";
  let onPass: "approve" | "review" | "reject" = "approve";
  const children: RuleTreeNodeRecord[] = [];
  nodes.forEach((node) => {
    if (node.type === "condition") {
      children.push({
        id: node.id,
        type: "condition",
        variable: node.variable ?? defaultVariable?.id ?? "",
        operator: node.operator ?? ">=",
        value: node.value ?? "",
        ...(node.value2 !== undefined ? { value2: node.value2 } : {}),
        ...(node.fieldType !== undefined ? { fieldType: node.fieldType } : {}),
      });
    }
    if (node.type === "and") {
      logic = "AND";
    }
    if (node.type === "or") {
      logic = "OR";
    }
    if (node.type === "approve" || node.type === "review" || node.type === "reject") {
      onPass = node.type;
    }
  });
  return normalizeRuleTree(
    {
      type: "group",
      logic,
      children,
      onPass,
      onFail: "reject",
    },
    defaultVariable
  );
}

function flattenTreeToNodes(tree: RuleTreeNodeRecord | null | undefined): RuleNodeRecord[] {
  if (!tree) {
    return [];
  }
  const nodes: RuleNodeRecord[] = [];
  let counter = 1;
  const nextId = (prefix: string) => prefix + "_" + counter++;
  const walk = (node: RuleTreeNodeRecord, inheritedLogic: "AND" | "OR" = "AND") => {
    if (node.type === "condition") {
      if (nodes.length && nodes[nodes.length - 1]?.type === "condition") {
        nodes.push({
          id: nextId("logic"),
          type: inheritedLogic.toLowerCase() as "and" | "or",
          label: inheritedLogic,
        });
      }
      nodes.push({
        id: node.id ?? nextId("condition"),
        type: "condition",
        variable: node.variable,
        operator: node.operator,
        value: String(node.value ?? ""),
        label: "Condition",
      });
      return;
    }
    if (node.type === "not") {
      const child = node.child ? normalizeRuleTree(node.child) : createTreeCondition();
      if (child.type === "condition") {
        walk(
          {
            ...child,
            operator: child.operator === "!=" ? "==" : "!=",
          },
          inheritedLogic
        );
      } else {
        walk(child, inheritedLogic);
      }
      return;
    }
    const childLogic = node.logic ?? inheritedLogic;
    (node.children ?? []).forEach((child) => walk(child, childLogic));
  };
  walk(tree, tree.type === "group" ? tree.logic ?? "AND" : "AND");
  const onPass = tree.type === "group" ? tree.onPass ?? "approve" : "approve";
  nodes.push({
    id: nextId("outcome"),
    type: onPass,
    label: onPass[0].toUpperCase() + onPass.slice(1),
  });
  return nodes;
}

function countRuleTreeNodes(node: RuleTreeNodeRecord | null | undefined): number {
  if (!node) {
    return 0;
  }
  if (node.type === "condition") {
    return 1;
  }
  if (node.type === "not") {
    return 1 + countRuleTreeNodes(node.child);
  }
  return 1 + (node.children ?? []).reduce((total, child) => total + countRuleTreeNodes(child), 0);
}

function collectTreeVariableIds(node: RuleTreeNodeRecord | null | undefined): string[] {
  if (!node) {
    return [];
  }
  if (node.type === "condition") {
    return node.variable ? [node.variable] : [];
  }
  if (node.type === "not") {
    return collectTreeVariableIds(node.child);
  }
  return (node.children ?? []).flatMap((child) => collectTreeVariableIds(child));
}

function ruleVariableIds(rule: RuleRecord): string[] {
  if (rule.tree) {
    return collectTreeVariableIds(rule.tree);
  }
  return rule.nodes.filter((node) => node.type === "condition" && node.variable).map((node) => node.variable as string);
}

function treeExpression(node: RuleTreeNodeRecord, variables: VariableRecord[], depth = 0): string {
  if (node.type === "condition") {
    const variable = variables.find((item) => item.id === node.variable);
    return (variable?.name ?? node.variable ?? "Variable") + " " + (node.operator ?? "==") + " " + String(node.value ?? "");
  }
  if (node.type === "not") {
    return "NOT (" + treeExpression(normalizeRuleTree(node.child), variables, depth + 1) + ")";
  }
  const children = node.children ?? [];
  const joined = children.length
    ? children.map((child) => treeExpression(child, variables, depth + 1)).join(" " + (node.logic ?? "AND") + " ")
    : "?";
  const wrapped = "(" + joined + ")";
  if (depth > 0) {
    return wrapped;
  }
  return "IF " + wrapped + " → " + String(node.onPass ?? "approve").toUpperCase() + " ELSE → " + String(node.onFail ?? "reject").toUpperCase();
}

function updateRuleTreeNode(node: RuleTreeNodeRecord, targetId: string, updater: (current: RuleTreeNodeRecord) => RuleTreeNodeRecord): RuleTreeNodeRecord {
  if (node.id === targetId) {
    return normalizeRuleTree(updater(node));
  }
  if (node.type === "not" && node.child) {
    return { ...node, child: updateRuleTreeNode(node.child, targetId, updater) };
  }
  if (node.type === "group") {
    return { ...node, children: (node.children ?? []).map((child) => updateRuleTreeNode(child, targetId, updater)) };
  }
  return node;
}

function removeRuleTreeNode(node: RuleTreeNodeRecord, targetId: string, defaultVariable?: VariableRecord): RuleTreeNodeRecord {
  if (node.type === "not") {
    if (node.child?.id === targetId) {
      return { ...node, child: createTreeCondition(defaultVariable) };
    }
    return { ...node, child: node.child ? removeRuleTreeNode(node.child, targetId, defaultVariable) : createTreeCondition(defaultVariable) };
  }
  if (node.type === "group") {
    const nextChildren = (node.children ?? [])
      .filter((child) => child.id !== targetId)
      .map((child) => removeRuleTreeNode(child, targetId, defaultVariable));
    return {
      ...node,
      children: nextChildren.length ? nextChildren : [createTreeCondition(defaultVariable)],
    };
  }
  return node;
}

function moveRuleTreeNode(node: RuleTreeNodeRecord, targetId: string, direction: -1 | 1): RuleTreeNodeRecord {
  if (node.type === "not" && node.child) {
    return { ...node, child: moveRuleTreeNode(node.child, targetId, direction) };
  }
  if (node.type === "group") {
    const children = [...(node.children ?? [])];
    const index = children.findIndex((child) => child.id === targetId);
    if (index >= 0) {
      const nextIndex = index + direction;
      if (nextIndex >= 0 && nextIndex < children.length) {
        [children[index], children[nextIndex]] = [children[nextIndex], children[index]];
      }
      return { ...node, children };
    }
    return { ...node, children: children.map((child) => moveRuleTreeNode(child, targetId, direction)) };
  }
  return node;
}

function simulateRuleTreeDraft(
  tree: RuleTreeNodeRecord,
  variables: VariableRecord[]
): { passed: boolean; outcome: string; conditions: RuleConditionResult[]; groupResults: Array<{ id?: string; logic: string; passed: boolean; childCount: number }> } {
  const variableMap = Object.fromEntries(variables.map((variable) => [variable.id, variable]));
  const conditions: RuleConditionResult[] = [];
  const groupResults: Array<{ id?: string; logic: string; passed: boolean; childCount: number }> = [];

  const evaluateNode = (node: RuleTreeNodeRecord, inheritedLogic: string = "AND"): boolean => {
    if (node.type === "condition") {
      const variable = node.variable ? variableMap[node.variable] : undefined;
      const actual = variable?.last_test_result?.value;
      const expected = parseDraftRuleValue(node.value ?? "");
      const expected2 = node.value2 === undefined ? undefined : parseDraftRuleValue(node.value2);
      const passed = compareDraftRuleValue(actual, node.operator ?? "==", expected, expected2, node.fieldType);
      conditions.push({
        variable_id: node.variable ?? "",
        variable_name: variable?.name ?? node.variable ?? "",
        source_id: variable?.source_id,
        operator: node.operator ?? "==",
        threshold: expected,
        value: actual,
        passed,
        group: inheritedLogic,
      });
      return passed;
    }
    if (node.type === "not") {
      const result = !evaluateNode(normalizeRuleTree(node.child), "NOT");
      groupResults.push({ id: node.id, logic: "NOT", passed: result, childCount: 1 });
      return result;
    }
    const logic = node.logic ?? "AND";
    const childResults = (node.children ?? []).map((child) => evaluateNode(child, logic));
    const passed = logic === "AND" ? childResults.every(Boolean) : childResults.some(Boolean);
    groupResults.push({ id: node.id, logic, passed, childCount: childResults.length });
    return passed;
  };

  const passed = evaluateNode(tree, tree.logic ?? "AND");
  return {
    passed,
    outcome: passed ? String(tree.onPass ?? "approve") : String(tree.onFail ?? "reject"),
    conditions,
    groupResults,
  };
}

function generateExpression(nodes: RuleNodeRecord[], variables: VariableRecord[]): string {
  if (!nodes.length) {
    return "IF (?) → ?";
  }
  let logic = "AND";
  let outcome = "?";
  const conditions = nodes
    .filter((node) => node.type === "condition")
    .map((node) => {
      const variable = variables.find((item) => item.id === node.variable);
      return (variable?.name ?? node.variable ?? "Variable") + " " + (node.operator ?? "==") + " " + (node.value ?? "");
    });
  nodes.forEach((node) => {
    if (node.type === "or") {
      logic = "OR";
    }
    if (node.type === "and") {
      logic = "AND";
    }
    if (node.type === "approve" || node.type === "review" || node.type === "reject") {
      outcome = node.type.toUpperCase();
    }
  });
  return "IF (" + conditions.join(" " + logic + " ") + ") → " + outcome;
}

function simulateRuleDraft(nodes: RuleNodeRecord[], variables: VariableRecord[]): { passed: boolean; outcome: string; conditions: RuleConditionResult[] } {
  const conditions: RuleConditionResult[] = [];
  let useOr = false;
  let outcome = "reject";
  nodes.forEach((node) => {
    if (node.type === "or") {
      useOr = true;
    }
    if (node.type === "and") {
      useOr = false;
    }
    if (node.type === "approve" || node.type === "review" || node.type === "reject") {
      outcome = node.type;
    }
  });

  nodes
    .filter((node) => node.type === "condition")
    .forEach((node) => {
      const variable = variables.find((item) => item.id === node.variable);
      const actualValue = variable?.last_test_result?.value ?? 0;
      const expectedValue = Number(node.value ?? "0");
      let passed = false;
      if (node.operator === ">=") passed = Number(actualValue) >= expectedValue;
      if (node.operator === "<=") passed = Number(actualValue) <= expectedValue;
      if (node.operator === "==") passed = String(actualValue) === String(node.value ?? "");
      if (node.operator === ">") passed = Number(actualValue) > expectedValue;
      if (node.operator === "<") passed = Number(actualValue) < expectedValue;
      if (node.operator === "!=") passed = String(actualValue) !== String(node.value ?? "");
      conditions.push({
        variable_id: node.variable ?? "",
        variable_name: variable?.name ?? node.variable ?? "",
        source_id: variable?.source_id,
        operator: node.operator ?? "==",
        threshold: node.value ?? "",
        value: actualValue,
        passed,
      });
    });

  const passed = useOr ? conditions.some((condition) => condition.passed) : conditions.every((condition) => condition.passed);
  return { passed, outcome: passed ? outcome : "reject", conditions };
}

function AdvancedRuleTreeEditor(props: {
  node: RuleTreeNodeRecord;
  depth: number;
  isRoot?: boolean;
  variables: VariableRecord[];
  connectors: Record<string, ConnectorRecord>;
  onChange: (node: RuleTreeNodeRecord) => void;
  onRemove?: () => void;
  onMoveUp?: () => void;
  onMoveDown?: () => void;
}) {
  const theme = useTheme();
  const canAddChildren = props.depth < 3;

  const actionButtonStyle: React.CSSProperties = {
    border: "1px solid " + theme.border,
    background: theme.card,
    color: theme.muted,
    borderRadius: 8,
    width: 28,
    height: 28,
    display: "grid",
    placeItems: "center",
    cursor: "pointer",
  };

  if (props.node.type === "condition") {
    const variable = props.variables.find((item) => item.id === props.node.variable);
    const connector = variable ? props.connectors[variable.source_id] : undefined;
    return (
      <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 12, display: "grid", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <div style={{ fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.accent, letterSpacing: 0.7 }}>CONDITION</div>
          <div style={{ display: "flex", gap: 6 }}>
            {props.onMoveUp ? (
              <button type="button" onClick={props.onMoveUp} style={actionButtonStyle}>
                <ArrowUp size={14} />
              </button>
            ) : null}
            {props.onMoveDown ? (
              <button type="button" onClick={props.onMoveDown} style={actionButtonStyle}>
                <ArrowDown size={14} />
              </button>
            ) : null}
            {props.onRemove ? (
              <button type="button" onClick={props.onRemove} style={actionButtonStyle}>
                <Trash2 size={14} />
              </button>
            ) : null}
          </div>
        </div>
        <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
          <InlineSelect
            value={props.node.variable ?? ""}
            onChange={(event) => props.onChange({ ...props.node, variable: event.target.value })}
            style={{ minWidth: 220 }}
          >
            {props.variables.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </InlineSelect>
          <InlineSelect
            value={props.node.operator ?? ">="}
            onChange={(event) => props.onChange({ ...props.node, operator: event.target.value as RuleNodeRecord["operator"] })}
            style={{ width: 104 }}
          >
            {RULE_OPERATORS.map((operator) => (
              <option key={operator.value} value={operator.value}>
                {operator.label}
              </option>
            ))}
          </InlineSelect>
          {props.node.operator !== "exists" && props.node.operator !== "!exists" ? (
            <InlineInput
              value={String(props.node.value ?? "")}
              onChange={(event) => props.onChange({ ...props.node, value: event.target.value })}
              placeholder={props.node.operator === "in" || props.node.operator === "not_in" ? "a, b, c" : props.node.operator === "regex" ? "pattern" : "Value"}
              style={{ width: 140 }}
            />
          ) : null}
          {props.node.operator === "between" ? (
            <InlineInput
              value={String(props.node.value2 ?? "")}
              onChange={(event) => props.onChange({ ...props.node, value2: event.target.value })}
              placeholder="Upper"
              style={{ width: 100 }}
            />
          ) : null}
        </div>
        <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted, display: "inline-flex", alignItems: "center", gap: 6 }}>
          <ConnectorIcon connectorId={connector?.id ?? "custom"} color={connector?.color} size={13} />
          <span>{connector?.name ?? "No source selected"}</span>
        </div>
      </div>
    );
  }

  if (props.node.type === "not") {
    return (
      <div style={{ background: theme.cardAlt, border: "1px solid " + theme.border, borderRadius: 12, padding: 12, display: "grid", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.warning }}>
            <CircleSlash size={14} />
            <span>NOT</span>
          </div>
          <div style={{ display: "flex", gap: 6 }}>
            {props.onMoveUp ? (
              <button type="button" onClick={props.onMoveUp} style={actionButtonStyle}>
                <ArrowUp size={14} />
              </button>
            ) : null}
            {props.onMoveDown ? (
              <button type="button" onClick={props.onMoveDown} style={actionButtonStyle}>
                <ArrowDown size={14} />
              </button>
            ) : null}
            {props.onRemove ? (
              <button type="button" onClick={props.onRemove} style={actionButtonStyle}>
                <Trash2 size={14} />
              </button>
            ) : null}
          </div>
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          <Button small onClick={() => props.onChange({ ...props.node, child: createTreeCondition(props.variables[0]) })}>
            Condition
          </Button>
          <Button small onClick={() => props.onChange({ ...props.node, child: createTreeGroup(props.variables[0]) })} disabled={!canAddChildren}>
            Group
          </Button>
        </div>
        <div style={{ paddingLeft: 12, borderLeft: "2px solid " + theme.border }}>
          <AdvancedRuleTreeEditor
            node={normalizeRuleTree(props.node.child, props.variables[0])}
            depth={props.depth + 1}
            variables={props.variables}
            connectors={props.connectors}
            onChange={(child) => props.onChange({ ...props.node, child })}
          />
        </div>
      </div>
    );
  }

  const children = props.node.children ?? [];
  return (
    <div style={{ background: props.isRoot ? theme.card : theme.cardAlt, border: "1px solid " + theme.border, borderRadius: 12, padding: 12, display: "grid", gap: 12 }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10, flexWrap: "wrap" }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
          <div style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.success }}>
            <GitBranch size={14} />
            <span>{props.isRoot ? "ROOT GROUP" : "GROUP"}</span>
          </div>
          <InlineSelect
            value={props.node.logic ?? "AND"}
            onChange={(event) => props.onChange({ ...props.node, logic: event.target.value as "AND" | "OR" })}
            style={{ width: 88 }}
          >
            <option value="AND">AND</option>
            <option value="OR">OR</option>
          </InlineSelect>
          {props.isRoot ? (
            <>
              <InlineSelect
                value={props.node.onPass ?? "approve"}
                onChange={(event) => props.onChange({ ...props.node, onPass: event.target.value as "approve" | "review" | "reject" })}
                style={{ width: 112 }}
              >
                <option value="approve">onPass: approve</option>
                <option value="review">onPass: review</option>
                <option value="reject">onPass: reject</option>
              </InlineSelect>
              <InlineSelect
                value={props.node.onFail ?? "reject"}
                onChange={(event) => props.onChange({ ...props.node, onFail: event.target.value as "approve" | "review" | "reject" })}
                style={{ width: 110 }}
              >
                <option value="approve">onFail: approve</option>
                <option value="review">onFail: review</option>
                <option value="reject">onFail: reject</option>
              </InlineSelect>
            </>
          ) : null}
        </div>
        <div style={{ display: "flex", gap: 6 }}>
          {props.onMoveUp ? (
            <button type="button" onClick={props.onMoveUp} style={actionButtonStyle}>
              <ArrowUp size={14} />
            </button>
          ) : null}
          {props.onMoveDown ? (
            <button type="button" onClick={props.onMoveDown} style={actionButtonStyle}>
              <ArrowDown size={14} />
            </button>
          ) : null}
          {props.onRemove ? (
            <button type="button" onClick={props.onRemove} style={actionButtonStyle}>
              <Trash2 size={14} />
            </button>
          ) : null}
        </div>
      </div>

      <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
        <Button small onClick={() => props.onChange({ ...props.node, children: [...children, createTreeCondition(props.variables[0])] })} disabled={!canAddChildren}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Plus size={12} />
            <span>Condition</span>
          </span>
        </Button>
        <Button small onClick={() => props.onChange({ ...props.node, children: [...children, createTreeGroup(props.variables[0])] })} disabled={!canAddChildren}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Plus size={12} />
            <span>Group</span>
          </span>
        </Button>
        <Button small onClick={() => props.onChange({ ...props.node, children: [...children, createTreeNot(props.variables[0])] })} disabled={!canAddChildren}>
          <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
            <Plus size={12} />
            <span>NOT</span>
          </span>
        </Button>
      </div>

      <div style={{ display: "grid", gap: 10, paddingLeft: props.isRoot ? 0 : 12, borderLeft: props.isRoot ? "none" : "2px solid " + theme.border }}>
        {children.map((child, index) => (
          <AdvancedRuleTreeEditor
            key={child.id ?? "child_" + index}
            node={child}
            depth={props.depth + 1}
            variables={props.variables}
            connectors={props.connectors}
            onChange={(updatedChild) =>
              props.onChange({
                ...props.node,
                children: children.map((item, itemIndex) => (itemIndex === index ? updatedChild : item)),
              })
            }
            onRemove={() => props.onChange({ ...props.node, children: children.filter((_, itemIndex) => itemIndex !== index).length ? children.filter((_, itemIndex) => itemIndex !== index) : [createTreeCondition(props.variables[0])] })}
            onMoveUp={index > 0 ? () => props.onChange({ ...props.node, children: children.map((item, itemIndex) => itemIndex === index ? children[index - 1] : itemIndex === index - 1 ? children[index] : item) }) : undefined}
            onMoveDown={index < children.length - 1 ? () => props.onChange({ ...props.node, children: children.map((item, itemIndex) => itemIndex === index ? children[index + 1] : itemIndex === index + 1 ? children[index] : item) }) : undefined}
          />
        ))}
      </div>
    </div>
  );
}

function RulesPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, environment, isMobile } = useRuleMindStore();
  const connectorMap = React.useMemo(
    () => Object.fromEntries(props.data.connectors.map((connector) => [connector.id, connector])),
    [props.data.connectors]
  );
  const environmentRules = React.useMemo(
    () => props.data.rules.filter((rule) => rule.status === environment),
    [environment, props.data.rules]
  );
  const activeVariables = React.useMemo(
    () => props.data.variables.filter((variable) => connectorMap[variable.source_id]?.is_active),
    [connectorMap, props.data.variables]
  );
  const [tab, setTab] = React.useState<"builder" | "saved" | "test">("builder");
  const [selectedRuleId, setSelectedRuleId] = React.useState<string | null>(environmentRules[0]?.id ?? null);
  const [ruleName, setRuleName] = React.useState("");
  const [builderMode, setBuilderMode] = React.useState<RuleBuilderMode>("simple");
  const [nodes, setNodes] = React.useState<RuleNodeRecord[]>([]);
  const [ruleTree, setRuleTree] = React.useState<RuleTreeNodeRecord>(() => createTreeGroup(activeVariables[0]));
  const [inlineTest, setInlineTest] = React.useState<RuleTestResponse["result"] | null>(null);
  const [ruleTestResult, setRuleTestResult] = React.useState<RuleTestResponse | null>(null);
  const [savedSearch, setSavedSearch] = React.useState("");
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    const current = selectedRuleId ? props.data.rules.find((rule) => rule.id === selectedRuleId) : null;
    if (current) {
      const nextMode: RuleBuilderMode = current.rule_format === "v2" || Boolean(current.tree) ? "advanced" : "simple";
      setBuilderMode(nextMode);
      setRuleName(current.name);
      setNodes(current.nodes ?? []);
      setRuleTree(normalizeRuleTree(current.tree ?? simpleNodesToTree(current.nodes ?? [], activeVariables[0]), activeVariables[0]));
      setInlineTest(current.last_test_result ?? null);
      return;
    }
    setRuleName("New Rule");
    setNodes([]);
    setRuleTree(createTreeGroup(activeVariables[0]));
    setInlineTest(null);
  }, [activeVariables, props.data.rules, selectedRuleId]);

  const activeNodeCount = builderMode === "advanced" ? countRuleTreeNodes(ruleTree) : nodes.length;

  const switchBuilderMode = React.useCallback(
    (nextMode: RuleBuilderMode) => {
      if (nextMode === builderMode) {
        return;
      }
      if (nextMode === "advanced") {
        setRuleTree(normalizeRuleTree(simpleNodesToTree(nodes, activeVariables[0]), activeVariables[0]));
        setBuilderMode("advanced");
        setInlineTest(null);
        return;
      }
      if (!window.confirm("Switching to simple mode will flatten nested groups into a compatibility rule and may change NOT / nested logic structure. Continue?")) {
        return;
      }
      setNodes(flattenTreeToNodes(ruleTree));
      setBuilderMode("simple");
      setInlineTest(null);
    },
    [activeVariables, builderMode, nodes, ruleTree]
  );

  const saveRule = React.useCallback(async () => {
    setBusy(true);
    try {
      const selectedRule = selectedRuleId ? props.data.rules.find((rule) => rule.id === selectedRuleId) : null;
      const payload =
        builderMode === "advanced"
          ? {
              name: ruleName || "Untitled Rule",
              nodes: flattenTreeToNodes(ruleTree),
              tree: ruleTree,
              ruleFormat: "v2",
              status: selectedRule?.status ?? environment,
            }
          : {
              name: ruleName || "Untitled Rule",
              nodes,
              ruleFormat: "v1",
              status: selectedRule?.status ?? environment,
            };
      const response = selectedRuleId
        ? await apiJson<RuleRecord>(apiBaseUrl, "/api/v1/rules/" + selectedRuleId, { method: "PUT", body: JSON.stringify(payload) }, apiKey)
        : await apiJson<RuleRecord>(apiBaseUrl, "/api/v1/rules", { method: "POST", body: JSON.stringify(payload) }, apiKey);
      setSelectedRuleId(response.id);
      props.refresh();
      props.onNotify("Rule saved.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Rule save failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, builderMode, environment, nodes, props, ruleName, ruleTree, selectedRuleId]);

  const testDraft = React.useCallback(() => {
    if (builderMode === "advanced") {
      const result = simulateRuleTreeDraft(ruleTree, props.data.variables);
      setInlineTest({
        passed: result.passed,
        outcome: result.outcome,
        conditions: result.conditions,
        groupResults: result.groupResults,
        latency_ms: 0,
        tested_at: new Date().toISOString(),
      });
      return;
    }
    const result = simulateRuleDraft(nodes, props.data.variables);
    setInlineTest({
      passed: result.passed,
      outcome: result.outcome,
      conditions: result.conditions,
      groupResults: [],
      latency_ms: 0,
      tested_at: new Date().toISOString(),
    });
  }, [builderMode, nodes, props.data.variables, ruleTree]);

  const runSavedRuleTest = React.useCallback(async (ruleId: string) => {
    setBusy(true);
    try {
      const response = await apiJson<RuleTestResponse>(apiBaseUrl, "/api/v1/test/rule/" + ruleId, { method: "POST", body: JSON.stringify({ payload: {} }) }, apiKey);
      setRuleTestResult(response);
      props.refresh();
      props.onNotify("Rule executed.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Rule test failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props]);

  const promoteRule = React.useCallback(async (ruleId: string) => {
    if (!window.confirm("Promote this rule to the next environment?")) {
      return;
    }
    setBusy(true);
    try {
      await apiJson<RuleRecord>(apiBaseUrl, "/api/v1/rules/" + ruleId + "/promote", { method: "POST", body: JSON.stringify({ promoted_by: "web", reason: "Manual promotion from Rules page" }) }, apiKey);
      props.refresh();
      props.onNotify("Rule promoted.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Rule promotion failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props]);

  const filteredSavedRules = React.useMemo(() => {
    const query = savedSearch.trim().toLowerCase();
    if (!query) {
      return environmentRules;
    }
    return environmentRules.filter((rule) => {
      const sourceNames = ruleVariableIds(rule).map(
        (variableId) => connectorMap[props.data.variables.find((variable) => variable.id === variableId)?.source_id ?? ""]?.name ?? ""
      );
      return rule.name.toLowerCase().includes(query) || sourceNames.some((source) => source.toLowerCase().includes(query));
    });
  }, [connectorMap, environmentRules, props.data.variables, savedSearch]);

  const expressionPreview = builderMode === "advanced" ? treeExpression(ruleTree, props.data.variables) : generateExpression(nodes, props.data.variables);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 120px)" }}>
      <div style={{ display: "flex", gap: 18, padding: "0 20px", borderBottom: "1px solid " + theme.border }}>
        {[
          { id: "builder", label: "Builder" },
          { id: "saved", label: "Saved Rules" },
          { id: "test", label: "Test Console" },
        ].map((item) => (
          <button
            key={item.id}
            type="button"
            onClick={() => setTab(item.id as "builder" | "saved" | "test")}
            style={{
              background: "none",
              border: "none",
              borderBottom: tab === item.id ? "2px solid " + theme.accent : "2px solid transparent",
              color: tab === item.id ? theme.accent : theme.muted,
              padding: "12px 0",
              fontSize: "var(--rm-fs-body)",
              fontWeight: "var(--rm-fw-bold)" as unknown as number,
              cursor: "pointer",
            }}
          >
            {item.label}
          </button>
        ))}
      </div>

      {tab === "builder" ? (
        <div style={{ display: "grid", gridTemplateRows: "auto auto 1fr auto auto", flex: 1 }}>
          <div style={{ padding: 16, borderBottom: "1px solid " + theme.border, display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10 }}>
            <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
              <InlineInput value={ruleName} onChange={(event) => setRuleName(event.target.value)} style={{ minWidth: 220 }} data-testid="rule-name" />
              <span style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{activeNodeCount} nodes</span>
            </div>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <div style={{ display: "inline-flex", gap: 6, background: theme.hover, padding: 4, borderRadius: 10 }}>
                <button
                  type="button"
                  onClick={() => switchBuilderMode("simple")}
                  style={{
                    border: "none",
                    background: builderMode === "simple" ? theme.card : "transparent",
                    color: builderMode === "simple" ? theme.text : theme.muted,
                    padding: "6px 10px",
                    borderRadius: 8,
                    cursor: "pointer",
                    fontSize: "var(--rm-fs-small)",
                    fontWeight: "var(--rm-fw-bold)" as unknown as number,
                  }}
                >
                  Simple
                </button>
                <button
                  type="button"
                  onClick={() => switchBuilderMode("advanced")}
                  style={{
                    border: "none",
                    background: builderMode === "advanced" ? theme.card : "transparent",
                    color: builderMode === "advanced" ? theme.text : theme.muted,
                    padding: "6px 10px",
                    borderRadius: 8,
                    cursor: "pointer",
                    fontSize: "var(--rm-fs-small)",
                    fontWeight: "var(--rm-fw-bold)" as unknown as number,
                  }}
                >
                  Advanced
                </button>
              </div>
              <Button
                small
                variant="ghost"
                onClick={() => {
                  setSelectedRuleId(null);
                  setRuleName("New Rule");
                  setNodes([]);
                  setRuleTree(createTreeGroup(activeVariables[0]));
                  setInlineTest(null);
                }}
                testId="rule-clear"
              >
                Clear
              </Button>
              <Button small onClick={testDraft} disabled={activeNodeCount === 0} testId="rule-test">
                Test
              </Button>
              <Button small variant="primary" onClick={saveRule} disabled={activeNodeCount === 0 || busy} testId="rule-save">
                Save
              </Button>
            </div>
          </div>

          <div style={{ padding: "12px 16px", borderBottom: "1px solid " + theme.border, display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }}>
            {builderMode === "simple" ? (
              NODE_TYPES.map((nodeType) => {
                const palette = tone(theme, nodeType.colorKey);
                return (
                  <button
                    key={nodeType.type}
                    type="button"
                    data-testid={"rule-add-" + nodeType.type}
                    onClick={() => setNodes((items) => [...items, cloneNode(nodeType.type, activeVariables[0])])}
                    style={{
                      border: "1px solid " + palette.fg + "30",
                      background: palette.bg,
                      color: palette.fg,
                      borderRadius: 10,
                      padding: "6px 10px",
                      fontSize: "var(--rm-fs-small)",
                      fontWeight: "var(--rm-fw-bold)" as unknown as number,
                      cursor: "pointer",
                    }}
                  >
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      <nodeType.icon size={14} color={palette.fg} strokeWidth={2} />
                      <span>{nodeType.label}</span>
                    </span>
                  </button>
                );
              })
            ) : (
              <>
                <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>
                  Nested groups support <strong style={{ color: theme.text }}>AND</strong>, <strong style={{ color: theme.text }}>OR</strong>, and <strong style={{ color: theme.text }}>NOT</strong> with a max depth of 3.
                </div>
                <Button small variant="ghost" onClick={() => setRuleTree(createTreeGroup(activeVariables[0]))}>
                  Reset tree
                </Button>
              </>
            )}
          </div>

          <div style={{ padding: 16, overflow: "auto" }}>
            {builderMode === "simple" && !nodes.length ? (
              <EmptyState
                icon={<Workflow size={28} />}
                title="Click to add rule nodes"
                description="Use conditions, logic, and outcomes to build a decision without drag-and-drop."
              />
            ) : (
              <>
                {builderMode === "simple" ? (
                  <div style={{ display: "grid", gap: 8 }}>
                    {nodes.map((node, index) => {
                      const spec = NODE_TYPES.find((item) => item.type === node.type)!;
                      const palette = tone(theme, spec.colorKey);
                      return (
                        <div key={node.id} style={{ display: "flex", alignItems: "center", gap: 8 }}>
                          {index > 0 ? <div style={{ width: 18, height: 2, background: theme.border }} /> : null}
                          <div style={{ flex: 1, background: theme.card, border: "1px solid " + palette.fg + "30", borderRadius: 12, padding: 10, display: "flex", alignItems: "center", gap: 10 }}>
                            <div style={{ width: 28, height: 28, borderRadius: 8, background: palette.bg, color: palette.fg, display: "grid", placeItems: "center", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>
                              <spec.icon size={15} color={palette.fg} strokeWidth={2} />
                            </div>
                            {node.type === "condition" ? (
                              <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap", flex: 1 }}>
                                <InlineSelect
                                  value={node.variable ?? ""}
                                  onChange={(event) => setNodes((items) => items.map((item) => (item.id === node.id ? { ...item, variable: event.target.value } : item)))}
                                  style={{ minWidth: 180 }}
                                  testId={"rule-condition-variable-" + node.id}
                                >
                                  {activeVariables.map((variable) => (
                                    <option key={variable.id} value={variable.id}>
                                      {variable.name}
                                    </option>
                                  ))}
                                </InlineSelect>
                                <InlineSelect
                                  value={node.operator ?? ">="}
                                  onChange={(event) => setNodes((items) => items.map((item) => (item.id === node.id ? { ...item, operator: event.target.value as RuleNodeRecord["operator"] } : item)))}
                                  style={{ width: 96 }}
                                >
                                  {RULE_OPERATORS.map((operator) => (
                                    <option key={operator.value} value={operator.value}>
                                      {operator.label}
                                    </option>
                                  ))}
                                </InlineSelect>
                                {node.operator !== "exists" && node.operator !== "!exists" ? (
                                  <InlineInput
                                    value={node.value ?? ""}
                                    onChange={(event) => setNodes((items) => items.map((item) => (item.id === node.id ? { ...item, value: event.target.value } : item)))}
                                    placeholder={node.operator === "in" || node.operator === "not_in" ? "a, b, c" : node.operator === "regex" ? "pattern" : "Value"}
                                    style={{ width: 110 }}
                                  />
                                ) : null}
                                {node.operator === "between" ? (
                                  <InlineInput
                                    value={node.value2 ?? ""}
                                    onChange={(event) => setNodes((items) => items.map((item) => (item.id === node.id ? { ...item, value2: event.target.value } : item)))}
                                    placeholder="Upper"
                                    style={{ width: 90 }}
                                  />
                                ) : null}
                              </div>
                            ) : (
                              <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: palette.fg, flex: 1 }}>{spec.label}</div>
                            )}
                            <button
                              type="button"
                              onClick={() => setNodes((items) => items.filter((item) => item.id !== node.id))}
                              style={{ border: "none", background: "transparent", color: theme.dim, cursor: "pointer", fontSize: "var(--rm-fs-heading)" }}
                            >
                              <Trash2 size={14} />
                            </button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                ) : (
                  <AdvancedRuleTreeEditor
                    node={ruleTree}
                    depth={0}
                    isRoot
                    variables={activeVariables}
                    connectors={connectorMap}
                    onChange={(nextTree) => setRuleTree(normalizeRuleTree(nextTree, activeVariables[0]))}
                  />
                )}
              </>
            )}
          </div>

          <div style={{ padding: "12px 16px", borderTop: "1px solid " + theme.border }}>
            <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.dim, marginBottom: 6, letterSpacing: 1.2, textTransform: "uppercase" }}>Expression</div>
            <div data-testid="rule-expression" style={{ background: theme.hover, borderRadius: 10, padding: "10px 12px", fontFamily: "var(--font-mono)", color: theme.accent, fontSize: "var(--rm-fs-body)" }}>
              {expressionPreview}
            </div>
          </div>

          {inlineTest ? (
            <div style={{ padding: "12px 16px", borderTop: "1px solid " + theme.border, background: inlineTest.passed ? theme.successBg : theme.dangerBg }}>
              <div data-testid="rule-inline-outcome" style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: inlineTest.passed ? theme.success : theme.danger, textTransform: "uppercase", marginBottom: 8 }}>
                {inlineTest.outcome}
              </div>
              <div style={{ display: "grid", gap: 4 }}>
                {inlineTest.conditions.map((condition) => (
                  <div key={condition.variable_id + condition.threshold} style={{ fontSize: "var(--rm-fs-small)", color: condition.passed ? theme.success : theme.danger }}>
                    <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                      {condition.passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                      <span>
                        {condition.variable_name}: {String(condition.value)} {condition.operator} {condition.threshold}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
              {inlineTest.groupResults?.length ? (
                <div style={{ marginTop: 10, display: "grid", gap: 4 }}>
                  {inlineTest.groupResults.map((group) => (
                    <div key={String(group.id) + group.logic} style={{ fontSize: "var(--rm-fs-small)", color: group.passed ? theme.success : theme.danger }}>
                      <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                        {group.passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                        <span>{group.logic} group · {group.childCount} child node(s)</span>
                      </span>
                    </div>
                  ))}
                </div>
              ) : null}
            </div>
          ) : null}
        </div>
      ) : null}

      {tab === "saved" ? (
        <div style={{ padding: 20, overflow: "auto", display: "grid", gap: 10 }}>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <InlineInput value={savedSearch} onChange={(event) => setSavedSearch(event.target.value)} placeholder="Search rules or source names" style={{ maxWidth: 320 }} />
            <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{filteredSavedRules.length} shown</div>
          </div>
          {filteredSavedRules.length === 0 ? (
            <EmptyState icon={<Layers size={28} />} title="No saved rules" description={"Create and save a rule in " + environment.toUpperCase() + " to manage it here."} />
          ) : (
            filteredSavedRules.map((rule) => {
              const usedSourceIds = Array.from(
                new Set(ruleVariableIds(rule).map((variableId) => props.data.variables.find((variable) => variable.id === variableId)?.source_id).filter(Boolean))
              );
              return (
                <div key={rule.id} style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "flex", justifyContent: "space-between", gap: 12 }}>
                  <div style={{ display: "grid", gap: 6 }}>
                    <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text, display: "inline-flex", gap: 8, alignItems: "center" }}>
                      <span>{rule.name}</span>
                      <span style={{ fontSize: "var(--rm-fs-caption)", color: theme.dim, textTransform: "uppercase" }}>{rule.rule_format ?? "v1"}</span>
                    </div>
                    <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{rule.tree ? countRuleTreeNodes(rule.tree) : rule.nodes.length} nodes</div>
                    <div style={{ display: "flex", gap: 6 }}>
                      {usedSourceIds.map((sourceId) => (
                        <ConnectorIcon key={String(sourceId)} connectorId={String(sourceId)} color={connectorMap[String(sourceId)]?.color} size={14} />
                      ))}
                    </div>
                    {!rule.last_test_result?.passed ? (
                      <div style={{ fontSize: "var(--rm-fs-small)", color: theme.warning }}>Latest test must pass before promotion.</div>
                    ) : null}
                  </div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <StatusBadge status={rule.status} />
                    {rule.status !== "prod" ? (
                      <Button small variant="success" onClick={() => promoteRule(rule.id)} disabled={!rule.last_test_result?.passed}>
                        Promote
                      </Button>
                    ) : null}
                    <Button
                      small
                      onClick={() => {
                        setSelectedRuleId(rule.id);
                        setTab("builder");
                      }}
                    >
                      Edit
                    </Button>
                  </div>
                </div>
              );
            })
          )}
        </div>
      ) : null}

      {tab === "test" ? (
        <div style={{ padding: 20, overflow: "auto", display: "grid", gap: 12 }}>
          <SectionHeader title="Saved Rule Test" subtitle="Execute a persisted rule against the active connector samples." />
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <InlineSelect value={selectedRuleId ?? ""} onChange={(event) => setSelectedRuleId(event.target.value || null)} style={{ minWidth: 280 }} data-testid="rules-test-select">
              <option value="">Select a rule</option>
              {environmentRules.map((rule) => (
                <option key={rule.id} value={rule.id}>
                  {rule.name}
                </option>
              ))}
            </InlineSelect>
            <Button small variant="primary" disabled={!selectedRuleId || busy} onClick={() => (selectedRuleId ? runSavedRuleTest(selectedRuleId) : undefined)} testId="rules-test-run">
              Execute
            </Button>
          </div>
          {ruleTestResult ? (
            <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
              <div style={{ padding: 14, background: ruleTestResult.result.passed ? theme.successBg : theme.dangerBg }}>
                <div data-testid="rule-saved-outcome" style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: ruleTestResult.result.passed ? theme.success : theme.danger, textTransform: "uppercase" }}>
                  {ruleTestResult.result.outcome}
                </div>
              </div>
              <div style={{ padding: 14, display: "grid", gap: 8 }}>
                {ruleTestResult.result.conditions.map((condition) => (
                  <div key={condition.variable_id + condition.threshold} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: "var(--rm-fs-body)", color: theme.text }}>
                    <span style={{ color: condition.passed ? theme.success : theme.danger }}>{condition.passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}</span>
                    <ConnectorIcon connectorId={condition.source_id ?? "custom"} color={connectorMap[condition.source_id ?? ""]?.color} size={12} />
                    <span>
                      {condition.variable_name}: {String(condition.value)} {condition.operator} {condition.threshold}
                    </span>
                  </div>
                ))}
                {ruleTestResult.result.groupResults?.length ? (
                  <div style={{ marginTop: 6, display: "grid", gap: 4 }}>
                    {ruleTestResult.result.groupResults.map((group) => (
                      <div key={String(group.id) + group.logic} style={{ fontSize: "var(--rm-fs-small)", color: group.passed ? theme.success : theme.danger }}>
                        {group.logic} group · {group.passed ? "PASS" : "FAIL"} · {group.childCount} child node(s)
                      </div>
                    ))}
                  </div>
                ) : null}
              </div>
            </div>
          ) : (
            <EmptyState icon={<FileSearch size={28} />} title="No test result yet" description="Select a rule and execute it to inspect the per-condition breakdown." />
          )}
        </div>
      ) : null}
    </div>
  );
}

function ScorecardsPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, environment, isMobile } = useRuleMindStore();
  const connectorMap = React.useMemo(
    () => Object.fromEntries(props.data.connectors.map((connector) => [connector.id, connector])),
    [props.data.connectors]
  );
  const environmentScorecards = props.data.scorecards.filter((scorecard) => scorecard.status === environment);
  const editableVariables = React.useMemo(() => {
    const referenced = new Set(
      environmentScorecards.flatMap((scorecard) => scorecard.bins.map((bin) => bin.variable_id))
    );
    return props.data.variables.filter((variable) => variable.status === "prod" || referenced.has(variable.id));
  }, [environmentScorecards, props.data.variables]);
  const [selectedId, setSelectedId] = React.useState<string | null>(environmentScorecards[0]?.id ?? null);
  const [scorecardName, setScorecardName] = React.useState("");
  const [baseScore, setBaseScore] = React.useState(300);
  const [maxScore, setMaxScore] = React.useState(900);
  const [bins, setBins] = React.useState<ScorecardBinRecord[]>([]);
  const [testResult, setTestResult] = React.useState<{ score: number; breakdown: Array<{ variable_id: string; points: number; active_range?: ScorecardRangeRecord | null }> } | null>(null);
  const [busy, setBusy] = React.useState(false);

  React.useEffect(() => {
    const current = selectedId ? props.data.scorecards.find((scorecard) => scorecard.id === selectedId) : null;
    if (current) {
      setScorecardName(current.name);
      setBaseScore(current.base_score);
      setMaxScore(current.max_score);
      setBins(current.bins);
      setTestResult(current.last_test_result ? { score: current.last_test_result.score, breakdown: [] } : null);
      return;
    }
    setScorecardName("New Scorecard");
    setBaseScore(300);
    setMaxScore(900);
    setBins([]);
    setTestResult(null);
  }, [props.data.scorecards, selectedId]);

  const saveScorecard = React.useCallback(async () => {
    setBusy(true);
    try {
      const payload = { name: scorecardName || "Untitled Scorecard", base_score: baseScore, max_score: maxScore, bins, status: selectedId ? props.data.scorecards.find((scorecard) => scorecard.id === selectedId)?.status ?? environment : environment };
      const response = selectedId
        ? await apiJson<ScorecardRecord>(apiBaseUrl, "/api/v1/scorecards/" + selectedId, { method: "PUT", body: JSON.stringify(payload) }, apiKey)
        : await apiJson<ScorecardRecord>(apiBaseUrl, "/api/v1/scorecards", { method: "POST", body: JSON.stringify(payload) }, apiKey);
      setSelectedId(response.id);
      props.refresh();
      props.onNotify("Scorecard saved.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Scorecard save failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, baseScore, bins, environment, maxScore, props, scorecardName, selectedId]);

  const runScorecardTest = React.useCallback(async () => {
    if (!selectedId) {
      props.onNotify("Save the scorecard first, then run a live sample calculation.");
      return;
    }
    setBusy(true);
    try {
      const response = await apiJson<{ result: { score: number; breakdown: Array<{ variable_id: string; points: number; active_range?: ScorecardRangeRecord | null }> } }>(
        apiBaseUrl,
        "/api/v1/scorecards/" + selectedId + "/test",
        { method: "POST", body: JSON.stringify({ payload: {} }) },
        apiKey
      );
      setTestResult(response.result);
      props.refresh();
      props.onNotify("Scorecard calculated.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Scorecard test failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, selectedId]);

  const promoteScorecard = React.useCallback(async () => {
    if (!selectedId) {
      return;
    }
    setBusy(true);
    try {
      await apiJson(apiBaseUrl, "/api/v1/scorecards/" + selectedId + "/promote", { method: "POST", body: JSON.stringify({ promoted_by: "web", reason: "Manual promotion from Scorecards page" }) }, apiKey);
      props.refresh();
      props.onNotify("Scorecard promoted.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Scorecard promotion failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, selectedId]);

  const breakdownMap = React.useMemo(
    () => Object.fromEntries((testResult?.breakdown ?? []).map((row) => [row.variable_id, row])),
    [testResult]
  );

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <SectionHeader
        title={PAGE_META.scorecards.title}
        subtitle={PAGE_META.scorecards.subtitle}
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <Button small onClick={runScorecardTest} disabled={busy} testId="scorecard-test">
              Test
            </Button>
            <Button small variant="primary" onClick={saveScorecard} disabled={busy} testId="scorecard-save">
              Save
            </Button>
            {selectedId && props.data.scorecards.find((scorecard) => scorecard.id === selectedId)?.status !== "prod" ? (
              <Button small variant="success" onClick={promoteScorecard} disabled={busy} testId="scorecard-promote">
                Promote
              </Button>
            ) : null}
          </div>
        }
      />

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 220px 220px 180px", gap: 10 }}>
        <InlineSelect value={selectedId ?? ""} onChange={(event) => setSelectedId(event.target.value || null)} data-testid="scorecard-select">
          <option value="">New scorecard</option>
          {environmentScorecards.map((scorecard) => (
            <option key={scorecard.id} value={scorecard.id}>
              {scorecard.name}
            </option>
          ))}
        </InlineSelect>
        <InlineInput value={scorecardName} onChange={(event) => setScorecardName(event.target.value)} placeholder="Scorecard name" />
        <InlineInput type="number" value={String(baseScore)} onChange={(event) => setBaseScore(Number(event.target.value || 0))} placeholder="Base score" />
        <div style={{ alignSelf: "center", justifySelf: "end", fontSize: "var(--rm-fs-hero)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.success }} data-testid="scorecard-live-score">
          {testResult?.score ?? baseScore}
        </div>
      </div>

      <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Score bins</div>
          <Button
            small
            onClick={() =>
              setBins((items) => [
                ...items,
                { variable_id: editableVariables[0]?.id ?? "", ranges: [{ min: 0, max: 100, points: 0 }] },
              ])
            }
            testId="scorecard-add-bin"
          >
            + Factor
          </Button>
        </div>
        {bins.length === 0 ? (
          <div style={{ padding: 24 }}>
            <EmptyState icon={<Layers size={28} />} title="No scorecard factors" description="Add one or more variable bins to calculate a score." />
          </div>
        ) : (
          bins.map((bin, index) => {
            const breakdown = breakdownMap[bin.variable_id];
            const variable = props.data.variables.find((item) => item.id === bin.variable_id);
            return (
              <div key={index} style={{ padding: 14, borderTop: index === 0 ? "none" : "1px solid " + theme.border, display: "grid", gap: 10 }}>
                <div style={{ display: "flex", gap: 10, alignItems: "center", justifyContent: "space-between" }}>
                  <InlineSelect
                    value={bin.variable_id}
                    onChange={(event) => setBins((items) => items.map((item, itemIndex) => (itemIndex === index ? { ...item, variable_id: event.target.value } : item)))}
                    style={{ minWidth: 260 }}
                  >
                    {editableVariables.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </InlineSelect>
                  <div style={{ fontSize: "var(--rm-fs-body)", color: theme.muted }}>
                    {variable ? connectorMap[variable.source_id]?.name + " · " + variable.category : ""}
                  </div>
                  <Button small variant="danger" onClick={() => setBins((items) => items.filter((_, itemIndex) => itemIndex !== index))}>
                    Remove
                  </Button>
                </div>

                <div style={{ display: "flex", gap: 8, flexWrap: "wrap" }}>
                  {bin.ranges.map((range, rangeIndex) => {
                    const active = breakdown?.active_range && breakdown.active_range.min === range.min && breakdown.active_range.max === range.max && breakdown.active_range.points === range.points;
                    const palette = range.points > 0 ? tone(theme, "success") : range.points < 0 ? tone(theme, "danger") : tone(theme, "warning");
                    return (
                      <div key={rangeIndex} style={{ background: active ? palette.bg : theme.hover, borderRadius: 10, padding: 8, display: "grid", gap: 6, opacity: active ? 1 : 0.78 }}>
                        <div style={{ display: "flex", justifyContent: "flex-end", gap: 4 }}>
                          <button type="button" onClick={() => setBins((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ranges: item.ranges.map((candidate, candidateIndex) => candidateIndex === rangeIndex - 1 ? item.ranges[rangeIndex] : candidateIndex === rangeIndex ? item.ranges[rangeIndex - 1] : candidate) } : item))} disabled={rangeIndex === 0} style={{ border: "none", background: "transparent", color: theme.dim, cursor: rangeIndex === 0 ? "not-allowed" : "pointer" }}><ArrowUp size={13} /></button>
                          <button type="button" onClick={() => setBins((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ranges: item.ranges.map((candidate, candidateIndex) => candidateIndex === rangeIndex ? item.ranges[rangeIndex + 1] : candidateIndex === rangeIndex + 1 ? item.ranges[rangeIndex] : candidate) } : item))} disabled={rangeIndex === bin.ranges.length - 1} style={{ border: "none", background: "transparent", color: theme.dim, cursor: rangeIndex === bin.ranges.length - 1 ? "not-allowed" : "pointer" }}><ArrowDown size={13} /></button>
                          <button type="button" onClick={() => setBins((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ranges: item.ranges.filter((_, candidateIndex) => candidateIndex !== rangeIndex) } : item))} style={{ border: "none", background: "transparent", color: theme.dim, cursor: "pointer" }}><Trash2 size={13} /></button>
                        </div>
                        <div style={{ display: "flex", gap: 6 }}>
                          <InlineInput type="number" value={String(range.min)} onChange={(event) => setBins((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ranges: item.ranges.map((candidate, candidateIndex) => candidateIndex === rangeIndex ? { ...candidate, min: Number(event.target.value || 0) } : candidate) } : item))} style={{ width: 80 }} />
                          <InlineInput type="number" value={String(range.max)} onChange={(event) => setBins((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ranges: item.ranges.map((candidate, candidateIndex) => candidateIndex === rangeIndex ? { ...candidate, max: Number(event.target.value || 0) } : candidate) } : item))} style={{ width: 80 }} />
                          <InlineInput type="number" value={String(range.points)} onChange={(event) => setBins((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ranges: item.ranges.map((candidate, candidateIndex) => candidateIndex === rangeIndex ? { ...candidate, points: Number(event.target.value || 0) } : candidate) } : item))} style={{ width: 90 }} />
                        </div>
                        <div style={{ fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: active ? palette.fg : theme.muted }}>
                          {range.min}–{range.max}: {range.points > 0 ? "+" : ""}{range.points}
                        </div>
                      </div>
                    );
                  })}
                  <Button small onClick={() => setBins((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ranges: [...item.ranges, { min: 0, max: 0, points: 0 }] } : item))}>
                    + Range
                  </Button>
                </div>
              </div>
            );
          })
        )}
      </div>
    </div>
  );
}

function PoliciesPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, environment, isMobile } = useRuleMindStore();
  const connectorMap = React.useMemo(
    () => Object.fromEntries(props.data.connectors.map((connector) => [connector.id, connector])),
    [props.data.connectors]
  );
  const editableRules = props.data.rules.filter((rule) => rule.status === "prod" || rule.status === environment);
  const editableScorecards = props.data.scorecards.filter((scorecard) => scorecard.status === "prod" || scorecard.status === environment);
  const environmentPolicies = props.data.policies.filter((policy) => policy.status === environment);
  const [selectedId, setSelectedId] = React.useState<string | null>(environmentPolicies[0]?.id ?? null);
  const [policyName, setPolicyName] = React.useState("");
  const [steps, setSteps] = React.useState<PolicyStepRecord[]>([]);
  const [execution, setExecution] = React.useState<PolicyExecuteResponse | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [meceResult, setMeceResult] = React.useState<{ isMutuallyExclusive: boolean; isCollectivelyExhaustive: boolean; diagnostics: Array<{ type: string; severity: string; fields: string[]; description: string; involvedRules: string[]; involvedNodeIds?: string[] }>; analyzedFields: string[]; ruleCount: number; hasOpaqueConstraints: boolean; warnings: string[] } | null>(null);
  const [meceOverlapRuleIds, setMeceOverlapRuleIds] = React.useState<Set<string>>(new Set());

  React.useEffect(() => {
    const current = selectedId ? props.data.policies.find((policy) => policy.id === selectedId) : null;
    if (current) {
      setPolicyName(current.name);
      setSteps(current.steps);
      return;
    }
    setPolicyName("New Policy");
    setSteps([]);
    setExecution(null);
  }, [props.data.policies, selectedId]);

  const savePolicy = React.useCallback(async () => {
    setBusy(true);
    try {
      const payload = { name: policyName || "Untitled Policy", steps, status: selectedId ? props.data.policies.find((policy) => policy.id === selectedId)?.status ?? environment : environment };
      const response = selectedId
        ? await apiJson<PolicyRecord>(apiBaseUrl, "/api/v1/policies/" + selectedId, { method: "PUT", body: JSON.stringify(payload) }, apiKey)
        : await apiJson<PolicyRecord>(apiBaseUrl, "/api/v1/policies", { method: "POST", body: JSON.stringify(payload) }, apiKey);
      setSelectedId(response.id);
      props.refresh();
      props.onNotify("Policy saved.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Policy save failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, environment, policyName, props, selectedId, steps]);

  const executePolicyRun = React.useCallback(async () => {
    if (!selectedId) {
      props.onNotify("Save the policy first, then execute it.");
      return;
    }
    setBusy(true);
    try {
      const response = await apiJson<PolicyExecuteResponse>(apiBaseUrl, "/api/v1/policies/" + selectedId + "/execute", { method: "POST", body: JSON.stringify({ payload: {} }) }, apiKey);
      setExecution(response);
      props.refresh();
      props.onNotify("Policy executed.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Policy execution failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, selectedId]);

  const promotePolicy = React.useCallback(async () => {
    if (!selectedId) {
      return;
    }
    if (!window.confirm("Promote this policy to the next environment?")) {
      return;
    }
    setBusy(true);
    try {
      await apiJson(apiBaseUrl, "/api/v1/policies/" + selectedId + "/promote", { method: "POST", body: JSON.stringify({ promoted_by: "web", reason: "Manual promotion from Policies page" }) }, apiKey);
      props.refresh();
      props.onNotify("Policy promoted.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Policy promotion failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, selectedId]);

  const analyzeMece = React.useCallback(async () => {
    if (!selectedId) {
      props.onNotify("Save the policy first, then analyze MECE.");
      return;
    }
    setBusy(true);
    setMeceResult(null);
    setMeceOverlapRuleIds(new Set());
    try {
      // Gather rule refs from this policy's steps
      const ruleSteps = steps.filter((step) => step.type === "rule");

      if (ruleSteps.length < 2) {
        props.onNotify("Need at least 2 rule steps to analyze MECE.");
        setBusy(false);
        return;
      }

      const result = await apiJson<typeof meceResult>(apiBaseUrl, "/api/v1/policies/" + selectedId + "/analyze-mece", { method: "POST", body: "{}" }, apiKey);
      setMeceResult(result);
      // Collect rule IDs that have overlaps for highlighting
      const overlapIds = new Set<string>();
      if (result?.diagnostics) {
        for (const d of result.diagnostics) {
          if (d.type === "overlap") {
            for (const rId of d.involvedRules) overlapIds.add(rId);
          }
        }
      }
      setMeceOverlapRuleIds(overlapIds);
      if (result?.isMutuallyExclusive && result?.isCollectivelyExhaustive) {
        props.onNotify("MECE check passed — rules are mutually exclusive and collectively exhaustive.");
      } else {
        props.onNotify("MECE issues found — see diagnostics below.");
      }
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "MECE analysis failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, selectedId, steps]);

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <SectionHeader
        title={PAGE_META.policies.title}
        subtitle={PAGE_META.policies.subtitle}
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <Button small onClick={analyzeMece} disabled={busy} testId="policy-mece">
              Analyze MECE
            </Button>
            <Button small onClick={executePolicyRun} disabled={busy} testId="policy-run">
              Execute
            </Button>
            <Button small variant="primary" onClick={savePolicy} disabled={busy} testId="policy-save">
              Save
            </Button>
            {selectedId && props.data.policies.find((policy) => policy.id === selectedId)?.status !== "prod" ? (
              <Button small variant="success" onClick={promotePolicy} disabled={busy} testId="policy-promote">
                Promote
              </Button>
            ) : null}
          </div>
        }
      />

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "280px 1fr", gap: 12 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 12, display: "grid", gap: 10 }}>
          <InlineSelect value={selectedId ?? ""} onChange={(event) => setSelectedId(event.target.value || null)} data-testid="policy-select">
            <option value="">New policy</option>
            {environmentPolicies.map((policy) => (
              <option key={policy.id} value={policy.id}>
                {policy.name}
              </option>
            ))}
          </InlineSelect>
          <InlineInput value={policyName} onChange={(event) => setPolicyName(event.target.value)} placeholder="Policy name" />
          <div style={{ display: "grid", gap: 8 }}>
            <Button small onClick={() => setSteps((items) => [...items, { type: "connector", ref_id: props.data.connectors[0]?.id ?? "bureau", label: "Connector" }])}>
              + Connector step
            </Button>
            <Button small onClick={() => setSteps((items) => [...items, { type: "rule", ref_id: editableRules[0]?.id ?? "", label: "Rule" }])}>
              + Rule step
            </Button>
            <Button small onClick={() => setSteps((items) => [...items, { type: "scorecard", ref_id: editableScorecards[0]?.id ?? "", label: "Scorecard" }])}>
              + Scorecard step
            </Button>
            <Button small onClick={() => setSteps((items) => [...items, { type: "transform", id: "transform_" + (items.length + 1), name: "Transform", config: { outputKey: "computed", mapping: { score: "$.variables.bureau_score" } } }])}>
              + Transform step
            </Button>
            <Button small onClick={() => setSteps((items) => [...items, { type: "action", id: "action_" + (items.length + 1), name: "Action", config: { url: "https://client.example.com/hook", method: "POST", bodyTemplate: { outcome: "{{outcome}}" }, onFailure: "continue" } }])}>
              + Action step
            </Button>
            <Button small onClick={() => setSteps((items) => [...items, { type: "review_gate", id: "review_" + (items.length + 1), name: "Review Gate", config: { assignTo: "underwriting_queue", timeoutHours: 48, onTimeout: "reject" } }])}>
              + Review gate
            </Button>
            <Button small onClick={() => setSteps((items) => [...items, { type: "outcome", ref_id: "approve", label: "Decision" }])}>
              + Outcome step
            </Button>
          </div>
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 16, display: "grid", gap: 14 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Visual pipeline</div>
          <div style={{ display: "flex", gap: 8, flexWrap: "wrap", alignItems: "center" }} data-testid="policy-pipeline">
            {steps.length === 0 ? (
              <div style={{ fontSize: "var(--rm-fs-body)", color: theme.dim }}>No policy steps yet.</div>
            ) : (
              steps.map((step, index) => {
                const isConnector = step.type === "connector";
                const stepRef = step.ref_id ?? step.ref ?? "";
                const label = isConnector
                  ? connectorMap[stepRef]?.name ?? stepRef
                  : step.type === "rule"
                    ? props.data.rules.find((rule) => rule.id === stepRef)?.name ?? stepRef
                    : step.type === "scorecard"
                      ? props.data.scorecards.find((scorecard) => scorecard.id === stepRef)?.name ?? stepRef
                      : step.name ?? step.id ?? stepRef;
                return (
                  <React.Fragment key={index}>
                    <div style={{ background: isConnector ? theme.accentBg : step.type === "rule" ? (meceOverlapRuleIds.has(stepRef) ? "rgba(239,68,68,0.15)" : theme.successBg) : step.type === "scorecard" ? theme.warningBg : step.type === "action" ? theme.purpleBg : theme.hover, color: isConnector ? theme.accent : step.type === "rule" ? (meceOverlapRuleIds.has(stepRef) ? "#ef4444" : theme.success) : step.type === "scorecard" ? theme.warning : step.type === "action" ? theme.purple : theme.text, borderRadius: 12, padding: 10, display: "grid", gap: 8, minWidth: 220, border: meceOverlapRuleIds.has(stepRef) ? "2px solid #ef4444" : "none", transition: "border 0.3s, background 0.3s" }}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                        <strong style={{ fontSize: "var(--rm-fs-body)" }}>{step.type.toUpperCase()}</strong>
                        <div style={{ display: "flex", gap: 4 }}>
                          <button type="button" onClick={() => setSteps((items) => items.map((item, itemIndex) => itemIndex === index - 1 ? items[index] : itemIndex === index ? items[index - 1] : item))} disabled={index === 0} style={{ border: "none", background: "transparent", color: theme.dim, cursor: index === 0 ? "not-allowed" : "pointer" }}><ArrowUp size={13} /></button>
                          <button type="button" onClick={() => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? items[index + 1] : itemIndex === index + 1 ? items[index] : item))} disabled={index === steps.length - 1} style={{ border: "none", background: "transparent", color: theme.dim, cursor: index === steps.length - 1 ? "not-allowed" : "pointer" }}><ArrowDown size={13} /></button>
                          <button type="button" onClick={() => setSteps((items) => items.filter((_, itemIndex) => itemIndex !== index))} style={{ border: "none", background: "transparent", color: theme.dim, cursor: "pointer" }}>
                            <Trash2 size={13} />
                          </button>
                        </div>
                      </div>
                      {step.type === "connector" ? (
                        <InlineSelect value={stepRef} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ref_id: event.target.value } : item))}>
                          {props.data.connectors.map((connector) => (
                            <option key={connector.id} value={connector.id}>
                              {connector.name}
                            </option>
                          ))}
                        </InlineSelect>
                      ) : null}
                      {step.type === "rule" ? (
                        <InlineSelect value={stepRef} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ref_id: event.target.value } : item))}>
                          {editableRules.map((rule) => (
                            <option key={rule.id} value={rule.id}>
                              {rule.name}
                            </option>
                          ))}
                        </InlineSelect>
                      ) : null}
                      {step.type === "scorecard" ? (
                        <InlineSelect value={stepRef} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ref_id: event.target.value } : item))}>
                          {editableScorecards.map((scorecard) => (
                            <option key={scorecard.id} value={scorecard.id}>
                              {scorecard.name}
                            </option>
                          ))}
                        </InlineSelect>
                      ) : null}
                      {step.type === "outcome" ? (
                        <InlineSelect value={stepRef} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, ref_id: event.target.value } : item))}>
                          <option value="approve">Approve</option>
                          <option value="review">Review</option>
                          <option value="reject">Reject</option>
                        </InlineSelect>
                      ) : null}
                      {step.type === "transform" ? (
                        <div style={{ display: "grid", gap: 6 }}>
                          <InlineInput value={String(step.name ?? "")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item))} placeholder="Transform name" />
                          <InlineInput value={String((step.config?.outputKey as string | undefined) ?? "computed")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), outputKey: event.target.value } } : item))} placeholder="Output key" />
                          <textarea value={JSON.stringify(step.config?.mapping ?? {}, null, 2)} onChange={(event) => {
                            try {
                              const parsed = JSON.parse(event.target.value);
                              setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), mapping: parsed } } : item));
                            } catch {}
                          }} style={{ minHeight: 100, borderRadius: 10, border: "1px solid " + theme.border, background: theme.editor, color: theme.codeText, padding: 10, fontSize: "var(--rm-fs-small)", fontFamily: "var(--font-mono)" }} />
                        </div>
                      ) : null}
                      {step.type === "action" ? (
                        <div style={{ display: "grid", gap: 6 }}>
                          <InlineInput value={String(step.name ?? "")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item))} placeholder="Action name" />
                          <InlineInput value={String((step.config?.url as string | undefined) ?? "")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), url: event.target.value } } : item))} placeholder="URL" />
                          <InlineSelect value={String((step.config?.method as string | undefined) ?? "POST")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), method: event.target.value } } : item))}>
                            <option value="POST">POST</option>
                            <option value="PUT">PUT</option>
                            <option value="PATCH">PATCH</option>
                            <option value="GET">GET</option>
                          </InlineSelect>
                          <InlineInput value={String((step.config?.condition as string | undefined) ?? "")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), condition: event.target.value } } : item))} placeholder="Condition (optional)" />
                          <textarea value={JSON.stringify(step.config?.bodyTemplate ?? {}, null, 2)} onChange={(event) => {
                            try {
                              const parsed = JSON.parse(event.target.value);
                              setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), bodyTemplate: parsed } } : item));
                            } catch {}
                          }} style={{ minHeight: 110, borderRadius: 10, border: "1px solid " + theme.border, background: theme.editor, color: theme.codeText, padding: 10, fontSize: "var(--rm-fs-small)", fontFamily: "var(--font-mono)" }} />
                        </div>
                      ) : null}
                      {step.type === "review_gate" ? (
                        <div style={{ display: "grid", gap: 6 }}>
                          <InlineInput value={String(step.name ?? "")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, name: event.target.value } : item))} placeholder="Review gate name" />
                          <InlineInput value={String((step.config?.assignTo as string | undefined) ?? "underwriting_queue")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), assignTo: event.target.value } } : item))} placeholder="Queue" />
                          <InlineInput value={String((step.config?.condition as string | undefined) ?? "")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), condition: event.target.value } } : item))} placeholder="Condition" />
                          <InlineSelect value={String((step.config?.onTimeout as string | undefined) ?? "reject")} onChange={(event) => setSteps((items) => items.map((item, itemIndex) => itemIndex === index ? { ...item, config: { ...(item.config ?? {}), onTimeout: event.target.value } } : item))}>
                            <option value="reject">Reject</option>
                            <option value="approve">Approve</option>
                            <option value="escalate">Escalate</option>
                          </InlineSelect>
                        </div>
                      ) : null}
                      <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted, display: "inline-flex", alignItems: "center", gap: 6 }}>
                        {isConnector ? <ConnectorIcon connectorId={stepRef} color={connectorMap[stepRef]?.color} size={12} /> : null}
                        <span>{label}</span>
                      </div>
                    </div>
                    {index < steps.length - 1 ? <span style={{ color: theme.dim }}>→</span> : null}
                  </React.Fragment>
                );
              })
            )}
          </div>

          {meceResult ? (
            <div data-testid="mece-diagnostics" style={{ background: meceResult.isMutuallyExclusive && meceResult.isCollectivelyExhaustive ? "rgba(34,197,94,0.08)" : "rgba(239,68,68,0.08)", borderRadius: 12, padding: 14, border: "1px solid " + (meceResult.isMutuallyExclusive && meceResult.isCollectivelyExhaustive ? "rgba(34,197,94,0.3)" : "rgba(239,68,68,0.3)") }}>
              <div style={{ display: "flex", gap: 12, alignItems: "center", marginBottom: 10 }}>
                <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: meceResult.isMutuallyExclusive && meceResult.isCollectivelyExhaustive ? "#22c55e" : "#ef4444" }}>
                  {meceResult.isMutuallyExclusive && meceResult.isCollectivelyExhaustive ? "MECE CHECK PASSED" : "MECE ISSUES DETECTED"}
                </div>
                <span style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>
                  {meceResult.ruleCount} rules analyzed across {meceResult.analyzedFields.length} fields
                </span>
              </div>
              <div style={{ display: "flex", gap: 8, marginBottom: 10 }}>
                <span style={{ padding: "2px 8px", borderRadius: 6, fontSize: "var(--rm-fs-caption)", fontWeight: 600, background: meceResult.isMutuallyExclusive ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)", color: meceResult.isMutuallyExclusive ? "#22c55e" : "#ef4444" }}>
                  ME: {meceResult.isMutuallyExclusive ? "PASS" : "FAIL"}
                </span>
                <span style={{ padding: "2px 8px", borderRadius: 6, fontSize: "var(--rm-fs-caption)", fontWeight: 600, background: meceResult.isCollectivelyExhaustive ? "rgba(34,197,94,0.15)" : "rgba(239,68,68,0.15)", color: meceResult.isCollectivelyExhaustive ? "#22c55e" : "#ef4444" }}>
                  CE: {meceResult.isCollectivelyExhaustive ? "PASS" : "FAIL"}
                </span>
                {meceResult.hasOpaqueConstraints ? (
                  <span style={{ padding: "2px 8px", borderRadius: 6, fontSize: "var(--rm-fs-caption)", fontWeight: 600, background: "rgba(234,179,8,0.15)", color: "#eab308" }}>OPAQUE OPS</span>
                ) : null}
              </div>
              {meceResult.diagnostics.length > 0 ? (
                <div style={{ display: "grid", gap: 6 }}>
                  {meceResult.diagnostics.map((d, i) => (
                    <div key={i} style={{ background: theme.card, borderRadius: 8, padding: 10, border: "1px solid " + (d.severity === "error" ? "rgba(239,68,68,0.3)" : d.severity === "warning" ? "rgba(234,179,8,0.3)" : theme.border), display: "grid", gap: 4 }}>
                      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                        <span style={{ padding: "1px 6px", borderRadius: 4, fontSize: "var(--rm-fs-caption)", fontWeight: 600, textTransform: "uppercase", background: d.type === "overlap" ? "rgba(239,68,68,0.15)" : d.type === "gap" ? "rgba(234,179,8,0.15)" : "rgba(96,165,250,0.15)", color: d.type === "overlap" ? "#ef4444" : d.type === "gap" ? "#eab308" : "#60a5fa" }}>
                          {d.type}
                        </span>
                        <span style={{ padding: "1px 6px", borderRadius: 4, fontSize: "var(--rm-fs-caption)", background: d.severity === "error" ? "rgba(239,68,68,0.1)" : d.severity === "warning" ? "rgba(234,179,8,0.1)" : "rgba(96,165,250,0.1)", color: d.severity === "error" ? "#ef4444" : d.severity === "warning" ? "#eab308" : "#60a5fa" }}>
                          {d.severity}
                        </span>
                        {d.fields.length > 0 ? <span style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted }}>fields: {d.fields.join(", ")}</span> : null}
                      </div>
                      <div style={{ fontSize: "var(--rm-fs-small)", color: theme.text }}>{d.description}</div>
                      {d.involvedRules.length > 0 ? (
                        <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted }}>
                          Rules: {d.involvedRules.map((rId) => props.data.rules.find((r) => r.id === rId)?.name ?? rId).join(", ")}
                        </div>
                      ) : null}
                    </div>
                  ))}
                </div>
              ) : null}
              {meceResult.warnings.length > 0 ? (
                <div style={{ marginTop: 8, fontSize: "var(--rm-fs-caption)", color: "#eab308" }}>
                  {meceResult.warnings.map((w, i) => <div key={i}>{w}</div>)}
                </div>
              ) : null}
            </div>
          ) : null}

          {execution ? (
            <div style={{ background: theme.hover, borderRadius: 12, padding: 14 }}>
              <div data-testid="policy-outcome" style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: execution.result.outcome === "approve" ? theme.success : execution.result.outcome === "review" ? theme.warning : theme.danger, textTransform: "uppercase", marginBottom: 10 }}>
                {execution.result.outcome}
              </div>
              <DecisionFlow trace={execution.result.trace as unknown as Array<Record<string, any>>} outcome={String(execution.result.outcome)} />
            </div>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function decisionStepStatus(entry: Record<string, any>): { key: string; label: string } {
  if (entry.skipped) return { key: "skipped", label: "Skipped" };
  if (entry.error) return { key: "error", label: "Error" };
  const result = entry.result;
  if (result && typeof result === "object") {
    if (result.passed === true) return { key: "pass", label: "Pass" };
    if (result.passed === false) return { key: "fail", label: "Fail" };
    if (typeof result.outcome === "string") return { key: result.outcome, label: String(result.outcome) };
    if (typeof result.score === "number") return { key: "info", label: "Scored" };
  }
  return { key: "neutral", label: "Done" };
}

// Node-wise decision-flow visualization: renders the execution trace as a
// connected sequence of steps, showing where each check passed/failed and where
// the final outcome was decided. Used by the Policy designer and Test Console.
function DecisionFlow(props: { trace: Array<Record<string, any>>; outcome: string }) {
  const theme = useTheme();
  const colorFor = (key: string): string => {
    if (key === "pass" || key === "approve") return theme.success;
    if (key === "fail" || key === "reject" || key === "error") return theme.danger;
    if (key === "review") return theme.warning;
    if (key === "info") return theme.accent;
    return theme.muted;
  };
  // The deciding step is the last one that set/merged the winning outcome.
  const decidingIndex = (() => {
    for (let i = props.trace.length - 1; i >= 0; i -= 1) {
      const result = props.trace[i]?.result;
      const stepType = props.trace[i]?.step?.type;
      if (result && (result.outcome === props.outcome || (stepType === "outcome"))) return i;
      if (result && result.passed === false && (props.outcome === "reject" || props.outcome === "review")) return i;
    }
    return props.trace.length - 1;
  })();

  return (
    <div data-testid="decision-flow" style={{ display: "grid", gap: 0 }}>
      {props.trace.map((entry, index) => {
        const status = decisionStepStatus(entry);
        const color = colorFor(status.key);
        const step = entry.step ?? {};
        const stepType = String(step.type ?? entry.type ?? "step");
        const label = String(step.label ?? step.ref_id ?? entry.label ?? entry.ref_id ?? stepType);
        const result = entry.result ?? {};
        const conditions: Array<Record<string, any>> = Array.isArray(result.conditions) ? result.conditions : [];
        const isDeciding = index === decidingIndex;
        return (
          <div key={index} style={{ display: "grid", gridTemplateColumns: "28px 1fr", gap: 12 }}>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center" }}>
              <div style={{ width: 26, height: 26, borderRadius: "50%", background: color, color: "#fff", display: "grid", placeItems: "center", fontSize: 12, fontWeight: 700, flex: "none" }}>{index + 1}</div>
              {index < props.trace.length - 1 ? <div style={{ width: 2, flex: 1, minHeight: 14, background: theme.border }} /> : null}
            </div>
            <div style={{ paddingBottom: 12 }}>
              <div style={{ background: theme.card, border: "1px solid " + (isDeciding ? color : theme.border), borderRadius: 10, padding: "10px 12px", boxShadow: isDeciding ? "0 0 0 3px " + color + "22" : "none" }}>
                <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                  <div style={{ fontSize: "var(--rm-fs-small)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>
                    <span style={{ textTransform: "uppercase", color: theme.muted, letterSpacing: "0.04em", fontSize: "var(--rm-fs-caption)" }}>{stepType}</span> · {label}
                  </div>
                  <span style={{ fontSize: "var(--rm-fs-caption)", fontWeight: 700, color, background: color + "1e", borderRadius: 999, padding: "2px 9px", textTransform: "uppercase" }}>{status.label}</span>
                </div>
                {typeof result.score === "number" ? (
                  <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted, marginTop: 4, fontFamily: "var(--font-mono)" }}>score = {result.score}</div>
                ) : null}
                {entry.reason ? (
                  <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted, marginTop: 4 }}>{String(entry.reason)}</div>
                ) : null}
                {conditions.length ? (
                  <div style={{ display: "grid", gap: 4, marginTop: 8 }}>
                    {conditions.map((cond, ci) => (
                      <div key={ci} style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8, fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-caption)" }}>
                        <span style={{ color: theme.muted }}>{String(cond.variable_name ?? cond.variable_id ?? "")} {String(cond.operator ?? "")} {String(cond.threshold ?? "")}</span>
                        <span style={{ color: cond.passed ? theme.success : theme.danger, fontWeight: 700 }}>{String(cond.value ?? "∅")} {cond.passed ? "✓" : "✗"}</span>
                      </div>
                    ))}
                  </div>
                ) : null}
                {isDeciding ? (
                  <div style={{ fontSize: "var(--rm-fs-caption)", color, marginTop: 8, fontWeight: 700 }}>→ Decision made here: {props.outcome.toUpperCase()}</div>
                ) : null}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

function TestingPage(props: { data: BootstrapPayload; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, environment, isMobile } = useRuleMindStore();
  const environmentRules = props.data.rules.filter((rule) => rule.status === environment);
  const environmentPolicies = props.data.policies.filter((policy) => policy.status === environment);
  const [variableResults, setVariableResults] = React.useState<VariableBatchTestResponse | null>(null);
  const [ruleId, setRuleId] = React.useState(environmentRules[0]?.id ?? "");
  const [policyId, setPolicyId] = React.useState(environmentPolicies[0]?.id ?? "");
  const [ruleResult, setRuleResult] = React.useState<RuleTestResponse | null>(null);
  const [policyResult, setPolicyResult] = React.useState<PolicyExecuteResponse | null>(null);
  const [batchTargetType, setBatchTargetType] = React.useState<"rule" | "policy" | "decide">("policy");
  const [batchPayloadText, setBatchPayloadText] = React.useState('[\n  {}\n]');
  const [batchResult, setBatchResult] = React.useState<Record<string, unknown> | null>(null);
  const [apiPayloadText, setApiPayloadText] = React.useState('{\n  "bureau": {},\n  "bank": {},\n  "kyc": {}\n}');
  const [apiResult, setApiResult] = React.useState<Record<string, unknown> | null>(null);
  const [busy, setBusy] = React.useState(false);

  const runAllVariables = React.useCallback(async () => {
    setBusy(true);
    try {
      const response = await apiJson<VariableBatchTestResponse>(apiBaseUrl, "/api/v1/test/variables", { method: "POST", body: JSON.stringify({ payload: {} }) }, apiKey);
      setVariableResults(response);
      props.onNotify("Variable suite completed.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Variable suite failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props]);

  const runRule = React.useCallback(async () => {
    if (!ruleId) return;
    setBusy(true);
    try {
      const response = await apiJson<RuleTestResponse>(apiBaseUrl, "/api/v1/test/rule/" + ruleId, { method: "POST", body: JSON.stringify({ payload: {} }) }, apiKey);
      setRuleResult(response);
      props.onNotify("Rule test completed.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Rule test failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, props, ruleId]);

  const runPolicy = React.useCallback(async () => {
    if (!policyId) return;
    setBusy(true);
    try {
      const response = await apiJson<PolicyExecuteResponse>(apiBaseUrl, "/api/v1/test/policy/" + policyId, { method: "POST", body: JSON.stringify({ payload: {} }) }, apiKey);
      setPolicyResult(response);
      props.onNotify("Policy trace completed.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Policy test failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, policyId, props]);

  const runBatchSimulation = React.useCallback(async () => {
    setBusy(true);
    try {
      const payloads = JSON.parse(batchPayloadText);
      const targetId = batchTargetType === "rule" ? ruleId : policyId;
      const response = await apiJson<Record<string, unknown>>(
        apiBaseUrl,
        "/api/v1/test/batch",
        {
          method: "POST",
          body: JSON.stringify({
            targetType: batchTargetType,
            targetId,
            payloads,
          }),
        },
        apiKey
      );
      setBatchResult(response);
      props.onNotify("Batch simulation completed.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Batch simulation failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, batchPayloadText, batchTargetType, policyId, props, ruleId]);

  const runApiSimulation = React.useCallback(async () => {
    if (!policyId) {
      return;
    }
    setBusy(true);
    try {
      const response = await apiJson<Record<string, unknown>>(
        apiBaseUrl,
        "/api/v1/decide",
        {
          method: "POST",
          body: JSON.stringify({
            policy_id: policyId,
            payload: JSON.parse(apiPayloadText),
          }),
        },
        apiKey
      );
      setApiResult(response);
      props.onNotify("Decision API simulation completed.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Decision API simulation failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, apiPayloadText, policyId, props]);

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <SectionHeader title={PAGE_META.testing.title} subtitle={PAGE_META.testing.subtitle} actions={<Button variant="primary" onClick={runAllVariables} disabled={busy} testId="testing-run-all">Run All Tests</Button>} />

      <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, display: "flex", justifyContent: "space-between" }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Variable suite</div>
          <div style={{ fontSize: "var(--rm-fs-small)", color: theme.success, fontWeight: "var(--rm-fw-bold)" as unknown as number }}>{variableResults?.summary ?? "Not run yet"}</div>
        </div>
        {!variableResults ? (
          <div style={{ padding: 22 }}>
            <EmptyState icon={<FileSearch size={28} />} title="Ready to test all variables" description="Run the suite to validate the active sample payload against every variable." />
          </div>
        ) : (
          <div>
            {variableResults.results.map((result) => (
              <div key={result.id} style={{ padding: "10px 14px", borderTop: "1px solid " + theme.border, display: "flex", justifyContent: "space-between", alignItems: "center", gap: 12 }}>
                <div style={{ display: "grid", gap: 3 }}>
                  <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-semibold)" as unknown as number, color: theme.text, display: "inline-flex", alignItems: "center", gap: 6 }}>
                    <ConnectorIcon connectorId={result.source_id} color={props.data.connectors.find((connector) => connector.id === result.source_id)?.color} size={14} />
                    <span>{result.name}</span>
                  </div>
                  <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted }}>{result.category} · {result.source_id}</div>
                </div>
                <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                  <span style={{ fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: result.passed ? theme.success : theme.warning }}>{String(result.computed_value)}</span>
                  <StatusBadge status={result.passed ? "prod" : "uat"} />
                </div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr", gap: 16 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Rule tester</div>
          <div style={{ display: "flex", gap: 8 }}>
            <InlineSelect value={ruleId} onChange={(event) => setRuleId(event.target.value)} style={{ flex: 1 }}>
              {environmentRules.map((rule) => (
                <option key={rule.id} value={rule.id}>
                  {rule.name}
                </option>
              ))}
            </InlineSelect>
            <Button onClick={runRule} disabled={!ruleId || busy}>Execute</Button>
          </div>
          {ruleResult ? (
            <div style={{ display: "grid", gap: 6 }}>
              <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: ruleResult.result.passed ? theme.success : theme.danger, textTransform: "uppercase" }}>{ruleResult.result.outcome}</div>
              {ruleResult.result.conditions.map((condition) => (
                <div key={condition.variable_id + condition.threshold} style={{ fontSize: "var(--rm-fs-small)", color: theme.text }}>
                  <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                    {condition.passed ? <CheckCircle2 size={14} /> : <XCircle size={14} />}
                    <span>
                      {condition.variable_name}: {String(condition.value)} {condition.operator} {condition.threshold}
                    </span>
                  </span>
                </div>
              ))}
            </div>
          ) : null}
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Policy trace</div>
          <div style={{ display: "flex", gap: 8 }}>
            <InlineSelect value={policyId} onChange={(event) => setPolicyId(event.target.value)} style={{ flex: 1 }}>
              {environmentPolicies.map((policy) => (
                <option key={policy.id} value={policy.id}>
                  {policy.name}
                </option>
              ))}
            </InlineSelect>
            <Button onClick={runPolicy} disabled={!policyId || busy}>Execute</Button>
          </div>
          {policyResult ? (
            <div style={{ display: "grid", gap: 6 }}>
              <div data-testid="policy-console-outcome" style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: policyResult.result.outcome === "approve" ? theme.success : policyResult.result.outcome === "review" ? theme.warning : theme.danger, textTransform: "uppercase" }}>
                {policyResult.result.outcome}
              </div>
              <DecisionFlow trace={policyResult.result.trace as unknown as Array<Record<string, any>>} outcome={String(policyResult.result.outcome)} />
            </div>
          ) : null}
        </div>
      </div>

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1fr 1fr", gap: 16 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Batch simulation</div>
          <div style={{ display: "flex", gap: 8 }}>
            <InlineSelect value={batchTargetType} onChange={(event) => setBatchTargetType(event.target.value as "rule" | "policy" | "decide")} style={{ width: 140 }}>
              <option value="rule">Rule</option>
              <option value="policy">Policy</option>
              <option value="decide">Decide</option>
            </InlineSelect>
            {batchTargetType === "rule" ? (
              <InlineSelect value={ruleId} onChange={(event) => setRuleId(event.target.value)} style={{ flex: 1 }}>
                {environmentRules.map((rule) => (
                  <option key={rule.id} value={rule.id}>
                    {rule.name}
                  </option>
                ))}
              </InlineSelect>
            ) : (
              <InlineSelect value={policyId} onChange={(event) => setPolicyId(event.target.value)} style={{ flex: 1 }}>
                {environmentPolicies.map((policy) => (
                  <option key={policy.id} value={policy.id}>
                    {policy.name}
                  </option>
                ))}
              </InlineSelect>
            )}
          </div>
          <InlineTextarea code value={batchPayloadText} onChange={(event) => setBatchPayloadText(event.target.value)} rows={10} />
          <Button onClick={runBatchSimulation} disabled={busy}>Run batch</Button>
          {batchResult ? (
            <pre style={{ margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-small)", color: theme.muted, whiteSpace: "pre-wrap", maxHeight: 220, overflow: "auto" }}>
              {JSON.stringify(batchResult, null, 2)}
            </pre>
          ) : null}
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Decision API simulation</div>
          <InlineSelect value={policyId} onChange={(event) => setPolicyId(event.target.value)} style={{ maxWidth: 280 }}>
            {environmentPolicies.map((policy) => (
              <option key={policy.id} value={policy.id}>
                {policy.name}
              </option>
            ))}
          </InlineSelect>
          <InlineTextarea code value={apiPayloadText} onChange={(event) => setApiPayloadText(event.target.value)} rows={10} />
          <Button onClick={runApiSimulation} disabled={busy || !policyId}>POST /api/v1/decide</Button>
          {apiResult ? (
            <pre style={{ margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-small)", color: theme.muted, whiteSpace: "pre-wrap", maxHeight: 220, overflow: "auto" }}>
              {JSON.stringify(apiResult, null, 2)}
            </pre>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function AuditPage(props: { onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, isMobile } = useRuleMindStore();
  const [tab, setTab] = React.useState<"decisions" | "promotions" | "errors">("decisions");
  const [decisions, setDecisions] = React.useState<Array<Record<string, unknown>>>([]);
  const [promotions, setPromotions] = React.useState<PromotionRecord[]>([]);
  const [errors, setErrors] = React.useState<AuditErrorRecord[]>([]);
  const [selected, setSelected] = React.useState<Record<string, unknown> | null>(null);
  const [live, setLive] = React.useState(false);
  const [replay, setReplay] = React.useState<{ original_outcome: string; replayed_outcome: string; changed: boolean; bundle_version: number } | null>(null);
  const [replayBusy, setReplayBusy] = React.useState(false);

  const runReplay = React.useCallback(async (decisionId: string) => {
    setReplayBusy(true);
    setReplay(null);
    try {
      const res = await apiJson<{ original_outcome: string; replayed_outcome: string; changed: boolean; bundle_version: number }>(
        apiBaseUrl, "/api/v1/decisions/" + decisionId + "/replay", { method: "POST" }, apiKey,
      );
      setReplay(res);
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Replay failed.");
    } finally {
      setReplayBusy(false);
    }
  }, [apiBaseUrl, apiKey, props]);

  React.useEffect(() => {
    Promise.all([
      apiJson<Array<Record<string, unknown>>>(apiBaseUrl, "/api/v1/audit/decisions", {}, apiKey),
      apiJson<PromotionRecord[]>(apiBaseUrl, "/api/v1/audit/promotions", {}, apiKey),
      apiJson<AuditErrorRecord[]>(apiBaseUrl, "/api/v1/audit/errors", {}, apiKey),
    ])
      .then(([decisionRows, promotionRows, errorRows]) => {
        setDecisions(decisionRows);
        setPromotions(promotionRows);
        setErrors(errorRows);
      })
      .catch((error) => props.onNotify(error instanceof Error ? error.message : "Unable to load audit data."));
  }, [apiBaseUrl, apiKey, props]);

  // Live feed: when enabled, subscribe to the SSE decision stream and prepend new decisions
  // (deduped by id, capped). The opening backlog the stream sends is filtered against what we
  // already have so we don't double-list rows from the initial fetch above.
  React.useEffect(() => {
    if (!live) return;
    const stop = streamDecisions(
      apiBaseUrl,
      apiKey,
      (decision: StreamedDecision) => {
        if (!decision.id) return;
        setDecisions((prev) => {
          if (prev.some((row) => row.id === decision.id)) return prev;
          return [decision as Record<string, unknown>, ...prev].slice(0, 500);
        });
      },
      (error) => props.onNotify(error instanceof Error ? error.message : "Live feed disconnected."),
    );
    return stop;
  }, [live, apiBaseUrl, apiKey, props]);

  const rows =
    tab === "decisions"
      ? decisions
      : tab === "promotions"
        ? promotions
        : errors;

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <SectionHeader title={PAGE_META.audit.title} subtitle={PAGE_META.audit.subtitle} />
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {[
          { id: "decisions", label: "Decision History" },
          { id: "promotions", label: "Promotion History" },
          { id: "errors", label: "Error Events" },
        ].map((item) => (
          <Button key={item.id} variant={tab === item.id ? "primary" : "default"} onClick={() => setTab(item.id as "decisions" | "promotions" | "errors")}>
            {item.label}
          </Button>
        ))}
        {tab === "decisions" ? (
          <div style={{ marginLeft: "auto" }}>
            <Button variant={live ? "primary" : "default"} onClick={() => setLive((value) => !value)}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: live ? theme.success : theme.dim,
                    boxShadow: live ? "0 0 0 3px " + theme.successBg : "none",
                  }}
                />
                {live ? "Live" : "Go live"}
              </span>
            </Button>
          </div>
        ) : null}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1.1fr 0.9fr", gap: 16 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>
            {tab === "decisions" ? "Recent decisions" : tab === "promotions" ? "Recent promotions" : "Recent operational errors"}
          </div>
          <div style={{ display: "grid" }}>
            {rows.length === 0 ? (
              <div style={{ padding: 18, fontSize: "var(--rm-fs-body)", color: theme.dim }}>No audit events yet.</div>
            ) : (
              rows.map((row, index) => {
                const record = row as Record<string, unknown>;
                return (
                  <button
                    key={String(record.id ?? index)}
                    type="button"
                    onClick={() => { setSelected(record); setReplay(null); }}
                    style={{
                      textAlign: "left",
                      border: "none",
                      borderTop: index === 0 ? "none" : "1px solid " + theme.border,
                      background: "transparent",
                      padding: "12px 14px",
                      color: theme.text,
                      cursor: "pointer",
                      display: "grid",
                      gap: 4,
                    }}
                  >
                    <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-semibold)" as unknown as number }}>
                      {tab === "decisions"
                        ? `${String(record.policy_id ?? "policy")} → ${String(record.outcome ?? "")}`
                        : tab === "promotions"
                          ? `${String(record.entity_type ?? "")} · ${String(record.entity_id ?? "")}`
                          : `${String(record.scope ?? "")} · ${String(record.message ?? "")}`}
                    </div>
                    <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>
                      {String(record.created_at ?? "")}
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Detail drawer</div>
          {!selected ? (
            <div style={{ padding: 18, fontSize: "var(--rm-fs-body)", color: theme.dim }}>Select an audit row to inspect payload, trace, and metadata.</div>
          ) : (
            <div style={{ display: "grid", gap: 12, padding: 14, maxHeight: 520, overflow: "auto" }}>
              {selected.id ? (
                <div style={{ background: theme.hover, border: "1px solid " + theme.border, borderRadius: 10, padding: 10, display: "grid", gap: 8 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                    <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Replay vs current policy</div>
                    <Button small variant="default" disabled={replayBusy} onClick={() => runReplay(String(selected.id))}>{replayBusy ? "Replaying…" : "Replay"}</Button>
                  </div>
                  {replay ? (
                    <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                      <span>original <strong style={{ color: theme.text }}>{replay.original_outcome}</strong></span>
                      <span>→ now <strong style={{ color: theme.text }}>{replay.replayed_outcome}</strong></span>
                      <span style={{ padding: "2px 8px", borderRadius: 999, background: replay.changed ? theme.warningBg : theme.successBg, color: replay.changed ? theme.warning : theme.success, fontWeight: "var(--rm-fw-bold)" as unknown as number }}>
                        {replay.changed ? "CHANGED" : "same"}
                      </span>
                      <span style={{ color: theme.dim }}>bundle v{replay.bundle_version}</span>
                    </div>
                  ) : (
                    <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.dim }}>Re-run this decision&apos;s inputs through the current policy to see if the outcome would change.</div>
                  )}
                </div>
              ) : null}
              {Array.isArray((selected as { trace?: unknown[] }).trace) ? (
                <div style={{ display: "grid", gap: 8 }}>
                  <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Execution Trace</div>
                  {(selected as { trace?: ExecutionTraceStepRecord[] }).trace?.map((entry, index) => (
                    <div key={index} style={{ background: theme.hover, border: "1px solid " + theme.border, borderRadius: 10, padding: 10, display: "grid", gap: 4 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                        <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>{index + 1}. {String(entry.step?.name ?? entry.step?.label ?? entry.type ?? entry.step?.type ?? "step")}</div>
                        <div style={{ fontSize: "var(--rm-fs-small)", color: entry.error ? theme.danger : entry.skipped ? theme.warning : theme.success }}>
                          {entry.error ? "FAILED" : entry.skipped ? "SKIPPED" : "OK"}
                        </div>
                      </div>
                      <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>
                        {String(entry.duration_ms ?? entry.result?.status ?? entry.result?.outcome ?? "")}
                      </div>
                      <pre style={{ margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-caption)", color: theme.dim, whiteSpace: "pre-wrap" }}>{JSON.stringify(entry, null, 2)}</pre>
                    </div>
                  ))}
                </div>
              ) : null}
              <pre style={{ margin: 0, padding: 12, fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-small)", color: theme.muted, whiteSpace: "pre-wrap", background: theme.editor, borderRadius: 10 }}>
                {JSON.stringify(selected, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

type PolicyDiffBucket = { added: string[]; removed: string[]; changed: string[] };
type PolicyDiff = {
  hasBaseline: boolean;
  changed: boolean;
  steps: { added: Array<{ type: string; ref_id: string }>; removed: Array<{ type: string; ref_id: string }> };
  rules: PolicyDiffBucket;
  scorecards: PolicyDiffBucket;
  decisionTables: PolicyDiffBucket;
};

function summarizePolicyDiff(diff: PolicyDiff): string {
  if (!diff.hasBaseline) return "First promotion — the whole policy is new.";
  const parts: string[] = [];
  if (diff.steps.added.length) parts.push("+" + diff.steps.added.length + " step(s)");
  if (diff.steps.removed.length) parts.push("−" + diff.steps.removed.length + " step(s)");
  const count = (b: PolicyDiffBucket) => b.added.length + b.removed.length + b.changed.length;
  if (count(diff.rules)) parts.push(count(diff.rules) + " rule change(s)");
  if (count(diff.scorecards)) parts.push(count(diff.scorecards) + " scorecard change(s)");
  if (count(diff.decisionTables)) parts.push(count(diff.decisionTables) + " decision-table change(s)");
  return parts.length
    ? "Changes since the last live version: " + parts.join(", ") + "."
    : "No decision-logic changes since the last promotion.";
}

function DeployPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, isMobile } = useRuleMindStore();
  const [deployData, setDeployData] = React.useState<DeployStatusPayload | null>(null);
  const [busyKey, setBusyKey] = React.useState<string | null>(null);

  React.useEffect(() => {
    apiJson<DeployStatusPayload>(apiBaseUrl, "/api/v1/deploy/status", {}, apiKey)
      .then(setDeployData)
      .catch(() => setDeployData(null));
  }, [apiBaseUrl, apiKey, props.data]);

  const promote = React.useCallback(
    async (entityType: "variable" | "rule" | "scorecard" | "policy", entityId: string) => {
      // For a policy, show exactly what decision logic changed since the last promotion before
      // shipping it — and record that summary on the approval.
      let changeSummary = "";
      if (entityType === "policy") {
        try {
          const diff = await apiJson<PolicyDiff>(apiBaseUrl, "/api/v1/policies/" + entityId + "/diff", {}, apiKey);
          changeSummary = summarizePolicyDiff(diff);
        } catch {
          /* diff is best-effort context; fall through to a plain confirm */
        }
      }
      const prompt = changeSummary
        ? changeSummary + "\n\nPromote this policy to the next environment?"
        : "Promote this item to the next environment?";
      if (!window.confirm(prompt)) {
        return;
      }
      setBusyKey(entityType + ":" + entityId);
      try {
        const route = "/api/v1/" + entityType + "s/" + entityId + "/promote";
        const reason = "Manual promotion from Deploy board." + (changeSummary ? " " + changeSummary : "");
        await apiJson(apiBaseUrl, route, { method: "POST", body: JSON.stringify({ promoted_by: "web", reason }) }, apiKey);
        props.refresh();
        props.onNotify("Promotion completed.");
      } catch (error) {
        props.onNotify(error instanceof Error ? error.message : "Promotion failed.");
      } finally {
        setBusyKey(null);
      }
    },
    [apiBaseUrl, apiKey, props]
  );

  const columns = deployData ?? {
    dev: { variables: [], rules: [], scorecards: [], policies: [] },
    uat: { variables: [], rules: [], scorecards: [], policies: [] },
    prod: { variables: [], rules: [], scorecards: [], policies: [] },
  };

  const recentPromotions = props.data.promotions.slice(0, 8);

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <SectionHeader title={PAGE_META.deploy.title} subtitle={PAGE_META.deploy.subtitle} />
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "repeat(3, minmax(0, 1fr))", gap: 12 }}>
        {STATUS_ORDER.map((statusName) => {
          const palette = tone(theme, statusName === "dev" ? "purple" : statusName === "uat" ? "warning" : "success");
          const column = columns[statusName];
          const items = [
            ...column.variables.map((item) => ({ entityType: "variable" as const, id: item.id, label: item.name, promotable: Boolean(item.last_test_result?.passed), subtitle: item.category })),
            ...column.rules.map((item) => ({ entityType: "rule" as const, id: item.id, label: item.name, promotable: Boolean(item.last_test_result?.passed), subtitle: item.expression })),
            ...column.scorecards.map((item) => ({ entityType: "scorecard" as const, id: item.id, label: item.name, promotable: Boolean(item.last_test_result?.passed), subtitle: "Base " + item.base_score })),
            ...column.policies.map((item) => ({ entityType: "policy" as const, id: item.id, label: item.name, promotable: Boolean(item.last_test_result?.passed), subtitle: item.steps.length + " steps" })),
          ];
          return (
            <div key={statusName} style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
              <div style={{ padding: "12px 14px", background: palette.bg, display: "flex", justifyContent: "space-between" }}>
                <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: palette.fg, textTransform: "uppercase" }}>{statusName}</div>
                <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{items.length}</div>
              </div>
              <div style={{ padding: 10, display: "grid", gap: 8 }}>
                {items.length === 0 ? <div style={{ fontSize: "var(--rm-fs-small)", color: theme.dim }}>No items.</div> : null}
                {items.map((item) => (
                  <div key={item.entityType + item.id} style={{ background: theme.hover, borderRadius: 10, padding: 10, display: "grid", gap: 6 }}>
                    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 8 }}>
                      <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>{item.label}</div>
                      {statusName !== "prod" ? (
                        <Button small variant="success" disabled={!item.promotable || busyKey === item.entityType + ":" + item.id} onClick={() => promote(item.entityType, item.id)}>
                          Promote
                        </Button>
                      ) : null}
                    </div>
                    <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted }}>{item.entityType.toUpperCase()} · {item.subtitle}</div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>

      <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Promotion audit trail</div>
        {recentPromotions.length === 0 ? (
          <div style={{ padding: 16, fontSize: "var(--rm-fs-body)", color: theme.dim }}>No promotions yet.</div>
        ) : (
          recentPromotions.map((item: PromotionRecord) => (
            <div key={item.id} style={{ padding: "10px 14px", borderTop: "1px solid " + theme.border, display: "flex", justifyContent: "space-between", gap: 12, fontSize: "var(--rm-fs-small)" }}>
              <span style={{ color: theme.text }}>{item.entity_type.toUpperCase()} · {item.entity_id}</span>
              <span style={{ color: theme.muted }}>{item.from_status.toUpperCase()} → {item.to_status.toUpperCase()} · {item.promoted_by}</span>
            </div>
          ))
        )}
      </div>
    </div>
  );
}

function ExportsPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, isMobile } = useRuleMindStore();
  const [format, setFormat] = React.useState<"json" | "yaml" | "python">("json");
  const [preview, setPreview] = React.useState("");
  const [importSummary, setImportSummary] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const loadPreview = React.useCallback(async () => {
    setBusy(true);
    try {
      const response = await apiText(apiBaseUrl, "/api/v1/export?format=" + format, {}, apiKey);
      setPreview(response.text);
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Export failed.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, format, props]);

  React.useEffect(() => {
    void loadPreview();
  }, [loadPreview]);

  const download = React.useCallback(async () => {
    try {
      const response = await apiText(apiBaseUrl, "/api/v1/export?format=" + format, {}, apiKey);
      const blob = new Blob([response.text], { type: response.response.headers.get("content-type") ?? "text/plain" });
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = "rulemind-export." + (format === "python" ? "py" : format);
      anchor.click();
      URL.revokeObjectURL(href);
      props.onNotify("Export downloaded.");
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Export download failed.");
    }
  }, [apiBaseUrl, apiKey, format, props]);

  const importConfig = React.useCallback(
    async (file: File) => {
      const text = await file.text();
      try {
        const payload = JSON.parse(text);
        if (!window.confirm("Import this configuration bundle and replace the current in-app configuration?")) {
          return;
        }
        const response = await apiJson<{ counts: Record<string, number> }>(apiBaseUrl, "/api/v1/import", { method: "POST", body: JSON.stringify(payload) }, apiKey);
        setImportSummary(Object.entries(response.counts).map(([key, value]) => `${key}: ${value}`).join(" · "));
        props.refresh();
        props.onNotify("Configuration imported.");
      } catch (error) {
        props.onNotify(error instanceof Error ? error.message : "Import failed.");
      }
    },
    [apiBaseUrl, apiKey, props]
  );

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <SectionHeader
        title={PAGE_META.exports.title}
        subtitle={PAGE_META.exports.subtitle}
        actions={
          <div style={{ display: "flex", gap: 8 }}>
            <Button onClick={loadPreview} disabled={busy}>Refresh preview</Button>
            <Button variant="primary" onClick={download} disabled={busy} testId="exports-download">
              Download
            </Button>
          </div>
        }
      />

      <div style={{ display: "flex", gap: 8 }}>
        {(["json", "yaml", "python"] as const).map((item) => (
          <Button key={item} variant={format === item ? "primary" : "default"} onClick={() => setFormat(item)} testId={"export-format-" + item}>
            {item.toUpperCase()}
          </Button>
        ))}
      </div>

      <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
        <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Preview</div>
        <pre data-testid="export-preview" style={{ margin: 0, padding: 14, whiteSpace: "pre-wrap", fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-small)", color: theme.codeText, background: theme.editor, maxHeight: 420, overflow: "auto" }}>
          {preview}
        </pre>
      </div>

      <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
        <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Import JSON</div>
        <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>Upload a previously exported JSON bundle to restore connectors, variables, rules, scorecards, policies, and settings.</div>
        {importSummary ? <InfoBanner message={"Imported " + importSummary} toneKey="success" /> : null}
        <input
          type="file"
          accept=".json,application/json"
          data-testid="import-json"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) {
              void importConfig(file);
            }
          }}
        />
      </div>
    </div>
  );
}

function SettingsPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
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
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "repeat(2, minmax(0, 1fr))", gap: 14 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>API</div>
          <InlineInput value={String(settings.api_base_url ?? "")} onChange={(event) => setSettings((current) => ({ ...current, api_base_url: event.target.value }))} placeholder="Base URL" />
          <InlineSelect value={String(settings.auth_config?.method ?? "none")} onChange={(event) => setSettings((current) => ({ ...current, auth_config: { ...current.auth_config, method: event.target.value } }))}>
            <option value="none">No auth</option>
            <option value="apikey">API key</option>
            <option value="jwt">JWT</option>
          </InlineSelect>
          <InlineInput value={String(settings.auth_config?.decision_endpoint ?? "/api/v1/decide")} onChange={(event) => setSettings((current) => ({ ...current, auth_config: { ...current.auth_config, decision_endpoint: event.target.value } }))} placeholder="Decision endpoint" />
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Engine</div>
          <InlineInput value={String(settings.engine_config?.python_version ?? "3.9")} onChange={(event) => setSettings((current) => ({ ...current, engine_config: { ...current.engine_config, python_version: event.target.value } }))} placeholder="Python version" />
          <InlineInput type="number" value={String(settings.engine_config?.timeout_ms ?? 2000)} onChange={(event) => setSettings((current) => ({ ...current, engine_config: { ...current.engine_config, timeout_ms: Number(event.target.value || 0) } }))} placeholder="Timeout ms" />
          <InlineInput type="number" value={String(settings.engine_config?.memory_mb ?? 128)} onChange={(event) => setSettings((current) => ({ ...current, engine_config: { ...current.engine_config, memory_mb: Number(event.target.value || 0) } }))} placeholder="Memory MB" />
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Sources</div>
          <InlineInput value={String(settings.source_defaults?.default_format ?? "json")} onChange={(event) => setSettings((current) => ({ ...current, source_defaults: { ...current.source_defaults, default_format: event.target.value } }))} placeholder="Default format" />
          <InlineSelect value={String(settings.source_defaults?.batch_support ?? "enabled")} onChange={(event) => setSettings((current) => ({ ...current, source_defaults: { ...current.source_defaults, batch_support: event.target.value } }))}>
            <option value="enabled">Batch enabled</option>
            <option value="disabled">Batch disabled</option>
          </InlineSelect>
          <InlineInput value={String(settings.source_defaults?.webhook_url ?? "")} onChange={(event) => setSettings((current) => ({ ...current, source_defaults: { ...current.source_defaults, webhook_url: event.target.value } }))} placeholder="Webhook URL" />
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, display: "grid", gap: 10 }}>
          <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Audit</div>
          <InlineSelect value={String(settings.source_defaults?.logging ?? "enabled")} onChange={(event) => setSettings((current) => ({ ...current, source_defaults: { ...current.source_defaults, logging: event.target.value } }))}>
            <option value="enabled">Logging enabled</option>
            <option value="disabled">Logging disabled</option>
          </InlineSelect>
          <InlineInput type="number" value={String(settings.audit_retention_days ?? 90)} onChange={(event) => setSettings((current) => ({ ...current, audit_retention_days: Number(event.target.value || 0) }))} placeholder="Retention days" />
          <InlineSelect value={settings.theme_mode ?? "light"} onChange={(event) => setSettings((current) => ({ ...current, theme_mode: event.target.value as "light" | "dark" }))}>
            <option value="light">Light</option>
            <option value="dark">Dark</option>
          </InlineSelect>
        </div>
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
              <InlineInput type="number" value={String(dp.retention_days)} onChange={(event) => setDp((current) => (current ? { ...current, retention_days: Math.max(1, Number(event.target.value || 0)) } : current))} testId="data-protection-retention" />
              <div style={{ fontSize: 11.5, color: theme.muted }}>Decisions older than this are archived (if a sink is set) then purged. Minimum 1 day.</div>
            </div>
          </div>

          <div style={{ display: "grid", gap: 6 }}>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.4, color: theme.muted, fontWeight: 700 }}>Redacted PII fields (comma-separated)</div>
            <InlineInput value={dpKeys} onChange={(event) => setDpKeys(event.target.value)} placeholder="account_no, member_id, tax_id" testId="data-protection-pii" />
            <div style={{ fontSize: 11.5, color: theme.muted }}>
              These payload keys are masked to <code style={{ fontFamily: "monospace" }}>***</code> in stored decision logs, on top of built-ins ({(dp.builtin_redact_keys ?? []).join(", ") || "none"}){dp.env_redact_keys && dp.env_redact_keys.length ? ` and server keys (${dp.env_redact_keys.join(", ")})` : ""}.
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}

export function RuleMindPage(props: { page: PageId }) {
  const theme = useTheme();
  const { data, error, loading, refresh } = useBootstrapData();
  const [notice, setNotice] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!notice) {
      return undefined;
    }
    const handle = window.setTimeout(() => setNotice(null), 3000);
    return () => window.clearTimeout(handle);
  }, [notice]);

  if (loading) {
    return (
      <div style={{ padding: 24 }}>
        <EmptyState icon={<Workflow size={28} />} title="Loading RuleMind" description="Fetching connectors, variables, rules, scorecards, policies, and settings." />
      </div>
    );
  }

  if (error || !data) {
    return (
      <div style={{ padding: 24 }}>
        <InfoBanner message={error ?? "Unable to load RuleMind."} toneKey="danger" />
      </div>
    );
  }

  return (
    <div style={{ background: theme.bg, minHeight: "100%" }}>
      {notice ? (
        <div style={{ padding: "16px 20px 0" }} data-testid="app-notice">
          <InfoBanner message={notice} toneKey="success" />
        </div>
      ) : null}
      {props.page === "dashboard" ? <DashboardPage data={data} /> : null}
      {props.page === "connectors" ? <ConnectorsPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "variables" ? <VariablesPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "rules" ? <RulesPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "scorecards" ? <ScorecardsPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "policies" ? <PoliciesPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "testing" ? <TestingPage data={data} onNotify={setNotice} /> : null}
      {props.page === "deploy" ? <DeployPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "audit" ? <AuditPage onNotify={setNotice} /> : null}
      {props.page === "exports" ? <ExportsPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
      {props.page === "settings" ? <SettingsPage data={data} refresh={refresh} onNotify={setNotice} /> : null}
    </div>
  );
}
