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
} from "./kit";
import { DashboardPage } from "./pages/DashboardPage";
import { ConnectorsPage } from "./pages/ConnectorsPage";
import { VariablesPage } from "./pages/VariablesPage";
import { RulesPage } from "./pages/RulesPage";
import { ScorecardsPage } from "./pages/ScorecardsPage";
import { PoliciesPage } from "./pages/PoliciesPage";
import { TestingPage } from "./pages/TestingPage";
import { AuditPage } from "./pages/AuditPage";
import { DeployPage } from "./pages/DeployPage";
import { ExportsPage } from "./pages/ExportsPage";
import { SettingsPage } from "./pages/SettingsPage";

export function RuleMindPage(props: { page: PageId }) {
  const theme = useTheme();
  const { data, error, loading, refresh, needsSetup } = useBootstrapData();
  const [notice, setNotice] = React.useState<string | null>(null);

  React.useEffect(() => {
    if (!notice) {
      return undefined;
    }
    const handle = window.setTimeout(() => setNotice(null), 3000);
    return () => window.clearTimeout(handle);
  }, [notice]);

  if (needsSetup) {
    return (
      <div style={{ padding: 24 }}>
        <EmptyState
          icon={<Workflow size={28} />}
          title="Connect RuleMind to get started"
          description="No API key is configured yet. Sign in, or add a workspace API key in Settings, to load your connectors, variables, rules, and policies."
        />
      </div>
    );
  }

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

