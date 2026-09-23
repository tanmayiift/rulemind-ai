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

export function AuditPage(props: { onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, isMobile } = useRuleMindStore();
  const [tab, setTab] = React.useState<"decisions" | "promotions" | "errors" | "changes">("decisions");
  const [decisions, setDecisions] = React.useState<Array<Record<string, unknown>>>([]);
  const [promotions, setPromotions] = React.useState<PromotionRecord[]>([]);
  const [errors, setErrors] = React.useState<AuditErrorRecord[]>([]);
  // THE AUTHORING CHANGE TRAIL -- the one the tamper-evident hash chain actually protects.
  // GET /api/v1/audit was called by NOTHING in the web app: the page labelled "Audit Logs"
  // showed decisions, promotions and errors, and the record of who changed which rule was
  // reachable only by opening the database. `chain` is the verification report from
  // GET /api/v1/audit/verify, equally uncalled.
  const [changes, setChanges] = React.useState<Array<Record<string, unknown>>>([]);
  const [chain, setChain] = React.useState<Record<string, unknown> | null>(null);
  const [selected, setSelected] = React.useState<Record<string, unknown> | null>(null);
  const [live, setLive] = React.useState(false);
  const [replay, setReplay] = React.useState<{ original_outcome: string; replayed_outcome: string; changed: boolean; bundle_version: number } | null>(null);
  const [replayBusy, setReplayBusy] = React.useState(false);

  const runReplay = React.useCallback(async (decisionId: string) => {
    setReplayBusy(true);
    setReplay(null);
    try {
      const res = await apiJson<{ original_outcome: string; replayed_outcome: string; changed: boolean; bundle_version: number }>(
        apiBaseUrl, "/api/v1/decisions/" + decisionId + "/replay", { method: "POST" }, apiKey,
      );
      setReplay(res);
    } catch (error) {
      props.onNotify(error instanceof Error ? error.message : "Replay failed.");
    } finally {
      setReplayBusy(false);
    }
  }, [apiBaseUrl, apiKey, props]);

  React.useEffect(() => {
    Promise.all([
      apiJson<Array<Record<string, unknown>>>(apiBaseUrl, "/api/v1/audit/decisions", {}, apiKey),
      apiJson<PromotionRecord[]>(apiBaseUrl, "/api/v1/audit/promotions", {}, apiKey),
      apiJson<AuditErrorRecord[]>(apiBaseUrl, "/api/v1/audit/errors", {}, apiKey),
      apiJson<Array<Record<string, unknown>>>(apiBaseUrl, "/api/v1/audit", {}, apiKey),
      apiJson<Record<string, unknown>>(apiBaseUrl, "/api/v1/audit/verify", {}, apiKey),
    ])
      .then(([decisionRows, promotionRows, errorRows, changeRows, chainReport]) => {
        setDecisions(decisionRows);
        setPromotions(promotionRows);
        setErrors(errorRows);
        setChanges(changeRows);
        setChain(chainReport);
      })
      .catch((error) => props.onNotify(error instanceof Error ? error.message : "Unable to load audit data."));
  }, [apiBaseUrl, apiKey, props]);

  // Live feed: when enabled, subscribe to the SSE decision stream and prepend new decisions
  // (deduped by id, capped). The opening backlog the stream sends is filtered against what we
  // already have so we don't double-list rows from the initial fetch above.
  React.useEffect(() => {
    if (!live) return;
    const stop = streamDecisions(
      apiBaseUrl,
      apiKey,
      (decision: StreamedDecision) => {
        if (!decision.id) return;
        setDecisions((prev) => {
          if (prev.some((row) => row.id === decision.id)) return prev;
          return [decision as Record<string, unknown>, ...prev].slice(0, 500);
        });
      },
      (error) => props.onNotify(error instanceof Error ? error.message : "Live feed disconnected."),
    );
    return stop;
  }, [live, apiBaseUrl, apiKey, props]);

  const rows =
    tab === "decisions"
      ? decisions
      : tab === "promotions"
        ? promotions
        : tab === "changes"
          ? changes
          : errors;

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <SectionHeader title={PAGE_META.audit.title} subtitle={PAGE_META.audit.subtitle} />
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        {[
          { id: "decisions", label: "Decision History" },
          { id: "promotions", label: "Promotion History" },
          { id: "changes", label: "Change History" },
          { id: "errors", label: "Error Events" },
        ].map((item) => (
          <Button key={item.id} variant={tab === item.id ? "primary" : "default"} onClick={() => setTab(item.id as "decisions" | "promotions" | "errors" | "changes")}>
            {item.label}
          </Button>
        ))}
        {tab === "changes" && chain ? (
          <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8, fontSize: 12.5 }}>
            {/* The chain report, which nothing surfaced. A hash chain nobody can verify from the
                product is a claim rather than a control -- and `breaks` is the only field that
                distinguishes "attestable" from "someone edited the database". */}
            <span style={{ fontWeight: 600 }}>
              {Array.isArray(chain.breaks) && (chain.breaks as unknown[]).length === 0
                ? "✓ chain intact"
                : "⚠ chain broken"}
            </span>
            <span style={{ opacity: 0.7 }}>
              {String(chain.verified ?? 0)} verified
              {Number(chain.pre_chain ?? 0) > 0 ? `, ${String(chain.pre_chain)} pre-chain` : ""}
            </span>
          </div>
        ) : null}
        {tab === "decisions" ? (
          <div style={{ marginLeft: "auto" }}>
            <Button variant={live ? "primary" : "default"} onClick={() => setLive((value) => !value)}>
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <span
                  style={{
                    width: 8,
                    height: 8,
                    borderRadius: "50%",
                    background: live ? theme.success : theme.dim,
                    boxShadow: live ? "0 0 0 3px " + theme.successBg : "none",
                  }}
                />
                {live ? "Live" : "Go live"}
              </span>
            </Button>
          </div>
        ) : null}
      </div>
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1.1fr 0.9fr", gap: 16 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>
            {tab === "decisions" ? "Recent decisions"
              : tab === "promotions" ? "Recent promotions"
              : tab === "changes" ? "Authoring changes — who changed what, when"
              : "Recent operational errors"}
          </div>
          <div style={{ display: "grid" }}>
            {rows.length === 0 ? (
              <div style={{ padding: 18, fontSize: "var(--rm-fs-body)", color: theme.dim }}>No audit events yet.</div>
            ) : (
              rows.map((row, index) => {
                const record = row as Record<string, unknown>;
                return (
                  <button
                    key={String(record.id ?? index)}
                    type="button"
                    onClick={() => { setSelected(record); setReplay(null); }}
                    style={{
                      textAlign: "left",
                      border: "none",
                      borderTop: index === 0 ? "none" : "1px solid " + theme.border,
                      background: "transparent",
                      padding: "12px 14px",
                      color: theme.text,
                      cursor: "pointer",
                      display: "grid",
                      gap: 4,
                    }}
                  >
                    <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-semibold)" as unknown as number }}>
                      {tab === "decisions"
                        ? `${String(record.policy_id ?? "policy")} → ${String(record.outcome ?? "")}`
                        : tab === "promotions"
                          ? `${String(record.entity_type ?? "")} · ${String(record.entity_id ?? "")}`
                          : tab === "changes"
                            ? `${String(record.event_type ?? "change")} · ${String(record.entity_id ?? "")}`
                            : `${String(record.scope ?? "")} · ${String(record.message ?? "")}`}
                    </div>
                    <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>
                      {/* On the change trail the ACTOR is the point -- this whole tab exists
                          because "who changed which rule" was unanswerable. user_id was NULL on
                          every row until the actor was threaded into add_audit_event; a row
                          without one predates that and says so rather than showing a blank. */}
                      {tab === "changes"
                        ? `${String(record.user_id ?? "actor not recorded")} · ${String(record.created_at ?? "")}`
                        : String(record.created_at ?? "")}
                    </div>
                  </button>
                );
              })
            )}
          </div>
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, overflow: "hidden" }}>
          <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Detail drawer</div>
          {!selected ? (
            <div style={{ padding: 18, fontSize: "var(--rm-fs-body)", color: theme.dim }}>Select an audit row to inspect payload, trace, and metadata.</div>
          ) : (
            <div style={{ display: "grid", gap: 12, padding: 14, maxHeight: 520, overflow: "auto" }}>
              {selected.id ? (
                <div style={{ background: theme.hover, border: "1px solid " + theme.border, borderRadius: 10, padding: 10, display: "grid", gap: 8 }}>
                  <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
                    <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Replay vs current policy</div>
                    <Button small variant="default" disabled={replayBusy} onClick={() => runReplay(String(selected.id))}>{replayBusy ? "Replaying…" : "Replay"}</Button>
                  </div>
                  {replay ? (
                    <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                      <span>original <strong style={{ color: theme.text }}>{replay.original_outcome}</strong></span>
                      <span>→ now <strong style={{ color: theme.text }}>{replay.replayed_outcome}</strong></span>
                      <span style={{ padding: "2px 8px", borderRadius: 999, background: replay.changed ? theme.warningBg : theme.successBg, color: replay.changed ? theme.warning : theme.success, fontWeight: "var(--rm-fw-bold)" as unknown as number }}>
                        {replay.changed ? "CHANGED" : "same"}
                      </span>
                      <span style={{ color: theme.dim }}>bundle v{replay.bundle_version}</span>
                    </div>
                  ) : (
                    <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.dim }}>Re-run this decision&apos;s inputs through the current policy to see if the outcome would change.</div>
                  )}
                </div>
              ) : null}
              {Array.isArray((selected as { trace?: unknown[] }).trace) ? (
                <div style={{ display: "grid", gap: 8 }}>
                  <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>Execution Trace</div>
                  {(selected as { trace?: ExecutionTraceStepRecord[] }).trace?.map((entry, index) => (
                    <div key={index} style={{ background: theme.hover, border: "1px solid " + theme.border, borderRadius: 10, padding: 10, display: "grid", gap: 4 }}>
                      <div style={{ display: "flex", justifyContent: "space-between", gap: 8 }}>
                        <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text }}>{index + 1}. {String(entry.step?.name ?? entry.step?.label ?? entry.type ?? entry.step?.type ?? "step")}</div>
                        <div style={{ fontSize: "var(--rm-fs-small)", color: entry.error ? theme.danger : entry.skipped ? theme.warning : theme.success }}>
                          {entry.error ? "FAILED" : entry.skipped ? "SKIPPED" : "OK"}
                        </div>
                      </div>
                      <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>
                        {String(entry.duration_ms ?? entry.result?.status ?? entry.result?.outcome ?? "")}
                      </div>
                      <pre style={{ margin: 0, fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-caption)", color: theme.dim, whiteSpace: "pre-wrap" }}>{JSON.stringify(entry, null, 2)}</pre>
                    </div>
                  ))}
                </div>
              ) : null}
              <pre style={{ margin: 0, padding: 12, fontFamily: "var(--font-mono)", fontSize: "var(--rm-fs-small)", color: theme.muted, whiteSpace: "pre-wrap", background: theme.editor, borderRadius: 10 }}>
                {JSON.stringify(selected, null, 2)}
              </pre>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

type PolicyDiffBucket = { added: string[]; removed: string[]; changed: string[] };
type PolicyDiff = {
  hasBaseline: boolean;
  changed: boolean;
  steps: { added: Array<{ type: string; ref_id: string }>; removed: Array<{ type: string; ref_id: string }> };
  rules: PolicyDiffBucket;
  scorecards: PolicyDiffBucket;
  decisionTables: PolicyDiffBucket;
};

