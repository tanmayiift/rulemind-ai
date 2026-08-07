"use client";

/**
 * AI Copilot — a floating action button (bottom-right) that invokes every BYO-key
 * AI action from anywhere in the app. Rendered by AppShell only when AI is enabled
 * (a provider key is configured and the admin hasn't switched AI off), so the
 * button never appears when it couldn't work.
 *
 * Each action posts to an existing /api/v1/ai/* endpoint; while the model works we
 * show an engaging animated "thinking" state (an orbiting spark + a cycling status
 * line) so the user knows something is happening. Results render inline; a 402
 * (over budget) or 422 (no key) comes back as the endpoint's message in a banner.
 */
import * as React from "react";
import { Sparkles, X, Wand2, Gauge, FlaskConical, TrendingDown, FileText, ListTree, type LucideIcon } from "lucide-react";
import { apiJson } from "../lib/api";
import type { ThemeTokens } from "../v3/theme";

type InputKind = "textarea" | "experiment" | "policy" | "decision";

interface AiAction {
  id: string;
  label: string;
  blurb: string;
  icon: LucideIcon;
  endpoint: string;
  input: InputKind;
  placeholder?: string;
  // Rotating status lines shown while this action runs — keeps the wait engaging.
  thinking: string[];
  buildBody: (value: string) => Record<string, unknown>;
}

const ACTIONS: AiAction[] = [
  {
    id: "generate-rule", label: "Generate a rule", icon: Wand2,
    blurb: "Describe a rule in plain English → a validated draft.",
    endpoint: "/api/v1/ai/generate-rule", input: "textarea",
    placeholder: "e.g. Approve when the bureau score is at least 700 and DTI is under 40%.",
    thinking: ["Reading your intent…", "Mapping it to your variables…", "Drafting the rule tree…", "Checking it's exhaustive…"],
    buildBody: (v) => ({ prompt: v }),
  },
  {
    id: "generate-policy", label: "Generate a policy", icon: ListTree,
    blurb: "Describe a flow → a draft policy over your rules & connectors.",
    endpoint: "/api/v1/ai/generate-policy", input: "textarea",
    placeholder: "e.g. Pull the bureau, run the KYC gate, then the risk scorecard, then approve.",
    thinking: ["Reading your flow…", "Wiring the steps…", "Selecting rules & connectors…", "Validating the sequence…"],
    buildBody: (v) => ({ prompt: v }),
  },
  {
    id: "generate-predictor", label: "Create a predictor", icon: Gauge,
    blurb: "Define a predictor → a draft scorecard over your variables.",
    endpoint: "/api/v1/ai/generate-predictor", input: "textarea",
    placeholder: "e.g. A risk scorecard from bureau utilisation, delinquencies and account age.",
    thinking: ["Reading the definition…", "Choosing variables…", "Binning the ranges…", "Assigning points…"],
    buildBody: (v) => ({ definition: v }),
  },
  {
    id: "analyze-experiment", label: "Analyze an experiment", icon: FlaskConical,
    blurb: "Read a champion/challenger's live results → a promote/hold/rollback call.",
    endpoint: "/api/v1/ai/analyze-experiment", input: "experiment",
    thinking: ["Pulling per-variant results…", "Comparing approval & risk…", "Weighing the trade-offs…", "Forming a recommendation…"],
    buildBody: (v) => ({ experiment_id: v }),
  },
  {
    id: "analyze-rejections", label: "Explain rejections", icon: TrendingDown,
    blurb: "Find why declines changed for a policy and what to do next.",
    endpoint: "/api/v1/ai/analyze-rejections", input: "policy",
    thinking: ["Scanning the decision log…", "Ranking the decline drivers…", "Spotting what moved…", "Writing recommendations…"],
    buildBody: (v) => (v ? { policy_id: v } : {}),
  },
  {
    id: "explain-decision", label: "Explain a decision", icon: FileText,
    blurb: "Plain-English reasons + adverse-action codes for one decision.",
    endpoint: "/api/v1/ai/explain-decision", input: "decision",
    thinking: ["Loading the decision…", "Reading the evidence…", "Explaining the outcome…", "Listing reason codes…"],
    buildBody: (v) => ({ decision_id: v }),
  },
];

interface Option { id: string; label: string }

