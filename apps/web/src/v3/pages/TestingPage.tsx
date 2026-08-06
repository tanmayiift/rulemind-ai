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
export function DecisionFlow(props: { trace: Array<Record<string, any>>; outcome: string }) {
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

export function TestingPage(props: { data: BootstrapPayload; onNotify: (message: string) => void }) {
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

