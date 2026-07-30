"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES, type ThemeTokens } from "../../src/v3/theme";

// ---- types mirroring the backend policy-step model ----------------------------

type Step = {
  id: string;
  type: string;
  ref_id?: string;
  label?: string;
  outcome?: string;
  config?: Record<string, unknown>;
  [k: string]: unknown;
};
type Policy = { id: string; name: string; steps?: Step[]; defaultOutcome?: string };
type Ref = { id: string; name?: string; label?: string };
type MeceResult = { valid?: boolean; overlaps?: unknown[]; gaps?: unknown[]; issues?: unknown[]; message?: string };

// Palette — the executor's supported step types, grouped for discoverability.
const PALETTE: Array<{ type: string; label: string; needsRef?: "connector" | "rule" | "scorecard"; tone: keyof Pick<ThemeTokens, "accent" | "success" | "warning" | "danger" | "purple"> }> = [
  { type: "connector", label: "Connector", needsRef: "connector", tone: "accent" },
  { type: "rule", label: "Rule", needsRef: "rule", tone: "purple" },
  { type: "scorecard", label: "Scorecard", needsRef: "scorecard", tone: "purple" },
  { type: "transform", label: "Transform", tone: "accent" },
  { type: "branch", label: "Branch", tone: "warning" },
  { type: "loop", label: "Loop", tone: "warning" },
  { type: "workflow", label: "Sub-workflow", tone: "accent" },
  { type: "model", label: "ML model", tone: "purple" },
  { type: "action", label: "Action", tone: "accent" },
  { type: "monitor", label: "Monitor", tone: "warning" },
  { type: "review_gate", label: "Review gate", tone: "warning" },
  { type: "outcome", label: "Outcome", tone: "success" },
];

const TONE_OF: Record<string, keyof Pick<ThemeTokens, "accent" | "success" | "warning" | "danger" | "purple">> = Object.fromEntries(
  PALETTE.map((p) => [p.type, p.tone])
);

function toneColor(theme: ThemeTokens, type: string): string {
  const key = TONE_OF[type] ?? "accent";
  return theme[key];
}

let seq = 0;
function newId(type: string): string {
  seq += 1;
  return `${type}_${Date.now().toString(36)}_${seq}`;
}

