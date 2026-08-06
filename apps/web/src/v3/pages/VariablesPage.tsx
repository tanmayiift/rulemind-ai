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

export function VariablesPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
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

