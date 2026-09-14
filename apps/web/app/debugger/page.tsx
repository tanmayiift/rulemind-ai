"use client";

/**
 * Step-through debugger (parity P04).
 *
 * The Decision Explorer answers "what happened" after the fact. This answers "what is about to
 * happen, and what does the policy know right now" -- you stop before a step, read the context AS
 * IT STANDS THERE, and advance one step at a time.
 *
 * Every run here is simulate=true on the server: no decision row, no billed unit, no outbound
 * action. That is why it is safe to step a PROD policy, and the page says so rather than leaving
 * the operator to wonder whether clicking Step just called a partner's API.
 */

import * as React from "react";
import Link from "next/link";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES } from "../../src/v3/theme";

type PolicyStep = { id?: string; type?: string; ref_id?: string; label?: string; name?: string };
type Policy = { id: string; name?: string; status?: string; steps?: PolicyStep[] };

type Snapshot = {
  session_id: string;
  policy_id: string;
  status: string;
  finished: boolean;
  step_count: number;
  paused_before_index: number | null;
  next_step: PolicyStep | null;
  outcome: string | null;
  variables: Record<string, unknown>;
  rule_results: unknown;
  scorecard_results: Record<string, unknown>;
  executed_steps: number;
  step_trace: Array<Record<string, unknown>>;
  variable_errors: Record<string, unknown>;
};

const OUTCOME_TONE: Record<string, "success" | "warning" | "danger"> = {
  approve: "success",
  review: "warning",
  reject: "danger",
};

// Every step type the executor dispatches on gets a glyph. A type with no entry falls back to
// its position number, which reads as "step 8" sitting between an R and an arrow -- the one place
// the column stops being scannable. `connector` and `review_gate` were exactly that.
const STEP_GLYPH: Record<string, string> = {
  connector: "◈",
  rule: "R",
  scorecard: "S",
  outcome: "→",
  branch: "?",
  loop: "↻",
  action: "!",
  model: "M",
  workflow: "W",
  review_gate: "⏸",
};

function stepLabel(step: PolicyStep | null | undefined, index: number): string {
  if (!step) return "Step " + (index + 1);
  return step.label || step.name || step.ref_id || step.id || step.type || "Step " + (index + 1);
}