export default function WorkflowBuilderPage() {
  const { apiBaseUrl, apiKey, themeMode } = useRuleMindStore();
  const theme = THEMES[themeMode];

  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [policyId, setPolicyId] = React.useState("");
  const [steps, setSteps] = React.useState<Step[]>([]);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [refs, setRefs] = React.useState<{ connector: Ref[]; rule: Ref[]; scorecard: Ref[] }>({ connector: [], rule: [], scorecard: [] });
  const [dirty, setDirty] = React.useState(false);
  const [status, setStatus] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [mece, setMece] = React.useState<MeceResult | null>(null);
  const dragIndex = React.useRef<number | null>(null);

  React.useEffect(() => {
    (async () => {
      try {
        const [ps, c, r, s] = await Promise.all([
          apiJson<Policy[]>(apiBaseUrl, "/api/v1/policies", {}, apiKey),
          apiJson<Ref[]>(apiBaseUrl, "/api/v1/connectors", {}, apiKey),
          apiJson<Ref[]>(apiBaseUrl, "/api/v1/rules", {}, apiKey),
          apiJson<Ref[]>(apiBaseUrl, "/api/v1/scorecards", {}, apiKey),
        ]);
        setPolicies(ps);
        setRefs({ connector: c, rule: r, scorecard: s });
        if (ps[0]) selectPolicy(ps[0].id, ps);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unable to load policies.");
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBaseUrl, apiKey]);

  const selectPolicy = (id: string, list = policies) => {
    const p = list.find((x) => x.id === id);
    setPolicyId(id);
    setSteps((p?.steps ?? []).map((s) => ({ ...s, id: s.id || newId(s.type) })));
    setSelected(null);
    setDirty(false);
    setMece(null);
    setStatus(null);
  };

  const mutate = (next: Step[]) => { setSteps(next); setDirty(true); setStatus(null); };
  const addStep = (type: string) => {
    const step: Step = { id: newId(type), type, label: PALETTE.find((p) => p.type === type)?.label ?? type };
    if (type === "outcome") step.outcome = "review";
    if (type === "branch") step.config = { branches: [], default: [] };
    if (type === "loop") step.config = { over: "", as: "item", indexAs: "index", maxIterations: 1000, steps: [] };
    mutate([...steps, step]);
    setSelected(step.id);
  };
  const removeStep = (id: string) => { mutate(steps.filter((s) => s.id !== id)); if (selected === id) setSelected(null); };
  const patchStep = (id: string, patch: Partial<Step>) => mutate(steps.map((s) => (s.id === id ? { ...s, ...patch } : s)));

  const onDrop = (target: number) => {
    const from = dragIndex.current;
    dragIndex.current = null;
    if (from === null || from === target) return;
    const next = [...steps];
    const [moved] = next.splice(from, 1);
    next.splice(target > from ? target - 1 : target, 0, moved);
    mutate(next);
  };

  const save = async () => {
    setError(null);
    try {
      const policy = policies.find((p) => p.id === policyId);
      await apiJson(apiBaseUrl, `/api/v1/policies/${policyId}`, { method: "PUT", body: JSON.stringify({ ...policy, steps }) }, apiKey);
      setPolicies((list) => list.map((p) => (p.id === policyId ? { ...p, steps } : p)));
      setDirty(false);
      setStatus("Saved.");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    }
  };

  const validate = async () => {
    setError(null);
    try {
      const res = await apiJson<MeceResult>(apiBaseUrl, `/api/v1/policies/${policyId}/analyze-mece`, { method: "POST", body: JSON.stringify({ steps }) }, apiKey);
      setMece(res);
    } catch (e) {
      setMece({ valid: false, message: e instanceof Error ? e.message : "Validation failed." });
    }
  };

  const selectedStep = steps.find((s) => s.id === selected) ?? null;

  return (
    <div style={{ padding: 20, height: "100%", display: "flex", flexDirection: "column" }}>
      {/* toolbar */}
      <div style={{ display: "flex", gap: 12, alignItems: "center", flexWrap: "wrap", marginBottom: 16 }}>
        <select value={policyId} onChange={(e) => selectPolicy(e.target.value)} style={selectStyle(theme)}>
          {policies.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <div style={{ flex: 1 }} />
        {dirty ? <span style={{ fontSize: 12, color: theme.warning }}>Unsaved changes</span> : null}
        {status ? <span style={{ fontSize: 12, color: theme.success }}>{status}</span> : null}
        <button onClick={validate} style={ghostStyle(theme)}>Validate (MECE)</button>
        <button onClick={save} disabled={!dirty} style={ctaStyle(theme, !dirty)}>Save workflow</button>
      </div>
      {error ? <div style={{ color: theme.danger, fontSize: 13, marginBottom: 10 }}>{error}</div> : null}

      <div style={{ display: "grid", gridTemplateColumns: "168px minmax(0,1fr) 300px", gap: 16, flex: 1, minHeight: 0 }}>
        {/* palette */}
        <div style={{ ...panel(theme), overflowY: "auto" }}>
          <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 1, color: theme.dim, marginBottom: 10 }}>Add step</div>
          <div style={{ display: "grid", gap: 6 }}>
            {PALETTE.map((p) => (
              <button key={p.type} onClick={() => addStep(p.type)}
                style={{ display: "flex", alignItems: "center", gap: 8, textAlign: "left", padding: "8px 10px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.card, color: theme.text, fontSize: 13, cursor: "pointer" }}>
                <span style={{ width: 8, height: 8, borderRadius: 2, background: theme[p.tone] }} />
                {p.label}
              </button>
            ))}
          </div>
        </div>

        {/* canvas */}
        <div style={{ ...panel(theme), overflowY: "auto", padding: 20 }}>
          {steps.length === 0 ? (
            <div style={{ color: theme.dim, fontSize: 14, textAlign: "center", marginTop: 60 }}>
              Empty workflow. Add steps from the palette to build the decision flow.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 0 }}>
              <FlowStart theme={theme} />
              {steps.map((step, i) => (
                <React.Fragment key={step.id}>
                  <Connector theme={theme} onDrop={() => onDrop(i)} />
                  <NodeCard
                    theme={theme} step={step} index={i} selected={selected === step.id}
                    onSelect={() => setSelected(step.id)}
                    onRemove={() => removeStep(step.id)}
                    onDragStart={() => { dragIndex.current = i; }}
                  />
                </React.Fragment>
              ))}
              <Connector theme={theme} onDrop={() => onDrop(steps.length)} />
              <FlowEnd theme={theme} outcome={policies.find((p) => p.id === policyId)?.defaultOutcome} />
            </div>
          )}
        </div>

        {/* inspector + validation */}
        <div style={{ display: "grid", gap: 16, gridTemplateRows: "1fr auto", minHeight: 0 }}>
          <div style={{ ...panel(theme), overflowY: "auto" }}>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 1, color: theme.dim, marginBottom: 12 }}>Inspector</div>
            {selectedStep ? (
              <Inspector theme={theme} step={selectedStep} refs={refs} onPatch={(patch) => patchStep(selectedStep.id, patch)} />
            ) : (
              <div style={{ color: theme.dim, fontSize: 13 }}>Select a node to edit it, or drag a node onto a connector to reorder.</div>
            )}
          </div>
          <div style={{ ...panel(theme) }}>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 1, color: theme.dim, marginBottom: 10 }}>Validation</div>
            {mece ? <MecePanel theme={theme} mece={mece} /> : <div style={{ color: theme.dim, fontSize: 13 }}>Run “Validate (MECE)” to check coverage & overlaps.</div>}
          </div>
        </div>
      </div>
    </div>
  );
}

