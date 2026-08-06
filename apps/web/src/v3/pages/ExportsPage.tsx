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

export function ExportsPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
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

