"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES, type ThemeTokens } from "../../src/v3/theme";

// Build Studio — a single canvas-first surface that replaces the split
// Policies-step-list / Workflow-Builder editors. A policy is a vertical pipeline
// of nodes; selecting a node opens an inline drawer to edit it (rules are edited
// in place; scorecards / decision tables are referenced with a quick-open).

type Step = { id: string; type: string; ref_id?: string; label?: string; outcome?: string; config?: Record<string, unknown>; [k: string]: unknown };
type Policy = { id: string; name: string; steps?: Step[]; defaultOutcome?: string };
type Ref = { id: string; name?: string; label?: string };
type Variable = { id: string; name?: string };
type Mece = { valid?: boolean; message?: string; issues?: unknown[]; overlaps?: unknown[]; gaps?: unknown[] };

const OPERATORS = ["==", "!=", ">", ">=", "<", "<=", "between", "in", "not_in", "regex", "exists", "!exists"];
const OUTCOMES = ["approve", "review", "reject"];

// Palette — grouped so it reads as "pull data → compute → decide", not a flat wall.
const PALETTE: Array<{ type: string; label: string; needsRef?: "connector" | "rule" | "scorecard" | "decision_table" | "model"; tone: keyof Pick<ThemeTokens, "accent" | "success" | "warning" | "danger" | "purple">; group: string }> = [
  { type: "connector", label: "Data source", needsRef: "connector", tone: "accent", group: "Pull" },
  { type: "rule", label: "Rule", needsRef: "rule", tone: "purple", group: "Decide" },
  { type: "decision_table", label: "Decision table", needsRef: "decision_table", tone: "purple", group: "Decide" },
  { type: "scorecard", label: "Scorecard", needsRef: "scorecard", tone: "purple", group: "Decide" },
  { type: "model", label: "ML model", needsRef: "model", tone: "purple", group: "Decide" },
  { type: "branch", label: "Branch", tone: "warning", group: "Flow" },
  { type: "loop", label: "Loop", tone: "warning", group: "Flow" },
  { type: "review_gate", label: "Review gate", tone: "warning", group: "Flow" },
  { type: "outcome", label: "Outcome", tone: "success", group: "Decide" },
];
const TONE_OF: Record<string, keyof Pick<ThemeTokens, "accent" | "success" | "warning" | "danger" | "purple">> =
  Object.fromEntries(PALETTE.map((p) => [p.type, p.tone]));
const NEEDS_REF: Record<string, "connector" | "rule" | "scorecard" | "decision_table" | "model" | undefined> =
  Object.fromEntries(PALETTE.map((p) => [p.type, p.needsRef]));

let seq = 0;
const newId = (t: string) => `${t}_${Date.now().toString(36)}_${seq++}`;