// ---- flow pieces --------------------------------------------------------------

function FlowStart({ theme }: { theme: ThemeTokens }) {
  return <div style={{ padding: "6px 16px", borderRadius: 999, background: theme.accentBg, color: theme.accent, fontSize: 12, fontWeight: 700 }}>START · payload</div>;
}
function FlowEnd({ theme, outcome }: { theme: ThemeTokens; outcome?: string }) {
  return <div style={{ padding: "6px 16px", borderRadius: 999, background: theme.successBg, color: theme.success, fontSize: 12, fontWeight: 700 }}>DECISION{outcome ? ` · default ${outcome}` : ""}</div>;
}
function Connector({ theme, onDrop }: { theme: ThemeTokens; onDrop: () => void }) {
  const [over, setOver] = React.useState(false);
  return (
    <div
      onDragOver={(e) => { e.preventDefault(); setOver(true); }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => { e.preventDefault(); setOver(false); onDrop(); }}
      style={{ height: 24, width: over ? 40 : 2, background: over ? theme.accent : theme.border, borderRadius: 2, transition: "all .1s" }}
      aria-hidden
    />
  );
}

function NodeCard({ theme, step, index, selected, onSelect, onRemove, onDragStart }: {
  theme: ThemeTokens; step: Step; index: number; selected: boolean; onSelect: () => void; onRemove: () => void; onDragStart: () => void;
}) {
  const color = toneColor(theme, step.type);
  const branches = step.type === "branch" ? ((step.config?.branches as Array<{ label?: string }>) ?? []) : null;
  return (
    <div
      draggable
      onDragStart={onDragStart}
      onClick={onSelect}
      style={{
        width: 280, borderRadius: 10, border: `1px solid ${selected ? color : theme.border}`,
        boxShadow: selected ? `0 0 0 2px ${color}33` : "none",
        background: theme.card, cursor: "grab", overflow: "hidden",
      }}
    >
      <div style={{ height: 3, background: color }} />
      <div style={{ padding: "10px 12px", display: "flex", alignItems: "center", gap: 10 }}>
        <span style={{ fontSize: 10, color: theme.dim, fontFamily: "var(--font-mono)", minWidth: 18 }}>{index + 1}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: theme.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{step.label || step.type}</div>
          <div style={{ fontSize: 11, color: theme.muted }}>
            <span style={{ color }}>{step.type}</span>
            {step.ref_id ? <span> · {step.ref_id}</span> : null}
            {step.type === "outcome" && step.outcome ? <span> · {step.outcome}</span> : null}
          </div>
        </div>
        <button onClick={(e) => { e.stopPropagation(); onRemove(); }} title="Remove"
          style={{ border: "none", background: "transparent", color: theme.dim, cursor: "pointer", fontSize: 16, lineHeight: 1 }}>×</button>
      </div>
      {branches && branches.length > 0 ? (
        <div style={{ padding: "0 12px 10px", display: "grid", gap: 4 }}>
          {branches.map((b, i) => (
            <div key={i} style={{ fontSize: 11, color: theme.muted, padding: "3px 8px", background: theme.hover, borderRadius: 6, borderLeft: `2px solid ${theme.warning}` }}>
              branch {i + 1}: {b.label || "condition"}
            </div>
          ))}
          <div style={{ fontSize: 11, color: theme.dim, padding: "3px 8px" }}>else → default</div>
        </div>
      ) : null}
    </div>
  );
}

