"use client";


import * as React from "react";
import { apiJson } from "../../lib/api";
import { useRuleMindStore } from "../../lib/store";
import { usePromotionReview } from "../PromotionReview";
import type {
BootstrapPayload,DeployStatusPayload,PromotionRecord
} from "../types";



import {
PAGE_META,STATUS_ORDER,useTheme,tone,Button,SectionHeader
} from "../kit";

export function DeployPage(props: { data: BootstrapPayload; refresh: () => void; onNotify: (message: string) => void }) {
  const theme = useTheme();
  const { apiBaseUrl, apiKey, isMobile } = useRuleMindStore();
  const [deployData, setDeployData] = React.useState<DeployStatusPayload | null>(null);

  React.useEffect(() => {
    apiJson<DeployStatusPayload>(apiBaseUrl, "/api/v1/deploy/status", {}, apiKey)
      .then(setDeployData)
      .catch(() => setDeployData(null));
  }, [apiBaseUrl, apiKey, props.data]);

  const promotion = usePromotionReview({
    onPromoted: (done) => {
      props.refresh();
      props.onNotify("Promoted " + done.label + " to " + done.to.toUpperCase() + ".");
    },
    onNotify: props.onNotify,
  });

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
                        <Button small variant="success" disabled={!item.promotable || promotion.busyKey === item.entityType + ":" + item.id} onClick={() => promotion.review(item.entityType, item.id, item.label, statusName)}>
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

      {promotion.dialog}
    </div>
  );
}

