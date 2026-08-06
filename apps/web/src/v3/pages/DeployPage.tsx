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

type PolicyBacktest = {
  policy_id: string;
  bundle_version: number | null;
  sample: number;
  changed: number;
  change_rate_pct: number;
  errors: number;
  transition_matrix: { from: string; to: string; count: number }[];
};

function summarizeBacktest(bt: PolicyBacktest): string {
  if (!bt.sample) return "Backtest: no recent decisions for this policy to measure against.";
  const flips = bt.transition_matrix
    .filter((t) => t.from !== t.to)
    .slice(0, 4)
    .map((t) => t.from + "→" + t.to + " (" + t.count + ")");
  const head = "Backtest vs " + bt.sample + " recent decision(s): " + bt.changed + " would change (" + bt.change_rate_pct + "%)";
  return flips.length ? head + " — " + flips.join(", ") + "." : head + ".";
}

export function DeployPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
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
      let impactSummary = "";
      if (entityType === "policy") {
        try {
          const diff = await apiJson<PolicyDiff>(apiBaseUrl, "/api/v1/policies/" + entityId + "/diff", {}, apiKey);
          changeSummary = summarizePolicyDiff(diff);
        } catch {
          /* diff is best-effort context; fall through to a plain confirm */
        }
        try {
          // Measured impact of the current bundle on this policy's real recent traffic.
          const bt = await apiJson<PolicyBacktest>(apiBaseUrl, "/api/v1/policies/" + entityId + "/backtest?sample=200", { method: "POST" }, apiKey);
          impactSummary = summarizeBacktest(bt);
        } catch {
          /* backtest is best-effort context */
        }
      }
      const context = [changeSummary, impactSummary].filter(Boolean).join("\n");
      const prompt = context
        ? context + "\n\nPromote this policy to the next environment?"
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

