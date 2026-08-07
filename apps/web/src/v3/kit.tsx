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
  SloConfig,
  SloStatus,
  VariableBatchTestResponse,
  VariableGraphResponse,
  VariableRecord,
} from "./types";



export type PageId =
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

export const PAGE_META: Record<PageId, { title: string; subtitle: string }> = {
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

export const NODE_TYPES: ReadonlyArray<{ type: RuleNodeRecord["type"]; label: string; icon: LucideIcon; colorKey: "accent" | "success" | "warning" | "danger" }> =
  [
    { type: "condition", label: "Condition", icon: CircleHelp, colorKey: "accent" },
    { type: "and", label: "AND", icon: GitBranch, colorKey: "success" },
    { type: "or", label: "OR", icon: Layers, colorKey: "warning" },
    { type: "approve", label: "Approve", icon: CheckCircle2, colorKey: "success" },
    { type: "review", label: "Review", icon: Eye, colorKey: "warning" },
    { type: "reject", label: "Reject", icon: XCircle, colorKey: "danger" },
  ];

export const STATUS_ORDER = ["dev", "uat", "prod"] as const;
export const CATEGORY_ORDER = ["Bureau", "Banking", "Business", "Device", "Identity", "Custom"] as const;

export function useTheme(): ThemeTokens {
  const themeMode = useRuleMindStore((state) => state.themeMode);
  return THEMES[themeMode];
}

export function useBootstrapData() {
  const hydrated = useRuleMindStore((state) => state.hydrated);
  const apiBaseUrl = useRuleMindStore((state) => state.apiBaseUrl);
  const apiKey = useRuleMindStore((state) => state.apiKey);
  const [refreshKey, setRefreshKey] = React.useState(0);
  const [data, setData] = React.useState<BootstrapPayload | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const dataRef = React.useRef<BootstrapPayload | null>(null);

  React.useEffect(() => {
    // Wait for the persisted store to rehydrate before fetching — otherwise the first run uses the
    // empty pre-hydration apiKey and 401s (a burst on every page). Once hydrated with no key, surface
    // a friendly first-run "needsSetup" state instead of firing a doomed request that dumps a raw
    // "Missing API key" error onto the dashboard.
    if (!hydrated) {
      return;
    }
    let mounted = true;
    if (!apiKey) {
      setError(null);
      setLoading(false);
      return;
    }
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
  }, [hydrated, apiBaseUrl, apiKey, refreshKey]);

  const refresh = React.useCallback(() => setRefreshKey((value) => value + 1), []);
  // needsSetup: hydrated, but no API key configured -> show the connect prompt, not a raw error.
  const needsSetup = hydrated && !apiKey;
  // Keep loading true until hydration completes so we never flash an unauthenticated state.
  return { apiBaseUrl, apiKey, data, loading: loading || !hydrated, error, refresh, needsSetup };
}

export function statusColorKey(status: string): "purple" | "warning" | "success" {
  if (status === "prod") {
    return "success";
  }
  if (status === "uat") {
    return "warning";
  }
  return "purple";
}

export function statusLabel(status: string): string {
  return status.toUpperCase();
}

export function tone(theme: ThemeTokens, toneKey: "accent" | "success" | "warning" | "danger" | "purple") {
  const mapping = {
    accent: { fg: theme.accent, bg: theme.accentBg },
    success: { fg: theme.success, bg: theme.successBg },
    warning: { fg: theme.warning, bg: theme.warningBg },
    danger: { fg: theme.danger, bg: theme.dangerBg },
    purple: { fg: theme.purple, bg: theme.purpleBg },
  };
  return mapping[toneKey];
}

export function toRuleNodeLabel(type: RuleNodeRecord["type"]): string {
  return NODE_TYPES.find((item) => item.type === type)?.label ?? type;
}

export function cloneNode(type: RuleNodeRecord["type"], defaultVariable?: VariableRecord): RuleNodeRecord {
  return {
    id: "node_" + Date.now() + "_" + Math.random().toString(16).slice(2, 8),
    type,
    label: toRuleNodeLabel(type),
    variable: type === "condition" ? defaultVariable?.id : undefined,
    operator: type === "condition" ? ">=" : undefined,
    value: type === "condition" ? "" : undefined,
  };
}

export function connectorLabel(connector: ConnectorRecord | undefined): string {
  return connector ? connector.name : "Unknown source";
}

export function sourceMark(connector: ConnectorRecord | undefined, label?: string) {
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

export function useFilteredVariables(variables: VariableRecord[], connectors: ConnectorRecord[]) {
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

export function StatCard(props: { label: string; value: string; hint: string; accent: string; onClick?: () => void; testId?: string }) {
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

export function Button(props: {
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

export function StatusBadge(props: { status: string; testId?: string }) {
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

export function InlineInput(props: React.InputHTMLAttributes<HTMLInputElement> & { testId?: string; "data-testid"?: string }) {
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

export function InlineSelect(props: React.SelectHTMLAttributes<HTMLSelectElement> & { testId?: string; "data-testid"?: string }) {
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

export function InlineTextarea(props: React.TextareaHTMLAttributes<HTMLTextAreaElement> & { testId?: string; "data-testid"?: string; code?: boolean }) {
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

export function SectionHeader(props: { title: string; subtitle?: string; actions?: React.ReactNode }) {
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

export function InfoBanner(props: { message: string; toneKey?: "accent" | "warning" | "danger" | "success" }) {
  const theme = useTheme();
  const variant = tone(theme, props.toneKey ?? "accent");
  return (
    <div style={{ marginBottom: 16, padding: "10px 12px", borderRadius: 10, background: variant.bg, color: variant.fg, fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-semibold)" as unknown as number }}>
      {props.message}
    </div>
  );
}

export function EmptyState(props: { icon: React.ReactNode; title: string; description: string }) {
  const theme = useTheme();
  return (
    <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 36, textAlign: "center" }}>
      <div style={{ fontSize: "var(--rm-fs-hero)", marginBottom: 6, display: "grid", placeItems: "center", color: theme.dim }}>{props.icon}</div>
      <div style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>{props.title}</div>
      <div style={{ fontSize: "var(--rm-fs-body)", color: theme.muted, marginTop: 6 }}>{props.description}</div>
    </div>
  );
}

