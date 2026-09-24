"use client";

import * as React from "react";

import { apiJson } from "../lib/api";
import { useRuleMindStore } from "../lib/store";
import { Button, useTheme } from "./kit";
import { Dialog } from "./ui";

/**
 * Promotion review, shared by every page that promotes (Deploy, Rules, Variables, Policies).
 *
 * All four promoted through `window.confirm()` and recorded a fixed reason ("Manual promotion from
 * Rules page", "…from Deploy board", …) -- so the promotion ledger said, on every row, that someone
 * clicked, and never why. Now the reviewer sees what changed and the measured impact on real traffic
 * (policies), and must write the reason that is recorded. One implementation, so the four pages
 * cannot drift apart again.
 */

export type PromotableType = "variable" | "rule" | "scorecard" | "policy";

export type PromotionTarget = {
  entityType: PromotableType;
  entityId: string;
  label: string;
  from: string;
  to: string;
};

export const MIN_PROMOTION_REASON = 10;

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

type Pending = PromotionTarget & { loading: boolean; changeSummary: string; backtest: PolicyBacktest | null };

export function usePromotionReview(options: {
  onPromoted: (target: PromotionTarget) => void;
  onNotify: (message: string) => void;
}) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [pending, setPending] = React.useState<Pending | null>(null);
  const [reason, setReason] = React.useState("");
  const [busyKey, setBusyKey] = React.useState<string | null>(null);
  const { onPromoted, onNotify } = options;

  const review = React.useCallback(
    (entityType: PromotableType, entityId: string, label: string, from: string) => {
      setReason("");
      setPending({ entityType, entityId, label, from, to: from === "dev" ? "uat" : "prod",
                   loading: entityType === "policy", changeSummary: "", backtest: null });
      if (entityType !== "policy") return;
      void (async () => {
        let changeSummary = "";
        let backtest: PolicyBacktest | null = null;
        try {
          const diff = await apiJson<PolicyDiff>(apiBaseUrl, "/api/v1/policies/" + entityId + "/diff", {}, apiKey);
          changeSummary = summarizePolicyDiff(diff);
        } catch {
          /* diff is best-effort context */
        }
        try {
          // Measured impact of the current bundle on this policy's real recent traffic.
          backtest = await apiJson<PolicyBacktest>(apiBaseUrl, "/api/v1/policies/" + entityId + "/backtest?sample=200", { method: "POST" }, apiKey);
        } catch {
          /* backtest is best-effort context */
        }
        setPending((current) => current && current.entityId === entityId
          ? { ...current, loading: false, changeSummary, backtest } : current);
      })();
    },
    [apiBaseUrl, apiKey]
  );

  const confirm = React.useCallback(async () => {
    if (!pending || reason.trim().length < MIN_PROMOTION_REASON) return;
    const key = pending.entityType + ":" + pending.entityId;
    setBusyKey(key);
    try {
      const route = "/api/v1/" + pending.entityType + "s/" + pending.entityId + "/promote";
      const recorded = reason.trim() + (pending.changeSummary ? " — " + pending.changeSummary : "");
      // No promoted_by: the server records the authenticated actor, not a name the client sends.
      await apiJson(apiBaseUrl, route, { method: "POST", body: JSON.stringify({ reason: recorded }) }, apiKey);
      const { loading: _loading, changeSummary: _summary, backtest: _backtest, ...target } = pending;
      setPending(null);
      onPromoted(target);
    } catch (error) {
      onNotify(error instanceof Error ? error.message : "Promotion failed.");
    } finally {
      setBusyKey(null);
    }
  }, [pending, reason, apiBaseUrl, apiKey, onPromoted, onNotify]);

  const flips = pending?.backtest ? pending.backtest.transition_matrix.filter((t) => t.from !== t.to) : [];
  const dialog = (
    <Dialog
      open={pending !== null}
      title={pending ? "Promote " + pending.label + " to " + pending.to.toUpperCase() : ""}
      onClose={() => setPending(null)}
      busy={busyKey !== null}
      footer={
        <>
          <Button variant="ghost" onClick={() => setPending(null)} disabled={busyKey !== null}>Cancel</Button>
          <Button variant="primary" testId="confirm-promotion" onClick={() => void confirm()}
                  disabled={!pending || pending.loading || busyKey !== null || reason.trim().length < MIN_PROMOTION_REASON}>
            {busyKey ? "Promoting…" : "Promote to " + (pending?.to.toUpperCase() ?? "")}
          </Button>
        </>
      }
    >
      {pending ? (
        <>
          <div style={{ color: theme.muted }}>
            {pending.entityType.toUpperCase()} · {pending.entityId} · {pending.from.toUpperCase()} → {pending.to.toUpperCase()}
          </div>
          {pending.loading ? (
            <div role="status">Measuring what this changes…</div>
          ) : pending.entityType === "policy" ? (
            <>
              <div data-testid="promotion-change-summary">{pending.changeSummary || "Could not load the change summary."}</div>
              {pending.backtest ? (
                <div data-testid="promotion-impact">
                  <div style={{ fontWeight: 600 }}>{summarizeBacktest(pending.backtest)}</div>
                  {flips.length ? (
                    <table style={{ marginTop: 6, borderCollapse: "collapse", fontSize: 12.5 }}>
                      <thead><tr><th style={{ textAlign: "left", paddingRight: 16 }}>from</th><th style={{ textAlign: "left", paddingRight: 16 }}>to</th><th style={{ textAlign: "right" }}>decisions</th></tr></thead>
                      <tbody>
                        {flips.map((t) => (
                          <tr key={t.from + t.to}><td style={{ paddingRight: 16 }}>{t.from}</td><td style={{ paddingRight: 16 }}>{t.to}</td><td style={{ textAlign: "right", fontVariantNumeric: "tabular-nums" }}>{t.count}</td></tr>
                        ))}
                      </tbody>
                    </table>
                  ) : null}
                </div>
              ) : (
                <div style={{ color: theme.muted }}>Backtest unavailable — no measured impact to show.</div>
              )}
            </>
          ) : null}
          <label style={{ display: "grid", gap: 6 }}>
            <span style={{ fontWeight: 600 }}>Why is this being promoted? (recorded on the promotion)</span>
            <textarea
              data-testid="promotion-reason"
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
              aria-describedby="promotion-reason-hint"
              style={{ width: "100%", borderRadius: 8, border: "1px solid " + theme.border, padding: 8,
                       background: theme.card, color: theme.text, font: "inherit" }}
            />
            <span id="promotion-reason-hint" style={{ fontSize: 12, color: theme.dim }}>
              At least {MIN_PROMOTION_REASON} characters.{pending.changeSummary ? " The change summary above is appended automatically." : ""}
            </span>
          </label>
        </>
      ) : null}
    </Dialog>
  );

  return { review, dialog, busyKey };
}