// ---- inspector ----------------------------------------------------------------

function Inspector({ theme, step, refs, onPatch }: { theme: ThemeTokens; step: Step; refs: { connector: Ref[]; rule: Ref[]; scorecard: Ref[] }; onPatch: (p: Partial<Step>) => void }) {
  const needsRef = PALETTE.find((p) => p.type === step.type)?.needsRef;
  const options = needsRef ? refs[needsRef] : [];
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Field theme={theme} label="Type"><div style={{ fontSize: 13, color: theme.text, fontWeight: 600 }}>{step.type}</div></Field>
      <Field theme={theme} label="Label">
        <input value={step.label ?? ""} onChange={(e) => onPatch({ label: e.target.value })} style={inputStyle(theme)} />
      </Field>
      {needsRef ? (
        <Field theme={theme} label={`${needsRef} reference`}>
          <select value={step.ref_id ?? ""} onChange={(e) => onPatch({ ref_id: e.target.value })} style={selectStyle(theme)}>
            <option value="">— select —</option>
            {options.map((o) => <option key={o.id} value={o.id}>{o.name || o.label || o.id}</option>)}
          </select>
        </Field>
      ) : null}
      {step.type === "outcome" ? (
        <Field theme={theme} label="Outcome">
          <select value={step.outcome ?? "review"} onChange={(e) => onPatch({ outcome: e.target.value })} style={selectStyle(theme)}>
            {["approve", "review", "reject"].map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </Field>
      ) : null}
      {step.type === "workflow" ? (
        <Field theme={theme} label="Sub-workflow policy id">
          <input value={step.ref_id ?? ""} onChange={(e) => onPatch({ ref_id: e.target.value })} placeholder="policy id" style={inputStyle(theme)} />
        </Field>
      ) : null}
      {(step.type === "action" || step.type === "monitor") ? (
        <Field theme={theme} label="Config (JSON)" hint="request / webhook / schedule">
          <JsonEditor theme={theme} value={step.config ?? {}} onChange={(cfg) => onPatch({ config: cfg })} />
        </Field>
      ) : null}
      {step.type === "branch" ? (
        <BranchEditor
          theme={theme}
          refs={refs}
          config={(step.config as BranchConfig) ?? { branches: [], default: [] }}
          onChange={(cfg) => onPatch({ config: cfg as unknown as Record<string, unknown> })}
        />
      ) : null}
      {step.type === "loop" ? (
        <LoopEditor
          theme={theme}
          refs={refs}
          config={(step.config as LoopConfig) ?? { steps: [] }}
          onChange={(cfg) => onPatch({ config: cfg as unknown as Record<string, unknown> })}
        />
      ) : null}
    </div>
  );
}