export function AiCopilotFab({ theme, apiBaseUrl, apiKey }: { theme: ThemeTokens; apiBaseUrl: string; apiKey: string }) {
  const [open, setOpen] = React.useState(false);
  const [active, setActive] = React.useState<AiAction | null>(null);
  const [value, setValue] = React.useState("");
  const [options, setOptions] = React.useState<Option[]>([]);
  const [busy, setBusy] = React.useState(false);
  const [thinkIdx, setThinkIdx] = React.useState(0);
  const [result, setResult] = React.useState<Record<string, unknown> | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  // Cycle the "thinking" status line while a request is in flight.
  React.useEffect(() => {
    if (!busy || !active) return;
    const lines = active.thinking;
    const t = setInterval(() => setThinkIdx((i) => (i + 1) % lines.length), 1400);
    return () => clearInterval(t);
  }, [busy, active]);

  // Lazily load the option list an action needs (experiments / policies).
  const loadOptions = React.useCallback(async (action: AiAction) => {
    setOptions([]);
    try {
      if (action.input === "experiment") {
        const rows = await apiJson<Array<{ id: string; name?: string; status?: string }>>(apiBaseUrl, "/api/v1/experiments", {}, apiKey);
        setOptions(rows.map((r) => ({ id: r.id, label: `${r.name || r.id}${r.status ? ` · ${r.status}` : ""}` })));
      } else if (action.input === "policy") {
        const rows = await apiJson<Array<{ id: string; name?: string }>>(apiBaseUrl, "/api/v1/policies", {}, apiKey);
        setOptions([{ id: "", label: "All policies" }, ...rows.map((r) => ({ id: r.id, label: r.name || r.id }))]);
        setValue("");
      } else if (action.input === "decision") {
        const rows = await apiJson<Array<{ id: string; outcome?: string; policy_id?: string }>>(apiBaseUrl, "/api/v1/audit/decisions?limit=25", {}, apiKey);
        setOptions(rows.map((r) => ({ id: r.id, label: `${(r.outcome || "?").toUpperCase()} · ${r.policy_id || ""} · ${r.id.slice(0, 8)}` })));
      }
    } catch {
      /* leave options empty — a free-text fallback is offered for decision/experiment */
    }
  }, [apiBaseUrl, apiKey]);

  function pick(action: AiAction) {
    setActive(action);
    setResult(null);
    setError(null);
    setValue("");
    if (action.input !== "textarea") void loadOptions(action);
  }

  async function run() {
    if (!active) return;
    if ((active.input === "textarea" || active.input === "decision" || active.input === "experiment") && !value.trim()) {
      setError("Enter something to run this action.");
      return;
    }
    setBusy(true);
    setThinkIdx(0);
    setError(null);
    setResult(null);
    try {
      const data = await apiJson<Record<string, unknown>>(
        apiBaseUrl, active.endpoint,
        { method: "POST", body: JSON.stringify(active.buildBody(value.trim())) },
        apiKey,
      );
      setResult(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "The AI request failed.");
    } finally {
      setBusy(false);
    }
  }

  function reset() {
    setActive(null);
    setResult(null);
    setError(null);
    setValue("");
  }

  const accent = theme.accent;
  return (
    <>
      <style>{KEYFRAMES}</style>

      {/* Launcher */}
      <button
        type="button"
        data-testid="ai-copilot-fab"
        aria-label="Open AI Copilot"
        onClick={() => setOpen((o) => !o)}
        style={{
          position: "fixed", right: 24, bottom: 24, zIndex: 1200,
          width: 60, height: 60, borderRadius: "50%", border: "none", cursor: "pointer",
          background: `linear-gradient(135deg, ${accent}, ${theme.purple})`,
          color: "#fff", display: "grid", placeItems: "center",
          boxShadow: `0 10px 30px ${hexA(accent, 0.45)}`,
          animation: busy ? "rm-ai-work 1.1s linear infinite" : "rm-ai-pulse 2.8s ease-in-out infinite",
        }}
      >
        <span style={{ display: "grid", placeItems: "center", animation: busy ? "rm-ai-bob 1s ease-in-out infinite" : "none" }}>
          {open && !busy ? <X size={24} /> : <Sparkles size={26} />}
        </span>
      </button>

      {/* Panel */}
      {open && (
        <div
          data-testid="ai-copilot-panel"
          style={{
            position: "fixed", right: 24, bottom: 96, zIndex: 1200,
            width: "min(420px, calc(100vw - 32px))", maxHeight: "min(72vh, 640px)",
            display: "flex", flexDirection: "column",
            background: theme.card, color: theme.text,
            border: `1px solid ${theme.border}`, borderRadius: 16,
            boxShadow: "0 24px 60px rgba(0,0,0,0.28)", overflow: "hidden",
            animation: "rm-ai-rise 0.18s ease-out",
          }}
        >
          {/* Header */}
          <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: `1px solid ${theme.border}`, background: hexA(accent, 0.06) }}>
            <span style={{ display: "grid", placeItems: "center", width: 30, height: 30, borderRadius: 9, background: `linear-gradient(135deg, ${accent}, ${theme.purple})`, color: "#fff" }}>
              <Sparkles size={18} />
            </span>
            <div style={{ flex: 1, minWidth: 0 }}>
              <div style={{ fontWeight: 600, fontSize: "var(--rm-fs-body)" }}>AI Copilot</div>
              <div style={{ fontSize: "var(--rm-fs-caption)", color: theme.muted }}>
                {active ? active.label : "What would you like help with?"}
              </div>
            </div>
            {active && !busy && (
              <button type="button" onClick={reset} style={ghostBtn(theme)}>Back</button>
            )}
            <button type="button" aria-label="Close" onClick={() => setOpen(false)} style={{ ...ghostBtn(theme), padding: 6 }}>
              <X size={16} />
            </button>
          </div>

          <div style={{ overflow: "auto", padding: 14 }}>
            {/* Action picker */}
            {!active && (
              <div style={{ display: "grid", gap: 8 }}>
                {ACTIONS.map((a) => (
                  <button
                    key={a.id}
                    type="button"
                    data-testid={`ai-action-${a.id}`}
                    onClick={() => pick(a)}
                    style={{
                      display: "flex", alignItems: "flex-start", gap: 12, textAlign: "left",
                      padding: "11px 12px", borderRadius: 12, cursor: "pointer",
                      background: theme.cardAlt, border: `1px solid ${theme.border}`, color: theme.text,
                    }}
                  >
                    <span style={{ display: "grid", placeItems: "center", width: 30, height: 30, borderRadius: 8, background: hexA(accent, 0.12), color: accent, flexShrink: 0 }}>
                      <a.icon size={17} />
                    </span>
                    <span style={{ minWidth: 0 }}>
                      <span style={{ display: "block", fontWeight: 600, fontSize: "var(--rm-fs-body)" }}>{a.label}</span>
                      <span style={{ display: "block", fontSize: "var(--rm-fs-caption)", color: theme.muted }}>{a.blurb}</span>
                    </span>
                  </button>
                ))}
              </div>
            )}

            {/* Action form + result */}
            {active && (
              <div style={{ display: "grid", gap: 12 }}>
                {!busy && !result && (
                  <>
                    {active.input === "textarea" && (
                      <textarea
                        data-testid="ai-input"
                        value={value}
                        onChange={(e) => setValue(e.target.value)}
                        placeholder={active.placeholder}
                        rows={4}
                        style={fieldStyle(theme)}
                      />
                    )}
                    {(active.input === "experiment" || active.input === "policy" || active.input === "decision") && options.length > 0 && (
                      <select data-testid="ai-input" value={value} onChange={(e) => setValue(e.target.value)} style={fieldStyle(theme)}>
                        {active.input !== "policy" && <option value="">Select…</option>}
                        {options.map((o) => <option key={o.id || "all"} value={o.id}>{o.label}</option>)}
                      </select>
                    )}
                    {(active.input === "experiment" || active.input === "decision") && options.length === 0 && (
                      <input
                        data-testid="ai-input"
                        value={value}
                        onChange={(e) => setValue(e.target.value)}
                        placeholder={active.input === "decision" ? "Decision id" : "Experiment id"}
                        style={fieldStyle(theme)}
                      />
                    )}
                    <button type="button" data-testid="ai-run" onClick={run} style={primaryBtn(theme)}>
                      <Sparkles size={16} /> Run
                    </button>
                  </>
                )}

                {busy && (
                  <div style={{ display: "grid", placeItems: "center", gap: 14, padding: "26px 0" }} data-testid="ai-thinking">
                    <div style={{ position: "relative", width: 56, height: 56 }}>
                      <div style={{ position: "absolute", inset: 0, borderRadius: "50%", border: `3px solid ${hexA(accent, 0.18)}`, borderTopColor: accent, animation: "rm-ai-spin 0.9s linear infinite" }} />
                      <span style={{ position: "absolute", inset: 0, display: "grid", placeItems: "center", color: accent, animation: "rm-ai-bob 1s ease-in-out infinite" }}>
                        <Sparkles size={22} />
                      </span>
                    </div>
                    <div style={{ fontSize: "var(--rm-fs-body)", color: theme.muted, minHeight: 20, animation: "rm-ai-fade 1.4s ease-in-out infinite" }}>
                      {active.thinking[thinkIdx]}
                    </div>
                  </div>
                )}

                {error && (
                  <div style={{ padding: "10px 12px", borderRadius: 10, background: theme.dangerBg, color: theme.danger, fontSize: "var(--rm-fs-caption)", whiteSpace: "pre-wrap" }} data-testid="ai-error">
                    {friendlyError(error)}
                  </div>
                )}

                {result && !busy && <ResultView theme={theme} action={active} data={result} />}

                {result && !busy && (
                  <button type="button" onClick={() => { setResult(null); setError(null); }} style={ghostBtn(theme)}>Run again</button>
                )}
              </div>
            )}
          </div>
        </div>
      )}
    </>
  );
}