function show(value: unknown): string {
  if (value === null) return "null";
  if (value === undefined) return "—";
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

export default function DebuggerPage() {
  const { apiBaseUrl, apiKey, themeMode, isMobile } = useRuleMindStore();
  const theme = THEMES[themeMode];

  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [policyId, setPolicyId] = React.useState("");
  const [payloadText, setPayloadText] = React.useState("{}");
  const [snapshot, setSnapshot] = React.useState<Snapshot | null>(null);
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(true);
  // Set when the payload was lifted off a logged decision, so the header can say where it came
  // from -- a prefilled editor with no provenance is a box of numbers you have to trust.
  const [replayOf, setReplayOf] = React.useState<string | null>(null);

  const policy = policies.find((p) => p.id === policyId) || null;
  const steps: PolicyStep[] = policy?.steps || [];

  React.useEffect(() => {
    let active = true;
    (async () => {
      try {
        const rows = await apiJson<Policy[]>(apiBaseUrl, "/api/v1/policies", {}, apiKey);
        if (!active) return;
        setPolicies(rows);
        const params = new URLSearchParams(window.location.search);
        const wanted = params.get("policy");
        const prod = rows.find((p) => p.status === "prod");
        setPolicyId(wanted || prod?.id || rows[0]?.id || "");
        setError(null);

        // ?decision=<id> is the entry point from the Decision Explorer: open a failing
        // execution, then step through it. The stored payload is the decrypted decision input;
        // if data protection redacted fields at write time, this replays what was RECORDED,
        // which is the honest thing to step through.
        const decisionId = params.get("decision");
        if (decisionId) {
          try {
            const decision = await apiJson<{ payload?: Record<string, unknown>; policy_id?: string }>(
              apiBaseUrl, "/api/v1/audit/decisions/" + encodeURIComponent(decisionId), {}, apiKey);
            if (!active) return;
            if (decision.payload) setPayloadText(JSON.stringify(decision.payload, null, 2));
            if (decision.policy_id) setPolicyId(decision.policy_id);
            setReplayOf(decisionId);
          } catch {
            /* a missing decision should not stop the debugger from opening */
          }
        }
      } catch (e) {
        if (active) setError(e instanceof Error ? e.message : "Unable to load policies.");
      } finally {
        if (active) setLoading(false);
      }
    })();
    return () => { active = false; };
  }, [apiBaseUrl, apiKey]);

  const call = React.useCallback(async (path: string, method: string, body?: unknown) => {
    setBusy(true);
    setError(null);
    try {
      const next = await apiJson<Snapshot>(apiBaseUrl, path, {
        method,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }, apiKey);
      setSnapshot(next);
    } catch (e) {
      setError(e instanceof Error ? e.message : "The debugger could not reach the API.");
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey]);

  const start = React.useCallback(async () => {
    let payload: unknown;
    try {
      payload = JSON.parse(payloadText || "{}");
    } catch {
      // Parsed here rather than server-side so the message names the real problem. An invalid
      // JSON body would otherwise come back as a 422 about a missing field.
      setError("That payload is not valid JSON.");
      return;
    }
    setSnapshot(null);
    await call("/api/v1/debug/sessions", "POST", { policy_id: policyId, payload });
  }, [call, payloadText, policyId]);

  const step = React.useCallback(() => {
    if (!snapshot) return;
    void call("/api/v1/debug/sessions/" + snapshot.session_id + "/step", "POST");
  }, [call, snapshot]);

  const runToEnd = React.useCallback(() => {
    if (!snapshot) return;
    void call("/api/v1/debug/sessions/" + snapshot.session_id + "/run", "POST");
  }, [call, snapshot]);

  const reset = React.useCallback(() => {
    setSnapshot(null);
    setError(null);
  }, []);

  const card: React.CSSProperties = {
    background: theme.card, border: "1px solid " + theme.border, borderRadius: 12,
  };
  const button = (kind: "primary" | "ghost"): React.CSSProperties => ({
    padding: "8px 14px", borderRadius: 8, fontSize: 13, fontWeight: 600, cursor: "pointer",
    border: "1px solid " + (kind === "primary" ? theme.accent : theme.border),
    background: kind === "primary" ? theme.accent : "transparent",
    color: kind === "primary" ? "#fff" : theme.text,
  });

  const pausedAt = snapshot?.paused_before_index ?? null;
  const outcomeTone = snapshot?.outcome ? OUTCOME_TONE[snapshot.outcome] : undefined;
  const outcomeColor = outcomeTone ? theme[outcomeTone] : theme.muted;
  const variableErrors = Object.entries(snapshot?.variable_errors || {});

  return (
    <div style={{ padding: isMobile ? 16 : 24, color: theme.text }}>
      <h1 style={{ fontSize: 22, letterSpacing: "-0.02em", margin: 0 }}>Step-through Debugger</h1>
      <p style={{ color: theme.muted, margin: "4px 0 6px", fontSize: 13.5 }}>
        Stop before a step, read what the policy knows at that moment, then advance one step.
      </p>
      {/* The single most important thing an operator needs to know before clicking Step on a
          prod policy, so it is stated on the page and not only in the docs. */}
      <p style={{ color: theme.muted, margin: "0 0 20px", fontSize: 12.5 }}>
        Runs in simulation — no decision is logged, nothing is billed, and no action step calls out.
      </p>

      {error ? (
        <div style={{ ...card, padding: 14, color: theme.danger, marginBottom: 16 }} data-testid="debugger-error">
          {error}
        </div>
      ) : null}

      {!loading && policies.length === 0 ? (
        <div style={{ ...card, padding: 48, textAlign: "center", color: theme.muted }}>
          <div style={{ marginBottom: 14 }}>There is no policy to step through yet.</div>
          <Link href="/policies" style={{ ...button("primary"), textDecoration: "none" }}>
            Build a policy
          </Link>
        </div>
      ) : null}

      {policies.length > 0 ? (
        <div style={{ display: "grid", gap: 16, gridTemplateColumns: isMobile ? "1fr" : "320px 1fr 320px", alignItems: "start" }}>

          {/* ------------------------------------------------------------------ input */}
          <div style={{ ...card, padding: 16 }}>
            <div style={{ fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", color: theme.muted, marginBottom: 10 }}>
              Input
            </div>
            <label style={{ display: "block", fontSize: 12, color: theme.muted, marginBottom: 4 }}>Policy</label>
            <select
              value={policyId}
              onChange={(e) => { setPolicyId(e.target.value); setSnapshot(null); }}
              disabled={Boolean(snapshot)}
              data-testid="debugger-policy"
              style={{ width: "100%", padding: "8px 10px", borderRadius: 8, marginBottom: 12, fontSize: 13, background: theme.cardAlt, color: theme.text, border: "1px solid " + theme.border }}
            >
              {policies.map((p) => (
                <option key={p.id} value={p.id}>{(p.name || p.id) + (p.status ? " · " + p.status : "")}</option>
              ))}
            </select>

            <label style={{ display: "block", fontSize: 12, color: theme.muted, marginBottom: 4 }}>
              Payload {replayOf ? <span style={{ color: theme.accent }}>· replaying {replayOf.slice(0, 8)}</span> : null}
            </label>
            <textarea
              value={payloadText}
              onChange={(e) => setPayloadText(e.target.value)}
              disabled={Boolean(snapshot)}
              spellCheck={false}
              rows={12}
              data-testid="debugger-payload"
              style={{ width: "100%", boxSizing: "border-box", padding: 10, borderRadius: 8, fontSize: 12.5, fontFamily: "var(--font-mono)", background: theme.cardAlt, color: theme.text, border: "1px solid " + theme.border, resize: "vertical" }}
            />

            <div style={{ display: "flex", gap: 8, marginTop: 12, flexWrap: "wrap" }}>
              {!snapshot ? (
                <button onClick={() => void start()} disabled={busy || !policyId} style={button("primary")} data-testid="debugger-start">
                  {busy ? "Starting…" : "Start debugging"}
                </button>
              ) : (
                <>
                  <button onClick={step} disabled={busy || snapshot.finished} style={button("primary")} data-testid="debugger-step">
                    Step over
                  </button>
                  <button onClick={runToEnd} disabled={busy || snapshot.finished} style={button("ghost")} data-testid="debugger-run">
                    Run to end
                  </button>
                  <button onClick={reset} disabled={busy} style={button("ghost")} data-testid="debugger-reset">
                    Restart
                  </button>
                </>
              )}
            </div>
          </div>

          {/* ------------------------------------------------------------------ the steps */}
          <div style={{ ...card, padding: 16 }} data-testid="debugger-steps">
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", marginBottom: 12 }}>
              <div style={{ fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", color: theme.muted }}>
                Execution
              </div>
              {snapshot ? (
                <div style={{ fontSize: 12, color: theme.muted }}>
                  {snapshot.finished
                    ? "finished · " + snapshot.executed_steps + " of " + snapshot.step_count + " steps ran"
                    : "paused before step " + ((pausedAt ?? 0) + 1) + " of " + snapshot.step_count}
                </div>
              ) : null}
            </div>

            {steps.length === 0 ? (
              <div style={{ color: theme.muted, fontSize: 13, padding: "24px 0", textAlign: "center" }}>
                This policy has no steps.
              </div>
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {steps.map((s, index) => {
                  const executed = snapshot ? index < snapshot.executed_steps : false;
                  const current = snapshot ? !snapshot.finished && index === pausedAt : false;
                  const tone = current ? theme.accent : executed ? theme.success : theme.border;
                  return (
                    <div
                      key={s.id || index}
                      data-testid={"debugger-step-" + index}
                      data-state={current ? "current" : executed ? "executed" : "pending"}
                      style={{
                        display: "flex", alignItems: "center", gap: 10, padding: "10px 12px",
                        borderRadius: 10,
                        // The stop is marked with a real left rail rather than a colour wash:
                        // "where am I" has to survive a glance at a 12-step policy.
                        borderLeft: "3px solid " + tone,
                        border: "1px solid " + (current ? theme.accent : theme.border),
                        borderLeftWidth: 3, borderLeftColor: tone,
                        background: current ? theme.accentBg : executed ? theme.card : "transparent",
                        opacity: !snapshot || current || executed ? 1 : 0.55,
                      }}
                    >
                      <span style={{ width: 22, height: 22, borderRadius: 6, display: "grid", placeItems: "center", fontSize: 11, fontWeight: 800, color: current ? "#fff" : theme.muted, background: current ? theme.accent : theme.cardAlt }}>
                        {STEP_GLYPH[s.type || ""] || String(index + 1)}
                      </span>
                      <span style={{ flex: 1, fontSize: 13.5, fontWeight: current ? 700 : 500 }}>
                        {stepLabel(s, index)}
                      </span>
                      <span style={{ fontSize: 11.5, color: theme.muted, fontFamily: "var(--font-mono)" }}>{s.type}</span>
                      {current ? (
                        <span style={{ fontSize: 11, fontWeight: 700, color: theme.accent, textTransform: "uppercase" }}>next</span>
                      ) : executed ? (
                        <span style={{ fontSize: 11, color: theme.success }}>✓</span>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            )}

            {snapshot ? (
              <div style={{ marginTop: 16, paddingTop: 14, borderTop: "1px solid " + theme.border, display: "flex", alignItems: "center", gap: 10 }}>
                <span style={{ fontSize: 12, color: theme.muted }}>Outcome so far</span>
                <span data-testid="debugger-outcome" style={{ padding: "3px 10px", borderRadius: 999, fontSize: 12, fontWeight: 700, textTransform: "uppercase", color: outcomeColor, background: outcomeColor + "1e" }}>
                  {snapshot.outcome || "pending"}
                </span>
              </div>
            ) : null}
          </div>

          {/* ------------------------------------------------------------------ state here */}
          <div style={{ ...card, padding: 16 }} data-testid="debugger-state">
            <div style={{ fontSize: 12, fontWeight: 700, textTransform: "uppercase", letterSpacing: "0.06em", color: theme.muted, marginBottom: 4 }}>
              State at this stop
            </div>
            <div style={{ fontSize: 12, color: theme.muted, marginBottom: 12 }}>
              {snapshot
                ? snapshot.finished ? "Final." : "What step " + ((pausedAt ?? 0) + 1) + " is about to see."
                : "Start a session to inspect it."}
            </div>

            {snapshot ? (
              <>
                {variableErrors.length > 0 ? (
                  <div style={{ marginBottom: 14, padding: 10, borderRadius: 8, background: theme.dangerBg, border: "1px solid " + theme.danger }}>
                    <div style={{ fontSize: 12, fontWeight: 700, color: theme.danger, marginBottom: 6 }}>
                      {variableErrors.length} variable failed
                    </div>
                    {variableErrors.map(([name, message]) => (
                      <div key={name} style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: theme.text, marginBottom: 3 }}>
                        {name}: {show(message)}
                      </div>
                    ))}
                  </div>
                ) : null}

                <div style={{ fontSize: 12, color: theme.muted, marginBottom: 6 }}>Variables</div>
                {Object.keys(snapshot.variables).length === 0 ? (
                  <div style={{ fontSize: 12.5, color: theme.muted, fontStyle: "italic" }}>None computed yet.</div>
                ) : (
                  <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                    {Object.entries(snapshot.variables).map(([name, value]) => (
                      <div key={name} style={{ display: "flex", justifyContent: "space-between", gap: 10, fontSize: 12.5, fontFamily: "var(--font-mono)", padding: "5px 8px", borderRadius: 6, background: theme.cardAlt }}>
                        <span style={{ color: theme.muted, overflow: "hidden", textOverflow: "ellipsis" }}>{name}</span>
                        <span style={{ fontWeight: 700 }}>{show(value)}</span>
                      </div>
                    ))}
                  </div>
                )}

                {snapshot.step_trace.length > 0 ? (
                  <>
                    <div style={{ fontSize: 12, color: theme.muted, margin: "14px 0 6px" }}>What already ran</div>
                    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
                      {snapshot.step_trace.map((raw, index) => {
                        // A trace entry nests its step: {step: {...}, result: {...}, error, duration_ms}.
                        // Reading ref_id off the entry itself found nothing and fell through to
                        // dumping the whole record as JSON, which is the opposite of a summary.
                        const entry = raw as { step?: PolicyStep; result?: Record<string, unknown>; error?: string | null; duration_ms?: number };
                        const outcome = entry.result?.outcome ?? entry.result?.status;
                        const failed = Boolean(entry.error);
                        return (
                          <div key={index} style={{ display: "flex", alignItems: "baseline", gap: 8, fontSize: 12, padding: "5px 8px", borderRadius: 6, background: theme.cardAlt }}>
                            <span style={{ flex: 1, minWidth: 0, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                              {stepLabel(entry.step, index)}
                            </span>
                            <span style={{ fontFamily: "var(--font-mono)", fontSize: 11.5, color: failed ? theme.danger : theme.muted }}>
                              {failed ? entry.error : show(outcome)}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </>
                ) : null}
              </>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