// ---- loop editor (iterate a collection variable over a body of steps) ----

type LoopConfig = { over?: string; as?: string; indexAs?: string; maxIterations?: number; steps?: Step[] };

function LoopEditor({ theme, refs, config, onChange }: { theme: ThemeTokens; refs: { connector: Ref[]; rule: Ref[]; scorecard: Ref[] }; config: LoopConfig; onChange: (c: LoopConfig) => void }) {
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div style={{ fontSize: 12, color: theme.muted, lineHeight: 1.5 }}>
        Iterates the body once per item in a collection. Point <span style={{ fontFamily: "var(--font-mono)" }}>over</span> at a
        variable that returns a list or map (e.g. <span style={{ fontFamily: "var(--font-mono)" }}>variables.line_items</span>).
        Each item is exposed under the <span style={{ fontFamily: "var(--font-mono)" }}>as</span> name to body steps and conditions.
      </div>
      <Field theme={theme} label="Over (collection path)">
        <input value={config.over ?? ""} onChange={(e) => onChange({ ...config, over: e.target.value })} placeholder="variables.line_items" style={{ ...inputStyle(theme), fontFamily: "var(--font-mono)", fontSize: 12 }} />
      </Field>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 8 }}>
        <Field theme={theme} label="Item name">
          <input value={config.as ?? "item"} onChange={(e) => onChange({ ...config, as: e.target.value })} style={{ ...inputStyle(theme), fontSize: 12 }} />
        </Field>
        <Field theme={theme} label="Index name">
          <input value={config.indexAs ?? "index"} onChange={(e) => onChange({ ...config, indexAs: e.target.value })} style={{ ...inputStyle(theme), fontSize: 12 }} />
        </Field>
        <Field theme={theme} label="Max iters">
          <input type="number" value={config.maxIterations ?? 1000} onChange={(e) => onChange({ ...config, maxIterations: Number(e.target.value) })} style={{ ...inputStyle(theme), fontSize: 12 }} />
        </Field>
      </div>
      <div style={{ borderTop: `1px dashed ${theme.border}`, paddingTop: 10 }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: theme.dim, marginBottom: 6 }}>LOOP BODY (per item)</div>
        <StepRows theme={theme} refs={refs} steps={config.steps ?? []} onChange={(s) => onChange({ ...config, steps: s })} />
      </div>
    </div>
  );
}

// ---- branch editor (fully editable: add/remove branches, condition, nested steps) ----

type Branch = { label?: string; condition?: string; steps?: Step[] };
type BranchConfig = { branches?: Branch[]; default?: Step[] };