function ResultView({ theme, action, data }: { theme: ThemeTokens; action: AiAction; data: Record<string, unknown> }) {
  // Out-of-scope guardrail (generate-* endpoints return in_scope:false with a message).
  if (data.in_scope === false) {
    return <div style={{ padding: "10px 12px", borderRadius: 10, background: theme.warningBg, color: theme.warning, fontSize: "var(--rm-fs-caption)" }}>{String(data.message || "That request is outside your decisioning workspace.")}</div>;
  }

  const chips: React.ReactNode[] = [];
  if (typeof data.recommendation === "string") chips.push(<Chip key="rec" theme={theme} tone={recTone(data.recommendation)}>{String(data.recommendation).toUpperCase()}</Chip>);
  if (typeof data.winning_variant === "string" && data.winning_variant) chips.push(<Chip key="win" theme={theme}>Winner: {String(data.winning_variant)}</Chip>);
  if (typeof data.valid === "boolean") chips.push(<Chip key="valid" theme={theme} tone={data.valid ? "success" : "danger"}>{data.valid ? "Valid draft" : "Needs fixes"}</Chip>);
  if (typeof data.outcome === "string") chips.push(<Chip key="out" theme={theme}>{String(data.outcome).toUpperCase()}</Chip>);

  const draft = (data.draft ?? null) as Record<string, unknown> | null;
  const topReasons = (data.top_reasons as Array<{ driver?: string; impact?: string }> | undefined) ?? undefined;
  const reasonCodes = (data.reason_codes as string[] | undefined) ?? undefined;
  const recs = (data.recommendations as string[] | undefined) ?? (data.cautions as string[] | undefined);

  return (
    <div style={{ display: "grid", gap: 10 }} data-testid="ai-result">
      {chips.length > 0 && <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>{chips}</div>}

      {typeof data.summary === "string" && data.summary && (
        <p style={{ margin: 0, fontSize: "var(--rm-fs-body)", lineHeight: 1.5 }}>{data.summary}</p>
      )}
      {typeof data.rationale === "string" && data.rationale && (
        <p style={{ margin: 0, fontSize: "var(--rm-fs-caption)", color: theme.muted, lineHeight: 1.5 }}>{data.rationale}</p>
      )}
      {typeof data.validation_error === "string" && data.validation_error && (
        <div style={{ padding: "8px 10px", borderRadius: 8, background: theme.dangerBg, color: theme.danger, fontSize: "var(--rm-fs-caption)" }}>{data.validation_error}</div>
      )}

      {draft && (
        <div style={{ border: `1px solid ${theme.border}`, borderRadius: 10, overflow: "hidden" }}>
          <div style={{ padding: "8px 10px", background: theme.cardAlt, fontWeight: 600, fontSize: "var(--rm-fs-caption)" }}>
            Draft: {String(draft.name || action.label)}
          </div>
          <pre style={{ margin: 0, padding: 10, maxHeight: 220, overflow: "auto", fontSize: 12, lineHeight: 1.45, background: theme.editor, color: theme.text }}>
            {JSON.stringify(draft, null, 2)}
          </pre>
        </div>
      )}

      {topReasons && topReasons.length > 0 && (
        <ul style={listStyle}>
          {topReasons.map((r, i) => (
            <li key={i} style={{ marginBottom: 6 }}>
              <strong>{r.driver}</strong>
              {r.impact ? <span style={{ color: theme.muted }}> — {r.impact}</span> : null}
            </li>
          ))}
        </ul>
      )}

      {reasonCodes && reasonCodes.length > 0 && (
        <ul style={listStyle}>{reasonCodes.map((c, i) => <li key={i}>{c}</li>)}</ul>
      )}

      {recs && recs.length > 0 && (
        <div>
          <div style={{ fontSize: "var(--rm-fs-caption)", fontWeight: 600, color: theme.muted, marginBottom: 4 }}>
            {data.recommendations ? "Recommendations" : "Cautions"}
          </div>
          <ul style={listStyle}>{recs.map((c, i) => <li key={i}>{c}</li>)}</ul>
        </div>
      )}
    </div>
  );
}