export default function BuildStudioPage() {
  const { apiBaseUrl, apiKey, themeMode } = useRuleMindStore();
  const theme = THEMES[themeMode];

  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [policyId, setPolicyId] = React.useState("");
  const [name, setName] = React.useState("");
  const [steps, setSteps] = React.useState<Step[]>([]);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [refs, setRefs] = React.useState<{ connector: Ref[]; rule: Ref[]; scorecard: Ref[]; decision_table: Ref[]; model: Ref[] }>(
    { connector: [], rule: [], scorecard: [], decision_table: [], model: [] });
  const [variables, setVariables] = React.useState<Variable[]>([]);
  const [dirty, setDirty] = React.useState(false);
  const [status, setStatus] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [mece, setMece] = React.useState<Mece | null>(null);
  const dragIndex = React.useRef<number | null>(null);

  const loadRefs = React.useCallback(async () => {
    const get = async <T,>(path: string): Promise<T[]> => { try { return await apiJson<T[]>(apiBaseUrl, path, {}, apiKey); } catch { return []; } };
    const [ps, c, r, s, d, m, v] = await Promise.all([
      get<Policy>("/api/v1/policies"), get<Ref>("/api/v1/connectors"), get<Ref>("/api/v1/rules"),
      get<Ref>("/api/v1/scorecards"), get<Ref>("/api/v1/decision-tables"), get<Ref>("/api/v1/models"), get<Variable>("/api/v1/variables"),
    ]);
    setPolicies(ps); setRefs({ connector: c, rule: r, scorecard: s, decision_table: d, model: m }); setVariables(v);
    return ps;
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void loadRefs().then((ps) => { if (ps[0]) selectPolicy(ps[0].id, ps); }); /* eslint-disable-next-line */ }, [apiBaseUrl, apiKey]);

  const selectPolicy = (id: string, list = policies) => {
    const p = list.find((x) => x.id === id);
    setPolicyId(id); setName(p?.name ?? "");
    setSteps((p?.steps ?? []).map((s) => ({ ...s, id: s.id || newId(s.type) })));
    setSelected(null); setDirty(false); setStatus(null); setMece(null);
  };
  const newPolicy = () => { setPolicyId(""); setName(""); setSteps([]); setSelected(null); setDirty(false); setMece(null); setStatus(null); };

  const mutate = (next: Step[]) => { setSteps(next); setDirty(true); setStatus(null); };
  const addNode = (type: string) => {
    const step: Step = { id: newId(type), type, label: PALETTE.find((p) => p.type === type)?.label ?? type };
    if (type === "outcome") step.outcome = "review";
    if (type === "branch") step.config = { branches: [], default: [] };
    if (type === "loop") step.config = { over: "", as: "item", steps: [] };
    mutate([...steps, step]); setSelected(step.id);
  };
  const patchStep = (id: string, patch: Partial<Step>) => mutate(steps.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  const removeStep = (id: string) => { mutate(steps.filter((s) => s.id !== id)); if (selected === id) setSelected(null); };
  const move = (from: number, to: number) => { if (to < 0 || to >= steps.length) return; const n = [...steps]; const [m] = n.splice(from, 1); n.splice(to, 0, m); mutate(n); };
  const onDrop = (target: number) => { const from = dragIndex.current; dragIndex.current = null; if (from === null || from === target) return; const n = [...steps]; const [m] = n.splice(from, 1); n.splice(target > from ? target - 1 : target, 0, m); mutate(n); };

  const save = async () => {
    setError(null); setStatus(null);
    if (!name.trim()) { setError("Name the policy."); return; }
    try {
      const body = JSON.stringify({ name, steps });
      const saved = policyId
        ? await apiJson<Policy>(apiBaseUrl, `/api/v1/policies/${policyId}`, { method: "PUT", body }, apiKey)
        : await apiJson<Policy>(apiBaseUrl, "/api/v1/policies", { method: "POST", body }, apiKey);
      setStatus(`Saved "${saved.name}".`); setDirty(false);
      const ps = await loadRefs(); selectPolicy(saved.id, ps);
    } catch (e) { setError(e instanceof Error ? e.message : "Save failed."); }
  };
  const analyze = async () => {
    setError(null);
    try { setMece(await apiJson<Mece>(apiBaseUrl, `/api/v1/policies/${policyId || "draft"}/analyze-mece`, { method: "POST", body: JSON.stringify({ steps }) }, apiKey)); }
    catch (e) { setMece({ valid: false, message: e instanceof Error ? e.message : "Validation failed." }); }
  };

  const selectedStep = steps.find((s) => s.id === selected) || null;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "calc(100vh - 120px)", minHeight: 520 }}>
      {/* top bar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", paddingBottom: 12, borderBottom: `1px solid ${theme.border}` }}>
        <select value={policyId} onChange={(e) => selectPolicy(e.target.value)} style={sel(theme)}>
          <option value="">— new policy —</option>
          {policies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <button onClick={newPolicy} style={ghost(theme)}>+ New</button>
        <input value={name} onChange={(e) => { setName(e.target.value); setDirty(true); }} placeholder="Policy name" style={{ ...inp(theme), flex: 1, minWidth: 180 }} />
        {dirty ? <span style={{ fontSize: 12, color: theme.warning }}>unsaved</span> : null}
        <button onClick={analyze} disabled={!steps.length} style={ghost(theme)}>Analyze MECE</button>
        <button onClick={save} style={cta(theme)}>Save</button>
      </div>

      {status ? <div style={{ color: theme.success, fontSize: 13, padding: "8px 0" }}>{status}</div> : null}
      {error ? <div style={{ color: theme.danger, fontSize: 13, padding: "8px 0" }}>{error}</div> : null}
      {mece ? (
        <div style={{ fontSize: 12.5, padding: "8px 12px", margin: "8px 0", borderRadius: 8, background: mece.valid ? theme.successBg : theme.dangerBg, color: mece.valid ? theme.success : theme.danger }}>
          {mece.valid ? "MECE OK — mutually exclusive and collectively exhaustive." : (mece.message || `${(mece.overlaps?.length ?? 0)} overlap(s), ${(mece.gaps?.length ?? 0)} gap(s).`)}
        </div>
      ) : null}

      <div style={{ display: "grid", gridTemplateColumns: "170px minmax(0,1fr) 340px", gap: 0, flex: 1, minHeight: 0 }}>
        {/* palette */}
        <div style={{ borderRight: `1px solid ${theme.border}`, padding: "12px 10px", overflowY: "auto" }}>
          {["Pull", "Decide", "Flow"].map((g) => (
            <div key={g} style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6, color: theme.dim, fontWeight: 700, marginBottom: 6 }}>{g}</div>
              {PALETTE.filter((p) => p.group === g).map((p) => (
                <button key={p.type} onClick={() => addNode(p.type)}
                  style={{ display: "flex", alignItems: "center", gap: 8, width: "100%", textAlign: "left", padding: "7px 9px", marginBottom: 4, borderRadius: 8, border: `1px solid ${theme.border}`, background: "transparent", color: theme.text, fontSize: 12.5, cursor: "pointer" }}>
                  <span style={{ width: 8, height: 8, borderRadius: 3, background: theme[p.tone], flexShrink: 0 }} /> {p.label}
                </button>
              ))}
            </div>
          ))}
        </div>

        {/* canvas */}
        <div style={{ overflowY: "auto", padding: "18px 20px", background: theme.bg }}>
          {steps.length === 0 ? (
            <div style={{ height: "100%", display: "grid", placeItems: "center", color: theme.muted, fontSize: 14, textAlign: "center" }}>
              <div>Add a node from the left to start building.<br /><span style={{ fontSize: 12.5, color: theme.dim }}>Pull data → compute a decision → set the outcome.</span></div>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 0 }}>
              {steps.map((s, i) => {
                const tone = theme[TONE_OF[s.type] ?? "accent"];
                const ref = NEEDS_REF[s.type] ? refs[NEEDS_REF[s.type]!].find((r) => r.id === s.ref_id) : undefined;
                return (
                  <React.Fragment key={s.id}>
                    {i > 0 ? <div style={{ width: 2, height: 22, background: theme.border }} /> : null}
                    <div draggable onDragStart={() => (dragIndex.current = i)} onDragOver={(e) => e.preventDefault()} onDrop={() => onDrop(i)}
                      onClick={() => setSelected(s.id)}
                      style={{ width: 320, borderRadius: 12, border: `1px solid ${selected === s.id ? tone : theme.border}`, boxShadow: selected === s.id ? `0 0 0 3px ${tone}22` : "none", background: theme.card, cursor: "pointer", overflow: "hidden" }}>
                      <div style={{ height: 3, background: tone }} />
                      <div style={{ padding: "11px 13px", display: "flex", alignItems: "center", gap: 10 }}>
                        <span style={{ fontSize: 10, color: theme.dim, fontFamily: "var(--font-mono)", width: 16 }}>{i + 1}</span>
                        <div style={{ flex: 1, minWidth: 0 }}>
                          <div style={{ fontSize: 13.5, fontWeight: 600, color: theme.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{s.label || s.type}</div>
                          <div style={{ fontSize: 11, color: theme.muted }}>
                            <span style={{ color: tone }}>{s.type}</span>
                            {ref ? <span> · {ref.name || ref.label || ref.id}</span> : null}
                            {s.type === "outcome" && s.outcome ? <span> · {s.outcome}</span> : null}
                          </div>
                        </div>
                        <button onClick={(e) => { e.stopPropagation(); move(i, i - 1); }} title="Up" style={iconBtn(theme)}>↑</button>
                        <button onClick={(e) => { e.stopPropagation(); move(i, i + 1); }} title="Down" style={iconBtn(theme)}>↓</button>
                        <button onClick={(e) => { e.stopPropagation(); removeStep(s.id); }} title="Remove" style={iconBtn(theme)}>×</button>
                      </div>
                    </div>
                  </React.Fragment>
                );
              })}
            </div>
          )}
        </div>

        {/* inline drawer */}
        <div style={{ borderLeft: `1px solid ${theme.border}`, padding: 16, overflowY: "auto", background: theme.card }}>
          {selectedStep ? (
            <NodeDrawer theme={theme} step={selectedStep} refs={refs} variables={variables}
              onPatch={(p) => patchStep(selectedStep.id, p)}
              onRuleSaved={async () => { await loadRefs(); }}
              apiBaseUrl={apiBaseUrl} apiKey={apiKey} />
          ) : (
            <div style={{ color: theme.muted, fontSize: 13, marginTop: 20 }}>Select a node to edit it here.</div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---- node drawer (inline editing) --------------------------------------------

function NodeDrawer({ theme, step, refs, variables, onPatch, onRuleSaved, apiBaseUrl, apiKey }: {
  theme: ThemeTokens; step: Step; refs: Record<string, Ref[]>; variables: Variable[];
  onPatch: (p: Partial<Step>) => void; onRuleSaved: () => Promise<void>; apiBaseUrl: string; apiKey: string;
}) {
  const needsRef = NEEDS_REF[step.type];
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 0.5, color: theme.dim, fontWeight: 700 }}>{step.type}</div>
      <Field theme={theme} label="Label"><input value={step.label ?? ""} onChange={(e) => onPatch({ label: e.target.value })} style={inp(theme)} /></Field>

      {needsRef ? (
        <Field theme={theme} label={`${needsRef.replace("_", " ")} reference`}>
          <select value={step.ref_id ?? ""} onChange={(e) => onPatch({ ref_id: e.target.value })} style={sel(theme)}>
            <option value="">— select —</option>
            {(refs[needsRef] || []).map((o) => <option key={o.id} value={o.id}>{o.name || o.label || o.id}</option>)}
          </select>
        </Field>
      ) : null}

      {step.type === "rule" ? (
        <InlineRuleEditor theme={theme} variables={variables} apiBaseUrl={apiBaseUrl} apiKey={apiKey}
          onSaved={async (ruleId) => { await onRuleSaved(); onPatch({ ref_id: ruleId }); }} />
      ) : null}

      {(step.type === "scorecard" || step.type === "decision_table") ? (
        <a href={step.type === "scorecard" ? "/scorecards" : "/decision-tables"} style={{ fontSize: 12.5, color: theme.accent }}>
          Open the {step.type.replace("_", " ")} editor ↗
        </a>
      ) : null}

      {step.type === "outcome" ? (
        <Field theme={theme} label="Outcome">
          <select value={step.outcome ?? "review"} onChange={(e) => onPatch({ outcome: e.target.value })} style={sel(theme)}>
            {OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </Field>
      ) : null}

      {step.type === "workflow" ? (
        <Field theme={theme} label="Sub-policy id"><input value={step.ref_id ?? ""} onChange={(e) => onPatch({ ref_id: e.target.value })} placeholder="policy id" style={inp(theme)} /></Field>
      ) : null}

      {step.type === "branch" ? (
        <Field theme={theme} label="Branch condition (Python)" hint="e.g. variables['credit_score'] >= 700">
          <input value={String((step.config as Record<string, unknown>)?.condition ?? "")} onChange={(e) => onPatch({ config: { ...(step.config || {}), condition: e.target.value } })} style={{ ...inp(theme), fontFamily: "var(--font-mono)" }} />
        </Field>
      ) : null}

      {step.type === "loop" ? (
        <>
          <Field theme={theme} label="Iterate over" hint="a variable that returns a list, e.g. variables.line_items">
            <input value={String((step.config as Record<string, unknown>)?.over ?? "")} onChange={(e) => onPatch({ config: { ...(step.config || {}), over: e.target.value } })} placeholder="variables.line_items" style={{ ...inp(theme), fontFamily: "var(--font-mono)" }} />
          </Field>
          <Field theme={theme} label="Item name">
            <input value={String((step.config as Record<string, unknown>)?.as ?? "item")} onChange={(e) => onPatch({ config: { ...(step.config || {}), as: e.target.value } })} style={inp(theme)} />
          </Field>
        </>
      ) : null}

      {step.type === "review_gate" ? (
        <Field theme={theme} label="Assign to queue">
          <input value={String((step.config as Record<string, unknown>)?.assignTo ?? "")} onChange={(e) => onPatch({ config: { ...(step.config || {}), assignTo: e.target.value } })} placeholder="underwriting_queue" style={inp(theme)} />
        </Field>
      ) : null}
    </div>
  );
}