function BranchEditor({ theme, refs, config, onChange }: { theme: ThemeTokens; refs: { connector: Ref[]; rule: Ref[]; scorecard: Ref[] }; config: BranchConfig; onChange: (c: BranchConfig) => void }) {
  const branches = config.branches ?? [];
  const setBranches = (b: Branch[]) => onChange({ ...config, branches: b });
  const patchBranch = (i: number, p: Partial<Branch>) => setBranches(branches.map((b, j) => (j === i ? { ...b, ...p } : b)));
  return (
    <div style={{ display: "grid", gap: 12 }}>
      <div style={{ fontSize: 12, color: theme.muted, lineHeight: 1.5 }}>
        Runs the first branch whose condition is true, else the default lane. Condition is a Python
        expression over <span style={{ fontFamily: "var(--font-mono)" }}>variables</span> / <span style={{ fontFamily: "var(--font-mono)" }}>payload</span> (e.g. <span style={{ fontFamily: "var(--font-mono)" }}>{`variables['credit_score'] >= 700`}</span>).
      </div>
      {branches.map((b, i) => (
        <div key={i} style={{ border: `1px solid ${theme.border}`, borderLeft: `3px solid ${theme.warning}`, borderRadius: 8, padding: 10, display: "grid", gap: 8 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: theme.warning }}>BRANCH {i + 1}</span>
            <button onClick={() => setBranches(branches.filter((_, j) => j !== i))} style={{ ...ghostStyle(theme), padding: "4px 8px", fontSize: 11 }}>Remove</button>
          </div>
          <input value={b.label ?? ""} onChange={(e) => patchBranch(i, { label: e.target.value })} placeholder="Label" style={{ ...inputStyle(theme), fontSize: 12 }} />
          <input value={b.condition ?? ""} onChange={(e) => patchBranch(i, { condition: e.target.value })} placeholder="variables['x'] >= 700" style={{ ...inputStyle(theme), fontFamily: "var(--font-mono)", fontSize: 12 }} />
          <StepRows theme={theme} refs={refs} steps={b.steps ?? []} onChange={(s) => patchBranch(i, { steps: s })} />
        </div>
      ))}
      <button onClick={() => setBranches([...branches, { label: `Branch ${branches.length + 1}`, condition: "", steps: [] }])} style={ghostStyle(theme)}>+ Add branch</button>
      <div style={{ borderTop: `1px dashed ${theme.border}`, paddingTop: 10 }}>
        <div style={{ fontSize: 11, fontWeight: 700, color: theme.dim, marginBottom: 6 }}>DEFAULT LANE (else)</div>
        <StepRows theme={theme} refs={refs} steps={config.default ?? []} onChange={(s) => onChange({ ...config, default: s })} />
      </div>
    </div>
  );
}