function Chip({ theme, tone, children }: { theme: ThemeTokens; tone?: "success" | "danger" | "warning"; children: React.ReactNode }) {
  const map = { success: [theme.successBg, theme.success], danger: [theme.dangerBg, theme.danger], warning: [theme.warningBg, theme.warning] } as const;
  const [bg, fg] = tone ? map[tone] : [theme.accentBg, theme.accent];
  return <span style={{ padding: "3px 9px", borderRadius: 999, background: bg, color: fg, fontSize: "var(--rm-fs-caption)", fontWeight: 600 }}>{children}</span>;
}

const listStyle: React.CSSProperties = { margin: 0, paddingLeft: 18, fontSize: "var(--rm-fs-caption)", lineHeight: 1.5 };

function fieldStyle(theme: ThemeTokens): React.CSSProperties {
  return { width: "100%", padding: "9px 11px", borderRadius: 10, border: `1px solid ${theme.border}`, background: theme.input, color: theme.text, fontSize: "var(--rm-fs-body)", fontFamily: "inherit", resize: "vertical" };
}
function primaryBtn(theme: ThemeTokens): React.CSSProperties {
  return { display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 8, padding: "10px 14px", borderRadius: 10, border: "none", cursor: "pointer", background: `linear-gradient(135deg, ${theme.accent}, ${theme.purple})`, color: "#fff", fontWeight: 600, fontSize: "var(--rm-fs-body)" };
}
function ghostBtn(theme: ThemeTokens): React.CSSProperties {
  return { padding: "5px 10px", borderRadius: 8, border: `1px solid ${theme.border}`, background: "transparent", color: theme.muted, cursor: "pointer", fontSize: "var(--rm-fs-caption)", display: "inline-flex", alignItems: "center", gap: 4 };
}