// A compact inline rule authoring surface — conditions joined by AND/OR → outcome,
// saved to /rules and referenced by the node. This is the "edit on the node" flow.
function InlineRuleEditor({ theme, variables, apiBaseUrl, apiKey, onSaved }: {
  theme: ThemeTokens; variables: Variable[]; apiBaseUrl: string; apiKey: string; onSaved: (ruleId: string) => Promise<void>;
}) {
  const [ruleName, setRuleName] = React.useState("");
  const [logic, setLogic] = React.useState<"AND" | "OR">("AND");
  const [outcome, setOutcome] = React.useState("approve");
  const [conds, setConds] = React.useState<Array<{ variable: string; operator: string; value: string }>>([{ variable: "", operator: ">=", value: "" }]);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState<string | null>(null);

  const setC = (i: number, p: Partial<{ variable: string; operator: string; value: string }>) => setConds((c) => c.map((x, j) => (j === i ? { ...x, ...p } : x)));

  const create = async () => {
    setBusy(true); setMsg(null);
    try {
      // Build the flat node list the rule engine expects (condition/logic/outcome).
      const nodes: Array<Record<string, unknown>> = [];
      conds.filter((c) => c.variable).forEach((c, i) => {
        if (i > 0) nodes.push({ id: `l${i}`, type: logic.toLowerCase(), label: logic });
        nodes.push({ id: `c${i}`, type: "condition", variable: c.variable, operator: c.operator, value: c.value });
      });
      nodes.push({ id: "out", type: outcome, label: outcome });
      const saved = await apiJson<{ id: string }>(apiBaseUrl, "/api/v1/rules", { method: "POST", body: JSON.stringify({ name: ruleName || "Inline rule", nodes }) }, apiKey);
      setMsg("Rule created and linked.");
      await onSaved(saved.id);
    } catch (e) { setMsg(e instanceof Error ? e.message : "Could not create rule."); }
    finally { setBusy(false); }
  };

  return (
    <div style={{ borderTop: `1px dashed ${theme.border}`, paddingTop: 12, display: "grid", gap: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: theme.dim }}>OR AUTHOR A NEW RULE INLINE</div>
      <input value={ruleName} onChange={(e) => setRuleName(e.target.value)} placeholder="Rule name" style={{ ...inp(theme), fontSize: 12 }} />
      <div style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: theme.muted }}>
        Match
        <select value={logic} onChange={(e) => setLogic(e.target.value as "AND" | "OR")} style={{ ...sel(theme), width: 70, fontSize: 12 }}>
          <option>AND</option><option>OR</option>
        </select>
        of:
      </div>
      {conds.map((c, i) => (
        <div key={i} style={{ display: "flex", gap: 4 }}>
          <select value={c.variable} onChange={(e) => setC(i, { variable: e.target.value })} style={{ ...sel(theme), flex: 1, fontSize: 12, minWidth: 0 }}>
            <option value="">variable…</option>
            {variables.map((v) => <option key={v.id} value={v.id}>{v.name || v.id}</option>)}
          </select>
          <select value={c.operator} onChange={(e) => setC(i, { operator: e.target.value })} style={{ ...sel(theme), width: 66, fontSize: 12 }}>
            {OPERATORS.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <input value={c.value} onChange={(e) => setC(i, { value: e.target.value })} placeholder="value" style={{ ...inp(theme), width: 60, fontSize: 12 }} />
          <button onClick={() => setConds((cs) => cs.filter((_, j) => j !== i))} style={iconBtn(theme)}>×</button>
        </div>
      ))}
      <button onClick={() => setConds((c) => [...c, { variable: "", operator: ">=", value: "" }])} style={{ ...ghost(theme), fontSize: 12 }}>+ condition</button>
      <div style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, color: theme.muted }}>
        then
        <select value={outcome} onChange={(e) => setOutcome(e.target.value)} style={{ ...sel(theme), width: 110, fontSize: 12 }}>
          {OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      </div>
      <button onClick={create} disabled={busy} style={cta(theme)}>{busy ? "Creating…" : "Create & link rule"}</button>
      {msg ? <div style={{ fontSize: 12, color: theme.text }}>{msg}</div> : null}
    </div>
  );
}

