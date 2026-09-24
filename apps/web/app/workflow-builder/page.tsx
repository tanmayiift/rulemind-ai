"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES, type ThemeTokens } from "../../src/v3/theme";

// Workflow Builder -- THE policy canvas.
//
// There used to be two. Build Studio (`/studio`) was written as "a single canvas-first surface
// that replaces the split Policies-step-list / Workflow-Builder editors", shipped, and the editor
// it was meant to replace was never removed. It then fell short of this one in both directions,
// so neither could be deleted. Evaluated line by line on 2026-09-24 and merged here:
//
//   FROM STUDIO, because this page could not do it at all:
//     * create a policy (+ New, POST) and rename one -- this page could only EDIT an existing
//       policy; there was no way to start one;
//     * author a rule inline and link it, without leaving the canvas;
//     * reorder with ↑/↓ buttons -- drag-and-drop is mouse-only, so reordering was impossible
//       from a keyboard;
//     * configure a review gate -- this page had NO editor for it; the queue, role, SLA and
//       required fields the executor reads were unreachable;
//     * show a referenced rule/scorecard by NAME on its node instead of its raw id;
//     * the environment on each picker option, so editing a PROD policy is a visible choice;
//     * the late-load guard: without it, "+ New" then typing is overwritten when the policy list
//       lands, and Save repoints at policies[0] -- a bug Studio had already been bitten by;
//     * resilient loading: one failed GET (e.g. /models for a role without access) no longer
//       blanks the whole page.
//
//   NOT CARRIED OVER, because it was broken:
//     * Studio's "Branch condition" wrote `config.condition` on the branch step. The executor
//       routes on `config.branches[].condition` and `config.default` (executor._execute_branch),
//       and Studio had no way to add a branch -- so a branch built there routed NOTHING. This
//       page's BranchEditor writes the shape the executor reads.
//     * Studio's loop editor had no way to add body steps, so a loop built there iterated an
//       empty body. This page's LoopEditor has one.
//
// The Policies page stays, as the per-policy view with Run and Promote; its canvas lives here.

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
type Policy = { id: string; name: string; steps?: Step[]; defaultOutcome?: string; status?: string; [k: string]: unknown };
type Ref = { id: string; name?: string; label?: string };
type Variable = { id: string; name?: string };
type MeceResult = { valid?: boolean; overlaps?: unknown[]; gaps?: unknown[]; issues?: unknown[]; message?: string };

const OPERATORS = ["==", "!=", ">", ">=", "<", "<=", "between", "in", "not_in", "regex", "exists", "!exists"];
const OUTCOMES = ["approve", "review", "reject"];

// Palette — the executor's supported step types, grouped for discoverability.
// The entity kinds a step can reference. Declared once and reused everywhere a
// `refs` map is threaded, so the palette, the Inspector and the nested editors
// cannot drift apart again -- which is how `model` ended up in the palette with
// no way to configure it.
type RefKind = "connector" | "rule" | "scorecard" | "decision_table" | "model";
type RefMap = Record<RefKind, Ref[]>;

