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

export function DashboardPage(props: { data: BootstrapPayload }) {
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

