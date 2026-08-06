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

export function ScorecardsPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
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