// Grouped so it reads as "pull data → decide → control the flow → act", not a flat wall of 13
// (Studio's grouping, carried over). All 13 executor step types, which Studio never had.
type Group = "Pull" | "Decide" | "Flow" | "Act";
const GROUPS: Group[] = ["Pull", "Decide", "Flow", "Act"];
const PALETTE: Array<{ type: string; label: string; needsRef?: RefKind; group: Group; tone: keyof Pick<ThemeTokens, "accent" | "success" | "warning" | "danger" | "purple"> }> = [
  { type: "connector", label: "Connector", needsRef: "connector", group: "Pull", tone: "accent" },
  { type: "transform", label: "Transform", group: "Pull", tone: "accent" },
  { type: "rule", label: "Rule", needsRef: "rule", group: "Decide", tone: "purple" },
  { type: "decision_table", label: "Decision table", needsRef: "decision_table", group: "Decide", tone: "purple" },
  { type: "scorecard", label: "Scorecard", needsRef: "scorecard", group: "Decide", tone: "purple" },
  { type: "model", label: "ML model", needsRef: "model", group: "Decide", tone: "purple" },
  { type: "outcome", label: "Outcome", group: "Decide", tone: "success" },
  { type: "branch", label: "Branch", group: "Flow", tone: "warning" },
  { type: "loop", label: "Loop", group: "Flow", tone: "warning" },
  { type: "workflow", label: "Sub-workflow", group: "Flow", tone: "accent" },
  { type: "review_gate", label: "Review gate", group: "Flow", tone: "warning" },
  { type: "action", label: "Action", group: "Act", tone: "accent" },
  { type: "monitor", label: "Monitor", group: "Act", tone: "warning" },
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
  const [name, setName] = React.useState("");
  const [steps, setSteps] = React.useState<Step[]>([]);
  const [selected, setSelected] = React.useState<string | null>(null);
  const [refs, setRefs] = React.useState<RefMap>({ connector: [], rule: [], scorecard: [], decision_table: [], model: [] });
  const [variables, setVariables] = React.useState<Variable[]>([]);
  const [dirty, setDirty] = React.useState(false);
  const [status, setStatus] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [mece, setMece] = React.useState<MeceResult | null>(null);
  const dragIndex = React.useRef<number | null>(null);

  // Has the author done anything yet? The initial load resolves seven parallel GETs and then
  // auto-selects policies[0]. Without this guard that late `.then` overwrites whatever the author
  // started in the meantime: click "+ New", type a name, and when the list lands the picker jumps
  // to the first policy and replaces the name and steps with its own -- and `policyId` is now set,
  // so the next Save writes over THAT policy instead of creating one. Studio shipped that bug and
  // then fixed it; creating policies here would have reintroduced it without this.
  const touched = React.useRef(false);

  // Each GET degrades to [] on its own. This was a Promise.all, so one failed request -- /models
  // for a role that cannot read models, a connector list that 500s -- blanked the entire canvas
  // with "Unable to load policies." when every policy had loaded fine.
  const loadAll = React.useCallback(async () => {
    const get = async <T,>(path: string): Promise<T[]> => {
      try { return await apiJson<T[]>(apiBaseUrl, path, {}, apiKey); } catch { return []; }
    };
    const [ps, c, r, s, dt, m, v] = await Promise.all([
      get<Policy>("/api/v1/policies"), get<Ref>("/api/v1/connectors"), get<Ref>("/api/v1/rules"),
      get<Ref>("/api/v1/scorecards"), get<Ref>("/api/v1/decision-tables"), get<Ref>("/api/v1/models"),
      get<Variable>("/api/v1/variables"),
    ]);
    setPolicies(ps);
    setRefs({ connector: c, rule: r, scorecard: s, decision_table: dt, model: m });
    setVariables(v);
    return ps;
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => {
    // `?policy=<id>` opens the canvas ON that policy -- how the Policies page hands a step it
    // cannot edit to this one. Read from window rather than useSearchParams so the page does not
    // need a Suspense boundary; it is a client component and only the initial value matters.
    const requested = typeof window !== "undefined"
      ? new URLSearchParams(window.location.search).get("policy") : null;
    void loadAll().then((ps) => {
      if (touched.current) return;
      const target = (requested && ps.find((p) => p.id === requested)) || ps[0];
      if (target) selectPolicy(target.id, ps);
      if (requested && !ps.some((p) => p.id === requested)) {
        setError(`Policy "${requested}" was not found. Showing ${target ? `"${target.name}"` : "a new policy"} instead.`);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiBaseUrl, apiKey]);

  const selectPolicy = (id: string, list = policies) => {
    const p = list.find((x) => x.id === id);
    setPolicyId(id);
    setName(p?.name ?? "");
    setSteps((p?.steps ?? []).map((s) => ({ ...s, id: s.id || newId(s.type) })));
    setSelected(null);
    setDirty(false);
    setMece(null);
    setStatus(null);
  };
  const newPolicy = () => {
    touched.current = true;
    setPolicyId(""); setName(""); setSteps([]); setSelected(null);
    setDirty(false); setMece(null); setStatus(null); setError(null);
  };

  const mutate = (next: Step[]) => { touched.current = true; setSteps(next); setDirty(true); setStatus(null); };
  const addStep = (type: string) => {
    const step: Step = { id: newId(type), type, label: PALETTE.find((p) => p.type === type)?.label ?? type };
    if (type === "outcome") step.outcome = "review";
    if (type === "branch") step.config = { branches: [], default: [] };
    if (type === "loop") step.config = { over: "", as: "item", indexAs: "index", maxIterations: 1000, steps: [] };
    if (type === "review_gate") step.config = { assignTo: "", priority: "normal", slaHours: 24 };
    mutate([...steps, step]);
    setSelected(step.id);
  };
  const removeStep = (id: string) => { mutate(steps.filter((s) => s.id !== id)); if (selected === id) setSelected(null); };
  const patchStep = (id: string, patch: Partial<Step>) => mutate(steps.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  // Keyboard reorder. Drag-and-drop alone made reordering impossible without a mouse -- the
  // ↑/↓ buttons are real <button>s, so Tab + Enter moves a step.
  const move = (from: number, to: number) => {
    if (to < 0 || to >= steps.length) return;
    const next = [...steps];
    const [moved] = next.splice(from, 1);
    next.splice(to, 0, moved);
    mutate(next);
  };

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
    setError(null); setStatus(null);
    if (!name.trim()) { setError("Name the policy."); return; }
    try {
      // PUT is a FULL REPLACE assembled from PolicyUpsertRequest, whose `status` defaults to
      // "dev". Sending only {name, steps} would therefore not leave the other fields alone -- it
      // would reset them, and saving a rename on a PROD policy would silently drop it out of
      // production. Spread the loaded record.
      const existing = policyId ? policies.find((p) => p.id === policyId) : undefined;
      const body = JSON.stringify(existing ? { ...existing, name, steps } : { name, steps });
      const saved = policyId
        ? await apiJson<Policy>(apiBaseUrl, `/api/v1/policies/${policyId}`, { method: "PUT", body }, apiKey)
        : await apiJson<Policy>(apiBaseUrl, "/api/v1/policies", { method: "POST", body }, apiKey);
      const ps = await loadAll();
      selectPolicy(saved.id, ps);
      // AFTER selectPolicy, which clears `status` -- set first, the only confirmation that the
      // write landed was a flash lasting one network round trip.
      setStatus(`Saved "${saved.name}".`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    }
  };

  const validate = async () => {
    setError(null);
    if (!policyId) { setError("Save the policy before validating it."); return; }
    try {
      const res = await apiJson<MeceResult>(apiBaseUrl, `/api/v1/policies/${policyId}/analyze-mece`, { method: "POST", body: JSON.stringify({ steps }) }, apiKey);
      setMece(res);
    } catch (e) {
      setMece({ valid: false, message: e instanceof Error ? e.message : "Validation failed." });
    }
  };

  const refName = (step: Step): string | undefined => {
    const kind = PALETTE.find((p) => p.type === step.type)?.needsRef;
    if (!kind || !step.ref_id) return undefined;
    const ref = refs[kind].find((r) => r.id === step.ref_id);
    return ref ? (ref.name || ref.label || ref.id) : step.ref_id;
  };

  const selectedStep = steps.find((s) => s.id === selected) ?? null;
  const current = policies.find((p) => p.id === policyId);

  return (
    <div style={{ padding: 20, height: "100%", display: "flex", flexDirection: "column" }}>
      {/* The page heading. Visually hidden because a full-height canvas cannot spare the vertical
        * space, present because a page with no heading element has no outline and no landmark to
        * navigate to -- this file had neither. See .rm-sr-only. */}
      <h1 className="rm-sr-only">Workflow Builder</h1>
      {/* toolbar */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap", marginBottom: 16 }}>
        <select aria-label="Policy" value={policyId} onChange={(e) => { touched.current = true; selectPolicy(e.target.value); }} style={{ ...selectStyle(theme), width: "auto", minWidth: 220 }}>
          <option value="">— new policy —</option>
          {/* Each option names its environment. Every environment is listed on purpose -- hiding
              a PROD policy would make it uneditable -- but the label turns editing a live policy
              from a DEV context into a visible choice rather than an accident. */}
          {policies.map((p) => (
            <option key={p.id} value={p.id}>{p.name}{p.status ? ` (${String(p.status).toUpperCase()})` : ""}</option>
          ))}
        </select>
        <button onClick={newPolicy} style={ghostStyle(theme)}>+ New</button>
        <input aria-label="Policy name" placeholder="Policy name" value={name}
          onChange={(e) => { touched.current = true; setName(e.target.value); setDirty(true); setStatus(null); }}
          style={{ ...inputStyle(theme), width: "auto", flex: 1, minWidth: 180 }} />
        {policyId ? <a href={`/policies?policy=${encodeURIComponent(policyId)}`} style={{ fontSize: 12, color: theme.accent }} title="Run and promote this policy on the Policies page">Run / promote ↗</a> : null}
        {dirty ? <span style={{ fontSize: 12, color: theme.warning }}>Unsaved changes</span> : null}
        {status ? <span role="status" style={{ fontSize: 12, color: theme.success }}>{status}</span> : null}
        {/* Requires a SAVED policy. analyze-mece accepts posted steps so unsaved EDITS can be
            checked, but it still resolves the policy id first -- for a policy that does not exist
            yet, all this button could produce is a 404 in the validation panel. */}
        <button onClick={validate} disabled={!policyId || !steps.length}
          title={!policyId ? "Save the policy first — MECE analysis runs against a stored policy." : undefined}
          style={{ ...ghostStyle(theme), opacity: !policyId || !steps.length ? 0.5 : 1 }}>Validate (MECE)</button>
        <button onClick={save} disabled={!dirty} style={ctaStyle(theme, !dirty)}>Save</button>
      </div>
      {error ? <div role="alert" style={{ color: theme.danger, fontSize: 13, marginBottom: 10 }}>{error}</div> : null}

      <div style={{ display: "grid", gridTemplateColumns: "176px minmax(0,1fr) 320px", gap: 16, flex: 1, minHeight: 0 }}>
        {/* palette */}
        <div style={{ ...panel(theme), overflowY: "auto" }}>
          {GROUPS.map((group) => (
            <div key={group} style={{ marginBottom: 14 }}>
              <div style={{ fontSize: 10.5, textTransform: "uppercase", letterSpacing: 0.6, color: theme.dim, fontWeight: 700, marginBottom: 6 }}>{group}</div>
              <div style={{ display: "grid", gap: 5 }}>
                {PALETTE.filter((p) => p.group === group).map((p) => (
                  <button key={p.type} onClick={() => addStep(p.type)}
                    style={{ display: "flex", alignItems: "center", gap: 8, textAlign: "left", padding: "7px 10px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.card, color: theme.text, fontSize: 12.5, cursor: "pointer" }}>
                    <span style={{ width: 8, height: 8, borderRadius: 2, background: theme[p.tone] }} />
                    {p.label}
                  </button>
                ))}
              </div>
            </div>
          ))}
        </div>

        {/* canvas */}
        <div style={{ ...panel(theme), overflowY: "auto", padding: 20 }}>
          {steps.length === 0 ? (
            <div style={{ color: theme.dim, fontSize: 14, textAlign: "center", marginTop: 60 }}>
              {policyId ? "Empty workflow." : "New policy."} Add steps from the palette to build the decision flow.
              <div style={{ fontSize: 12.5, marginTop: 6 }}>Pull data → decide → set the outcome.</div>
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 0 }}>
              <FlowStart theme={theme} />
              {steps.map((step, i) => (
                <React.Fragment key={step.id}>
                  <Connector theme={theme} onDrop={() => onDrop(i)} />
                  <NodeCard
                    theme={theme} step={step} index={i} total={steps.length} selected={selected === step.id}
                    refName={refName(step)}
                    onSelect={() => setSelected(step.id)}
                    onRemove={() => removeStep(step.id)}
                    onMove={(to) => move(i, to)}
                    onDragStart={() => { dragIndex.current = i; }}
                  />
                </React.Fragment>
              ))}
              <Connector theme={theme} onDrop={() => onDrop(steps.length)} />
              <FlowEnd theme={theme} outcome={current?.defaultOutcome} />
            </div>
          )}
        </div>

        {/* inspector + validation */}
        <div style={{ display: "grid", gap: 16, gridTemplateRows: "1fr auto", minHeight: 0 }}>
          <div style={{ ...panel(theme), overflowY: "auto" }}>
            <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 1, color: theme.dim, marginBottom: 12 }}>Inspector</div>
            {selectedStep ? (
              <Inspector theme={theme} step={selectedStep} refs={refs} policies={policies} currentPolicyId={policyId}
                variables={variables} apiBaseUrl={apiBaseUrl} apiKey={apiKey}
                onRuleSaved={async () => { await loadAll(); }}
                onPatch={(patch) => patchStep(selectedStep.id, patch)} />
            ) : (
              <div style={{ color: theme.dim, fontSize: 13 }}>Select a node to edit it. Reorder with ↑ ↓, or drag a node onto a connector.</div>
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

function NodeCard({ theme, step, index, total, selected, refName, onSelect, onRemove, onMove, onDragStart }: {
  theme: ThemeTokens; step: Step; index: number; total: number; selected: boolean; refName?: string;
  onSelect: () => void; onRemove: () => void; onMove: (to: number) => void; onDragStart: () => void;
}) {
  const color = toneColor(theme, step.type);
  const branches = step.type === "branch" ? ((step.config?.branches as Array<{ label?: string }>) ?? []) : null;
  const known = PALETTE.some((p) => p.type === step.type);
  return (
    <div
      draggable
      onDragStart={onDragStart}
      onClick={onSelect}
      data-step-type={step.type}
      style={{
        width: 300, borderRadius: 10, border: `1px solid ${selected ? color : theme.border}`,
        boxShadow: selected ? `0 0 0 2px ${color}33` : "none",
        background: theme.card, cursor: "grab", overflow: "hidden",
      }}
    >
      <div style={{ height: 3, background: color }} />
      <div style={{ padding: "10px 12px", display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ fontSize: 10, color: theme.dim, fontFamily: "var(--font-mono)", minWidth: 18 }}>{index + 1}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: theme.text, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{step.label || step.type}</div>
          <div style={{ fontSize: 11, color: theme.muted, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
            <span style={{ color }}>{step.type}</span>
            {/* The referenced entity by NAME. It showed the raw ref_id, so a rule step read
                "rule · r_7f3a" -- which says nothing to the person reviewing the flow. */}
            {refName ? <span> · {refName}</span> : null}
            {step.type === "outcome" && step.outcome ? <span> · {step.outcome}</span> : null}
            {/* A step type this page has no editor for (a future type, or one added by API) is
                shown and SAVED UNCHANGED -- round-tripped, never dropped. Say so. */}
            {!known ? <span style={{ color: theme.warning }}> · not editable here</span> : null}
          </div>
        </div>
        <button aria-label={`Move step ${index + 1} up`} disabled={index === 0}
          onClick={(e) => { e.stopPropagation(); onMove(index - 1); }} style={iconButton(theme, index === 0)}>↑</button>
        <button aria-label={`Move step ${index + 1} down`} disabled={index === total - 1}
          onClick={(e) => { e.stopPropagation(); onMove(index + 1); }} style={iconButton(theme, index === total - 1)}>↓</button>
        <button aria-label={`Remove step ${index + 1}`} onClick={(e) => { e.stopPropagation(); onRemove(); }}
          style={iconButton(theme, false)}>×</button>
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

function Inspector({ theme, step, refs, policies, currentPolicyId, variables, apiBaseUrl, apiKey, onRuleSaved, onPatch }: {
  theme: ThemeTokens; step: Step; refs: RefMap; policies: Policy[]; currentPolicyId: string; variables: Variable[];
  apiBaseUrl: string; apiKey: string; onRuleSaved: () => Promise<void>; onPatch: (p: Partial<Step>) => void;
}) {
  const needsRef = PALETTE.find((p) => p.type === step.type)?.needsRef;
  const options = needsRef ? refs[needsRef] : [];
  return (
    <div style={{ display: "grid", gap: 14 }}>
      <Field theme={theme} label="Type"><div style={{ fontSize: 13, color: theme.text, fontWeight: 600 }}>{step.type}</div></Field>
      <Field theme={theme} label="Label">
        <input value={step.label ?? ""} onChange={(e) => onPatch({ label: e.target.value })} style={inputStyle(theme)} />
      </Field>
      {needsRef ? (
        <Field theme={theme} label={`${needsRef.replace(/_/g, " ")} reference`}>
          <select value={step.ref_id ?? ""} onChange={(e) => onPatch({ ref_id: e.target.value })} style={selectStyle(theme)}>
            <option value="">— select —</option>
            {options.map((o) => <option key={o.id} value={o.id}>{o.name || o.label || o.id}</option>)}
          </select>
        </Field>
      ) : null}
      {step.type === "rule" ? (
        <InlineRuleEditor theme={theme} variables={variables} apiBaseUrl={apiBaseUrl} apiKey={apiKey}
          // LINK FIRST, then refresh. This was the other way round (and was in Studio, where it
          // came from): the step's ref_id was patched only after seven GETs reloaded every list,
          // so for that whole window the step on screen was unlinked -- and Save sent it that way.
          // Patching synchronously means the step is linked the instant the rule exists; the
          // reload only fetches the new rule's NAME for the node label.
          onSaved={async (ruleId) => { onPatch({ ref_id: ruleId }); await onRuleSaved(); }} />
      ) : null}
      {(step.type === "scorecard" || step.type === "decision_table") ? (
        <a href={step.type === "scorecard" ? "/scorecards" : "/decision-tables"} style={{ fontSize: 12.5, color: theme.accent }}>
          Open the {step.type.replace("_", " ")} editor ↗
        </a>
      ) : null}
      {step.type === "outcome" ? (
        <Field theme={theme} label="Outcome">
          <select value={step.outcome ?? "review"} onChange={(e) => onPatch({ outcome: e.target.value })} style={selectStyle(theme)}>
            {OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </Field>
      ) : null}
      {step.type === "workflow" ? (
        // A picker, not a free-text id. Both editors took the child as a typed id, so a typo was
        // only discovered at decision time -- as a fail-closed "unreachable child" on a live
        // request. The current policy is excluded: a policy calling itself is the cycle the
        // executor's _visited_workflows guard exists to catch.
        <Field theme={theme} label="Sub-workflow" hint="the policy to run as a step; its outcome merges into this one">
          <select value={step.ref_id ?? ""} onChange={(e) => onPatch({ ref_id: e.target.value })} style={selectStyle(theme)}>
            <option value="">— select a policy —</option>
            {step.ref_id && !policies.some((p) => p.id === step.ref_id) ? (
              <option value={step.ref_id}>{step.ref_id} (not found — alias or deleted?)</option>
            ) : null}
            {policies.filter((p) => p.id !== currentPolicyId).map((p) => (
              <option key={p.id} value={p.id}>{p.name}{p.status ? ` (${String(p.status).toUpperCase()})` : ""}</option>
            ))}
          </select>
        </Field>
      ) : null}
      {step.type === "review_gate" ? (
        <ReviewGateEditor theme={theme} config={(step.config as ReviewGateConfig) ?? {}}
          onChange={(cfg) => onPatch({ config: cfg as unknown as Record<string, unknown> })} />
      ) : null}
      {(step.type === "action" || step.type === "monitor" || step.type === "transform") ? (
        <Field theme={theme} label="Config (JSON)" hint={step.type === "transform" ? "mapping / outputKey" : "request / webhook / schedule"}>
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

// ---- review gate editor -------------------------------------------------------
//
// This page had NO editor for a review gate -- it was not even in the JSON-config list -- so the
// queue a gate assigns to, the role allowed to decide it, its SLA and the fields a reviewer must
// fill were unreachable from the canvas. Studio exposed only `assignTo`. These are exactly the
// keys executor._execute_review_gate reads; anything else here would be a field that looks
// configured and does nothing, which is how Studio's branch editor failed.

type ReviewGateConfig = {
  assignTo?: string; role?: string; priority?: string; slaHours?: number; timeoutHours?: number;
  requiredFields?: string[]; condition?: string;
};

function ReviewGateEditor({ theme, config, onChange }: { theme: ThemeTokens; config: ReviewGateConfig; onChange: (c: ReviewGateConfig) => void }) {
  const num = (v: string): number | undefined => (v.trim() === "" ? undefined : Number(v));
  return (
    <div style={{ display: "grid", gap: 10 }}>
      <div style={{ fontSize: 12, color: theme.muted, lineHeight: 1.5 }}>
        Pauses the decision and creates a task for a human. The execution resumes when the task is decided.
      </div>
      <Field theme={theme} label="Assign to queue">
        <input value={config.assignTo ?? ""} onChange={(e) => onChange({ ...config, assignTo: e.target.value })} placeholder="underwriting_queue" style={inputStyle(theme)} />
      </Field>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <Field theme={theme} label="Role allowed to decide">
          <input value={config.role ?? ""} onChange={(e) => onChange({ ...config, role: e.target.value || undefined })} placeholder="underwriter" style={inputStyle(theme)} />
        </Field>
        <Field theme={theme} label="Priority">
          <select value={config.priority ?? "normal"} onChange={(e) => onChange({ ...config, priority: e.target.value })} style={selectStyle(theme)}>
            {["low", "normal", "high", "urgent"].map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
        </Field>
        <Field theme={theme} label="SLA (hours)">
          <input type="number" min={0} value={config.slaHours ?? ""} onChange={(e) => onChange({ ...config, slaHours: num(e.target.value) })} style={inputStyle(theme)} />
        </Field>
        <Field theme={theme} label="Timeout (hours)" hint="then the gate's timeout path runs">
          <input type="number" min={0} value={config.timeoutHours ?? ""} onChange={(e) => onChange({ ...config, timeoutHours: num(e.target.value) })} style={inputStyle(theme)} />
        </Field>
      </div>
      <Field theme={theme} label="Fields the reviewer must fill" hint="comma-separated">
        <input value={(config.requiredFields ?? []).join(", ")}
          onChange={(e) => onChange({ ...config, requiredFields: e.target.value.split(",").map((x) => x.trim()).filter(Boolean) })}
          placeholder="decision_reason, verified_income" style={inputStyle(theme)} />
      </Field>
      <Field theme={theme} label="Only pause when (optional)" hint="Python expression; empty = always pause">
        <input value={config.condition ?? ""} onChange={(e) => onChange({ ...config, condition: e.target.value || undefined })}
          placeholder="variables['loan_amount'] > 500000" style={{ ...inputStyle(theme), fontFamily: "var(--font-mono)", fontSize: 12 }} />
      </Field>
    </div>
  );
}

// ---- inline rule authoring (from Build Studio) -----------------------------------
//
// Conditions joined by AND/OR → outcome, saved to /rules and linked to the node, without leaving
// the canvas. The one thing Studio could do that this page could not.

function InlineRuleEditor({ theme, variables, apiBaseUrl, apiKey, onSaved }: {
  theme: ThemeTokens; variables: Variable[]; apiBaseUrl: string; apiKey: string; onSaved: (ruleId: string) => Promise<void>;
}) {
  const [ruleName, setRuleName] = React.useState("");
  const [logic, setLogic] = React.useState<"AND" | "OR">("AND");
  const [outcome, setOutcome] = React.useState("approve");
  const [conds, setConds] = React.useState<Array<{ variable: string; operator: string; value: string }>>([{ variable: "", operator: ">=", value: "" }]);
  const [busy, setBusy] = React.useState(false);
  const [msg, setMsg] = React.useState<string | null>(null);

  const setC = (i: number, p: Partial<{ variable: string; operator: string; value: string }>) =>
    setConds((c) => c.map((x, j) => (j === i ? { ...x, ...p } : x)));

  const create = async () => {
    const usable = conds.filter((c) => c.variable);
    if (!usable.length) { setMsg("Pick a variable for at least one condition."); return; }
    setBusy(true); setMsg(null);
    try {
      // The flat node list the rule engine expects: condition, logic, condition, ..., outcome.
      const nodes: Array<Record<string, unknown>> = [];
      usable.forEach((c, i) => {
        if (i > 0) nodes.push({ id: `l${i}`, type: logic.toLowerCase(), label: logic });
        nodes.push({ id: `c${i}`, type: "condition", variable: c.variable, operator: c.operator, value: c.value });
      });
      nodes.push({ id: "out", type: outcome, label: outcome });
      const saved = await apiJson<{ id: string }>(apiBaseUrl, "/api/v1/rules",
        { method: "POST", body: JSON.stringify({ name: ruleName || "Inline rule", nodes }) }, apiKey);
      // The confirmation comes AFTER the link, not before. It used to be set first, so the page
      // said "linked" during the window in which the step was not -- and a user who believed it
      // and clicked Save stored a rule step pointing at nothing. Caught by the e2e test, which
      // clicks Save the moment the message appears, exactly as a person would.
      await onSaved(saved.id);
      setMsg("Rule created and linked.");
    } catch (e) {
      setMsg(e instanceof Error ? e.message : "Could not create rule.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ borderTop: `1px dashed ${theme.border}`, paddingTop: 12, display: "grid", gap: 8 }}>
      <div style={{ fontSize: 11, fontWeight: 700, color: theme.dim }}>OR AUTHOR A NEW RULE HERE</div>
      <input aria-label="New rule name" value={ruleName} onChange={(e) => setRuleName(e.target.value)} placeholder="Rule name" style={{ ...inputStyle(theme), fontSize: 12 }} />
      <div style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 12, color: theme.muted }}>
        Match
        <select aria-label="Condition logic" value={logic} onChange={(e) => setLogic(e.target.value as "AND" | "OR")} style={{ ...selectStyle(theme), width: 74, fontSize: 12 }}>
          <option>AND</option><option>OR</option>
        </select>
        of:
      </div>
      {conds.map((c, i) => (
        <div key={i} style={{ display: "flex", gap: 4 }}>
          <select aria-label={`Condition ${i + 1} variable`} value={c.variable} onChange={(e) => setC(i, { variable: e.target.value })} style={{ ...selectStyle(theme), flex: 1, fontSize: 12, minWidth: 0 }}>
            <option value="">variable…</option>
            {variables.map((v) => <option key={v.id} value={v.id}>{v.name || v.id}</option>)}
          </select>
          <select aria-label={`Condition ${i + 1} operator`} value={c.operator} onChange={(e) => setC(i, { operator: e.target.value })} style={{ ...selectStyle(theme), width: 70, fontSize: 12 }}>
            {OPERATORS.map((o) => <option key={o} value={o}>{o}</option>)}
          </select>
          <input aria-label={`Condition ${i + 1} value`} value={c.value} onChange={(e) => setC(i, { value: e.target.value })} placeholder="value" style={{ ...inputStyle(theme), width: 64, fontSize: 12 }} />
          <button aria-label={`Remove condition ${i + 1}`} onClick={() => setConds((cs) => cs.filter((_, j) => j !== i))} style={iconButton(theme, false)}>×</button>
        </div>
      ))}
      <button onClick={() => setConds((c) => [...c, { variable: "", operator: ">=", value: "" }])} style={{ ...ghostStyle(theme), fontSize: 12 }}>+ condition</button>
      <div style={{ display: "flex", gap: 6, alignItems: "center", fontSize: 12, color: theme.muted }}>
        then
        <select aria-label="Rule outcome" value={outcome} onChange={(e) => setOutcome(e.target.value)} style={{ ...selectStyle(theme), width: 110, fontSize: 12 }}>
          {OUTCOMES.map((o) => <option key={o} value={o}>{o}</option>)}
        </select>
      </div>
      <button onClick={create} disabled={busy} style={ctaStyle(theme, busy)}>{busy ? "Creating…" : "Create & link rule"}</button>
      {msg ? <div role="status" style={{ fontSize: 12, color: theme.text }}>{msg}</div> : null}
    </div>
  );
}

// ---- loop editor (iterate a collection variable over a body of steps) ----

type LoopConfig = { over?: string; as?: string; indexAs?: string; maxIterations?: number; mode?: string; concurrency?: number; steps?: Step[] };

function LoopEditor({ theme, refs, config, onChange }: { theme: ThemeTokens; refs: RefMap; config: LoopConfig; onChange: (c: LoopConfig) => void }) {
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
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8, alignItems: "end" }}>
        <Field theme={theme} label="Execution">
          <select value={config.mode ?? "sequential"} onChange={(e) => onChange({ ...config, mode: e.target.value })} style={{ ...inputStyle(theme), fontSize: 12 }}>
            <option value="sequential">Sequential (one at a time)</option>
            <option value="parallel">Parallel (concurrent map)</option>
          </select>
        </Field>
        {config.mode === "parallel" ? (
          <Field theme={theme} label="Max concurrency">
            <input type="number" min={1} max={32} value={config.concurrency ?? 8} onChange={(e) => onChange({ ...config, concurrency: Number(e.target.value) })} style={{ ...inputStyle(theme), fontSize: 12 }} />
          </Field>
        ) : <div />}
      </div>
      {config.mode === "parallel" ? (
        <div style={{ fontSize: 11, color: theme.muted, lineHeight: 1.5 }}>
          Iterations run concurrently on isolated contexts — the latency win for I/O-heavy bodies (a connector call per item). Iterations must be independent; review gates aren&apos;t supported inside a parallel loop.
        </div>
      ) : null}
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

function BranchEditor({ theme, refs, config, onChange }: { theme: ThemeTokens; refs: RefMap; config: BranchConfig; onChange: (c: BranchConfig) => void }) {
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

function StepRows({ theme, refs, steps, onChange }: { theme: ThemeTokens; refs: RefMap; steps: Step[]; onChange: (s: Step[]) => void }) {
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
function iconButton(theme: ThemeTokens, disabled: boolean): React.CSSProperties {
  return { border: "none", background: "transparent", color: disabled ? theme.border : theme.dim, cursor: disabled ? "default" : "pointer", fontSize: 15, lineHeight: 1, padding: "2px 3px" };
}