function StepRows({ theme, refs, steps, onChange }: { theme: ThemeTokens; refs: { connector: Ref[]; rule: Ref[]; scorecard: Ref[] }; steps: Step[]; onChange: (s: Step[]) => void }) {
  const [addType, setAddType] = React.useState("rule");
  const patch = (i: number, p: Partial<Step>) => onChange(steps.map((s, j) => (j === i ? { ...s, ...p } : s)));
  return (
    <div style={{ display: "grid", gap: 6 }}>
      {steps.map((s, i) => {
        const needsRef = PALETTE.find((p) => p.type === s.type)?.needsRef;
        return (
          <div key={s.id || i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
            <span style={{ width: 7, height: 7, borderRadius: 2, background: toneColor(theme, s.type), flexShrink: 0 }} />
            <span style={{ fontSize: 11, color: theme.muted, width: 62, flexShrink: 0 }}>{s.type}</span>
            {needsRef ? (
              <select value={s.ref_id ?? ""} onChange={(e) => patch(i, { ref_id: e.target.value })} style={{ ...selectStyle(theme), fontSize: 12, flex: 1 }}>
                <option value="">— ref —</option>
                {refs[needsRef].map((o) => <option key={o.id} value={o.id}>{o.name || o.label || o.id}</option>)}
              </select>
            ) : s.type === "outcome" ? (
              <select value={s.outcome ?? "review"} onChange={(e) => patch(i, { outcome: e.target.value })} style={{ ...selectStyle(theme), fontSize: 12, flex: 1 }}>
                {["approve", "review", "reject"].map((o) => <option key={o} value={o}>{o}</option>)}
              </select>
            ) : (
              <input value={s.label ?? ""} onChange={(e) => patch(i, { label: e.target.value })} placeholder="label" style={{ ...inputStyle(theme), fontSize: 12, flex: 1 }} />
            )}
            <button onClick={() => onChange(steps.filter((_, j) => j !== i))} style={{ ...ghostStyle(theme), padding: "4px 7px", fontSize: 11 }}>×</button>
          </div>
        );
      })}
      <div style={{ display: "flex", gap: 6 }}>
        <select value={addType} onChange={(e) => setAddType(e.target.value)} style={{ ...selectStyle(theme), fontSize: 12, flex: 1 }}>
          {PALETTE.filter((p) => p.type !== "branch" && p.type !== "loop").map((p) => <option key={p.type} value={p.type}>{p.label}</option>)}
        </select>
        <button onClick={() => onChange([...steps, { id: newId(addType), type: addType, label: PALETTE.find((p) => p.type === addType)?.label, ...(addType === "outcome" ? { outcome: "review" } : {}) }])} style={{ ...ghostStyle(theme), fontSize: 12 }}>Add step</button>
      </div>
    </div>
  );
}

function JsonEditor({ theme, value, onChange }: { theme: ThemeTokens; value: Record<string, unknown>; onChange: (v: Record<string, unknown>) => void }) {
  const [text, setText] = React.useState(JSON.stringify(value, null, 2));
  const [bad, setBad] = React.useState(false);
  React.useEffect(() => { setText(JSON.stringify(value, null, 2)); }, [value]);
  return (
    <div>
      <textarea value={text} spellCheck={false}
        onChange={(e) => {
          setText(e.target.value);
          try { onChange(JSON.parse(e.target.value)); setBad(false); } catch { setBad(true); }
        }}
        style={{ ...inputStyle(theme), fontFamily: "var(--font-mono)", fontSize: 12, minHeight: 120, resize: "vertical", borderColor: bad ? theme.danger : theme.border }} />
      {bad ? <div style={{ fontSize: 11, color: theme.danger, marginTop: 4 }}>Invalid JSON — not applied</div> : null}
    </div>
  );
}

function MecePanel({ theme, mece }: { theme: ThemeTokens; mece: MeceResult }) {
  const overlaps = (mece.overlaps ?? []) as unknown[];
  const gaps = (mece.gaps ?? []) as unknown[];
  const ok = mece.valid !== false && overlaps.length === 0 && gaps.length === 0 && !mece.message;
  return (
    <div style={{ fontSize: 13 }}>
      <div style={{ color: ok ? theme.success : theme.danger, fontWeight: 600, marginBottom: 6 }}>
        {ok ? "✓ Coverage valid" : "⚠ Issues found"}
      </div>
      {mece.message ? <div style={{ color: theme.muted, fontSize: 12 }}>{mece.message}</div> : null}
      {overlaps.length > 0 ? <div style={{ color: theme.warning, fontSize: 12 }}>{overlaps.length} overlapping range(s)</div> : null}
      {gaps.length > 0 ? <div style={{ color: theme.warning, fontSize: 12 }}>{gaps.length} coverage gap(s)</div> : null}
    </div>
  );
}

// ---- shared style helpers -----------------------------------------------------

function Field({ theme, label, hint, children }: { theme: ThemeTokens; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "grid", gap: 5 }}>
      <span style={{ fontSize: 12, fontWeight: 600, color: theme.text }}>{label}</span>
      {hint ? <span style={{ fontSize: 11, color: theme.dim }}>{hint}</span> : null}
      {children}
    </label>
  );
}
function panel(theme: ThemeTokens): React.CSSProperties {
  return { background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, minHeight: 0 };
}
function inputStyle(theme: ThemeTokens): React.CSSProperties {
  return { padding: "8px 10px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.input, color: theme.text, fontSize: 13, outline: "none", width: "100%", boxSizing: "border-box" };
}
function selectStyle(theme: ThemeTokens): React.CSSProperties {
  return { ...inputStyle(theme), cursor: "pointer" };
}
function ctaStyle(theme: ThemeTokens, disabled: boolean): React.CSSProperties {
  return { background: theme.accent, color: theme.inverseText, border: "none", borderRadius: 8, padding: "8px 16px", fontSize: 13, fontWeight: 600, cursor: disabled ? "not-allowed" : "pointer", opacity: disabled ? 0.5 : 1 };
}
function ghostStyle(theme: ThemeTokens): React.CSSProperties {
  return { background: "transparent", color: theme.muted, border: "1px solid " + theme.border, borderRadius: 8, padding: "8px 14px", fontSize: 13, fontWeight: 600, cursor: "pointer" };
}
