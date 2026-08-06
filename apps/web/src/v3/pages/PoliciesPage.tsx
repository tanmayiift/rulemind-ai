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
import { DecisionFlow } from "./TestingPage";

export function PoliciesPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
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