function recTone(rec: string): "success" | "danger" | "warning" {
  if (rec === "promote") return "success";
  if (rec === "rollback") return "danger";
  return "warning";
}

function friendlyError(raw: string): string {
  // The endpoint sends a JSON error body ({"detail": "..."}) as the message text.
  try {
    const parsed = JSON.parse(raw) as { detail?: string };
    if (parsed.detail) return parsed.detail;
  } catch { /* not JSON — show as-is */ }
  return raw;
}

function hexA(hex: string, alpha: number): string {
  const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

const KEYFRAMES = `
@keyframes rm-ai-pulse { 0%,100% { transform: scale(1); } 50% { transform: scale(1.06); } }
@keyframes rm-ai-work { 0% { box-shadow: 0 8px 24px rgba(99,91,255,0.35); } 50% { box-shadow: 0 12px 40px rgba(122,90,248,0.6); } 100% { box-shadow: 0 8px 24px rgba(99,91,255,0.35); } }
@keyframes rm-ai-bob { 0%,100% { transform: translateY(0); } 50% { transform: translateY(-3px); } }
@keyframes rm-ai-spin { to { transform: rotate(360deg); } }
@keyframes rm-ai-rise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }
@keyframes rm-ai-fade { 0%,100% { opacity: 0.55; } 50% { opacity: 1; } }
@media (prefers-reduced-motion: reduce) {
  [data-testid="ai-copilot-fab"], [data-testid="ai-thinking"] * { animation: none !important; }
}
`;
