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

export function ConnectorsPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
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