// ---- small styled helpers -----------------------------------------------------
function Field({ theme, label, hint, children }: { theme: ThemeTokens; label: string; hint?: string; children: React.ReactNode }) {
  return (<label style={{ display: "grid", gap: 5 }}>
    <span style={{ fontSize: 12.5, fontWeight: 600, color: theme.text }}>{label}</span>
    {hint ? <span style={{ fontSize: 11.5, color: theme.dim }}>{hint}</span> : null}
    {children}
  </label>);
}
const inp = (t: ThemeTokens): React.CSSProperties => ({ padding: "8px 10px", borderRadius: 8, border: `1px solid ${t.border}`, background: t.input, color: t.text, fontSize: 13, outline: "none" });
const sel = (t: ThemeTokens): React.CSSProperties => ({ ...inp(t), cursor: "pointer" });
const ghost = (t: ThemeTokens): React.CSSProperties => ({ padding: "8px 12px", borderRadius: 8, border: `1px solid ${t.border}`, background: "transparent", color: t.text, fontSize: 13, cursor: "pointer" });
const cta = (t: ThemeTokens): React.CSSProperties => ({ padding: "8px 14px", borderRadius: 8, border: "none", background: t.accent, color: t.inverseText, fontSize: 13, fontWeight: 600, cursor: "pointer" });
const iconBtn = (t: ThemeTokens): React.CSSProperties => ({ border: "none", background: "transparent", color: t.dim, cursor: "pointer", fontSize: 15, lineHeight: 1, padding: 2 });
