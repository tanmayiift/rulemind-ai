"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES } from "../../src/v3/theme";

type ReviewTask = {
  id: string;
  policy_id?: string;
  queue: string;
  status: string;
  context_snapshot?: Record<string, unknown>;
  timeout_at?: string | null;
  assigned_at?: string;
};

export default function ReviewQueuePage() {
  const { apiBaseUrl, apiKey, themeMode } = useRuleMindStore();
  const theme = THEMES[themeMode];
  const [tasks, setTasks] = React.useState<ReviewTask[]>([]);
  const [selected, setSelected] = React.useState<ReviewTask | null>(null);
  const [notes, setNotes] = React.useState("");
  const [amount, setAmount] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const load = React.useCallback(async () => {
    try {
      const response = await apiJson<ReviewTask[]>(apiBaseUrl, "/api/v1/reviews?status=pending", {}, apiKey);
      setTasks(response);
      if (!selected && response[0]) {
        setSelected(response[0]);
      }
      setError(null);
    } catch (fetchError) {
      setError(fetchError instanceof Error ? fetchError.message : "Unable to load review queue.");
    }
  }, [apiBaseUrl, apiKey, selected]);

  React.useEffect(() => {
    void load();
  }, [load]);

  const decide = React.useCallback(
    async (decision: "approve" | "reject") => {
      if (!selected) {
        return;
      }
      try {
        await apiJson(apiBaseUrl, "/api/v1/reviews/" + selected.id + "/decide", {
          method: "POST",
          body: JSON.stringify({
            decision,
            reviewer_id: "web-reviewer",
            response: {
              reviewer_notes: notes,
              approved_amount: amount ? Number(amount) : undefined,
            },
          }),
        }, apiKey);
        setNotes("");
        setAmount("");
        await load();
      } catch (submitError) {
        setError(submitError instanceof Error ? submitError.message : "Unable to submit review.");
      }
    },
    [amount, apiBaseUrl, apiKey, load, notes, selected]
  );

  return (
    <div style={{ padding: 20, display: "grid", gap: 16 }}>
      <div style={{ display: "grid", gap: 4 }}>
        <h1 style={{ margin: 0, fontSize: 24, fontWeight: 800 }}>Review Queue</h1>
        <div style={{ fontSize: 12, color: theme.muted }}>Pending human-in-the-loop tasks with decision context and inline approval controls.</div>
      </div>
      {error ? <div style={{ padding: 12, borderRadius: 12, background: theme.dangerBg, color: theme.danger }}>{error}</div> : null}
      <div style={{ display: "grid", gridTemplateColumns: "360px 1fr", gap: 16 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 14, overflow: "hidden" }}>
          <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, fontSize: 13, fontWeight: 800 }}>Pending Tasks</div>
          {tasks.length === 0 ? <div style={{ padding: 16, color: theme.dim, fontSize: 12 }}>No pending tasks.</div> : null}
          {tasks.map((task) => (
            <button
              key={task.id}
              type="button"
              onClick={() => setSelected(task)}
              style={{
                width: "100%",
                border: "none",
                borderTop: "1px solid " + theme.border,
                background: selected?.id === task.id ? theme.accentBg : "transparent",
                color: theme.text,
                textAlign: "left",
                padding: 14,
                display: "grid",
                gap: 4,
                cursor: "pointer",
              }}
            >
              <div style={{ fontSize: 12, fontWeight: 800 }}>{task.id}</div>
              <div style={{ fontSize: 11, color: theme.muted }}>{task.policy_id} · {task.queue}</div>
              <div style={{ fontSize: 11, color: theme.dim }}>Timeout: {task.timeout_at ?? "n/a"}</div>
            </button>
          ))}
        </div>

        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 14, padding: 16, display: "grid", gap: 14 }}>
          {!selected ? (
            <div style={{ color: theme.dim, fontSize: 12 }}>Select a review task to inspect variables, rules, and scorecards.</div>
          ) : (
            <>
              <div style={{ display: "grid", gap: 4 }}>
                <div style={{ fontSize: 20, fontWeight: 800 }}>Review Task {selected.id}</div>
                <div style={{ fontSize: 12, color: theme.muted }}>{selected.policy_id} · Queue {selected.queue}</div>
              </div>
              <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 12 }}>
                <Panel title="Variables" data={(selected.context_snapshot?.variables as Record<string, unknown>) ?? {}} theme={theme} />
                <Panel title="Rule Results" data={(selected.context_snapshot?.rule_results as Record<string, unknown>) ?? {}} theme={theme} />
                <Panel title="Scorecard" data={(selected.context_snapshot?.scorecard_results as Record<string, unknown>) ?? {}} theme={theme} />
              </div>
              <div style={{ display: "grid", gap: 8 }}>
                <input value={notes} onChange={(event) => setNotes(event.target.value)} placeholder="Reviewer notes" style={inputStyle(theme)} />
                <input value={amount} onChange={(event) => setAmount(event.target.value)} placeholder="Approved amount" style={inputStyle(theme)} />
              </div>
              <div style={{ display: "flex", gap: 10 }}>
                <button type="button" onClick={() => void decide("reject")} style={buttonStyle(theme, theme.dangerBg, theme.danger)}>Reject</button>
                <button type="button" onClick={() => void decide("approve")} style={buttonStyle(theme, theme.successBg, theme.success)}>Approve</button>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function Panel(props: { title: string; data: Record<string, unknown>; theme: typeof THEMES.light }) {
  return (
    <div style={{ background: props.theme.hover, border: "1px solid " + props.theme.border, borderRadius: 12, overflow: "hidden" }}>
      <div style={{ padding: 12, borderBottom: "1px solid " + props.theme.border, fontSize: 12, fontWeight: 800 }}>{props.title}</div>
      <pre style={{ margin: 0, padding: 12, fontFamily: "var(--font-mono)", fontSize: 11, color: props.theme.muted, whiteSpace: "pre-wrap" }}>
        {JSON.stringify(props.data, null, 2)}
      </pre>
    </div>
  );
}

function inputStyle(theme: typeof THEMES.light): React.CSSProperties {
  return {
    width: "100%",
    borderRadius: 10,
    border: "1px solid " + theme.border,
    background: theme.input,
    color: theme.text,
    padding: "10px 12px",
    fontSize: 12,
  };
}

function buttonStyle(theme: typeof THEMES.light, background: string, color: string): React.CSSProperties {
  return {
    border: "1px solid " + theme.border,
    background,
    color,
    borderRadius: 10,
    padding: "10px 14px",
    fontSize: 12,
    fontWeight: 700,
    cursor: "pointer",
  };
}
