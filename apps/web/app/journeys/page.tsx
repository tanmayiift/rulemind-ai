"use client";

/**
 * Journeys (parity P15) — the screens a customer fills in, and the policy that decides them.
 *
 * The screen shape predates this page: `app/experience_studio.py` has served it at
 * /sdk/v1/experience-manifest since the mobile studio shipped, and the Flutter example renders
 * it. What is new is that a workspace can author its own instead of everyone seeing the same
 * three demos.
 *
 * LOCK is the idea this page has to teach, because it is not the dev/uat/prod lifecycle sitting
 * next to it. Locked means "people are filling this in right now, so the screens must not move
 * under them" — a locked journey still promotes, and duplicating is the way to change one.
 */

import * as React from "react";
import { Copy, Lock, Plus, Trash2, Unlock, Smartphone } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type Screen = { id: string; type?: string; title?: string; subtitle?: string; fields?: unknown[] };
type Journey = {
  id: string; title: string; category?: string | null; description?: string | null;
  policy_id?: string | null; primary_color?: string | null;
  screens: Screen[]; step_count: number;
  is_locked: boolean; is_active: boolean; status: string; version: number;
  updated_at?: string;
};
type Policy = { id: string; name?: string; status?: string };

const SCREEN_TYPES = ["landing", "form", "review", "result", "callback"];

function emptyJourney(): Journey {
  return {
    id: "", title: "", category: "", description: "", policy_id: "", primary_color: "#496FBE",
    screens: [{ id: "landing", type: "landing", title: "Welcome" }],
    step_count: 1, is_locked: false, is_active: true, status: "dev", version: 1,
  };
}

export default function JourneysPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [journeys, setJourneys] = React.useState<Journey[]>([]);
  const [policies, setPolicies] = React.useState<Policy[]>([]);
  const [draft, setDraft] = React.useState<Journey>(emptyJourney());
  const [error, setError] = React.useState<string | null>(null);
  const [status, setStatus] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const load = React.useCallback(async () => {
    try {
      const [j, p] = await Promise.all([
        apiJson<Journey[]>(apiBaseUrl, "/api/v1/journeys", {}, apiKey),
        apiJson<Policy[]>(apiBaseUrl, "/api/v1/policies", {}, apiKey).catch(() => []),
      ]);
      setJourneys(j); setPolicies(p); setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load journeys.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void load(); }, [load]);

  const patch = (p: Partial<Journey>) => setDraft((d) => ({ ...d, ...p }));

  const call = React.useCallback(async (path: string, method: string, body?: unknown) => {
    setBusy(true); setError(null);
    try {
      const result = await apiJson<Journey>(apiBaseUrl, path, {
        method, ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }, apiKey);
      await load();
      return result;
    } catch (e) {
      // A 409 from lock is not a failure the user caused by accident — the API's message names
      // the way forward (duplicate it), so it is shown verbatim rather than replaced.
      setError(e instanceof Error ? e.message : "That did not work.");
      return null;
    } finally {
      setBusy(false);
    }
  }, [apiBaseUrl, apiKey, load]);

  const save = async () => {
    const body = {
      title: draft.title, category: draft.category, description: draft.description,
      policy_id: draft.policy_id, primary_color: draft.primary_color,
      screens: draft.screens, is_active: draft.is_active, status: draft.status,
    };
    const saved = draft.id
      ? await call(`/api/v1/journeys/${draft.id}`, "PUT", body)
      : await call("/api/v1/journeys", "POST", body);
    if (saved) { setDraft(saved); setStatus(draft.id ? "Journey saved." : "Journey created."); }
  };

  const setScreen = (index: number, p: Partial<Screen>) =>
    patch({ screens: draft.screens.map((s, i) => (i === index ? { ...s, ...p } : s)) });

  const addScreen = () => patch({
    screens: [...draft.screens, { id: `screen_${draft.screens.length + 1}`, type: "form", title: "" }],
  });

  const removeScreen = (index: number) =>
    patch({ screens: draft.screens.filter((_, i) => i !== index) });

  const statusTone = (j: Journey) =>
    j.status === "prod" ? "success" : j.status === "uat" ? "warning" : "neutral";

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="Journeys"
        subtitle="The screens a customer fills in, and the policy that decides what they submit."
        actions={<Button onClick={() => { setDraft(emptyJourney()); setStatus(null); }} data-testid="journey-new"><Plus size={15} /> New journey</Button>}
      />

      {error ? <Card><div style={{ color: "var(--rm-danger)", fontSize: 13 }} data-testid="journey-error">{error}</div></Card> : null}
      {status ? <Card><div style={{ color: "var(--rm-success)", fontSize: 13 }}>{status}</div></Card> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 1fr) 320px", gap: 20, alignItems: "start" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
          <Card>
            {draft.is_locked ? (
              // Said before the fields, not after a failed save. A form that looks editable and
              // then refuses is worse than one that says up front what it will not accept.
              <div style={{ marginBottom: 14, padding: 10, borderRadius: 8, background: "var(--rm-warning-bg, transparent)", border: "1px solid var(--rm-warning)", fontSize: 12.5 }} data-testid="journey-locked-banner">
                <Lock size={13} style={{ verticalAlign: "-2px" }} /> This journey is locked because
                it is in the field. Duplicate it to make changes, or unlock it if nobody is
                filling it in.
              </div>
            ) : null}

            <div style={{ display: "grid", gridTemplateColumns: "1fr 200px", gap: 12 }}>
              <Field label="Title"><Input value={draft.title} onChange={(e) => patch({ title: e.target.value })} placeholder="e.g. Instant Personal Loan" data-testid="journey-title" /></Field>
              <Field label="Category"><Input value={draft.category ?? ""} onChange={(e) => patch({ category: e.target.value })} placeholder="Lending" /></Field>
            </div>
            <div style={{ height: 10 }} />
            <div style={{ display: "grid", gridTemplateColumns: "1fr 160px 140px", gap: 12 }}>
              <Field label="Decided by">
                <Select value={draft.policy_id ?? ""} onChange={(e) => patch({ policy_id: e.target.value })} data-testid="journey-policy">
                  <option value="">Choose a policy…</option>
                  {policies.map((p) => <option key={p.id} value={p.id}>{(p.name || p.id) + (p.status ? ` · ${p.status}` : "")}</option>)}
                </Select>
              </Field>
              <Field label="Stage">
                <Select value={draft.status} onChange={(e) => patch({ status: e.target.value })}>
                  {["dev", "uat", "prod"].map((s) => <option key={s} value={s}>{s}</option>)}
                </Select>
              </Field>
              <Field label="Accent">
                <Input value={draft.primary_color ?? ""} onChange={(e) => patch({ primary_color: e.target.value })} placeholder="#496FBE" />
              </Field>
            </div>
          </Card>

          <Card>
            <SectionTitle right={<Button variant="secondary" onClick={addScreen} disabled={draft.is_locked} data-testid="journey-add-screen"><Plus size={14} /> Screen</Button>}>
              Screens
            </SectionTitle>
            <div style={{ fontSize: 12.5, color: "var(--rm-dim)", margin: "2px 0 12px" }}>
              In order. Screen {draft.screens.length ? 1 : 0} is what opens on the device.
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {draft.screens.map((screen, index) => (
                <div key={index} style={{ display: "grid", gridTemplateColumns: "28px 1fr 150px 1fr 32px", gap: 10, alignItems: "center" }}>
                  <span style={{ fontSize: 12, color: "var(--rm-dim)", fontFamily: "var(--font-mono)" }}>{index + 1}</span>
                  <Input value={screen.id} onChange={(e) => setScreen(index, { id: e.target.value })} placeholder="screen_id" disabled={draft.is_locked} />
                  <Select value={screen.type ?? "form"} onChange={(e) => setScreen(index, { type: e.target.value })} disabled={draft.is_locked}>
                    {SCREEN_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
                  </Select>
                  <Input value={screen.title ?? ""} onChange={(e) => setScreen(index, { title: e.target.value })} placeholder="Heading shown to the customer" disabled={draft.is_locked} />
                  <Button variant="secondary" onClick={() => removeScreen(index)} disabled={draft.is_locked}><Trash2 size={13} /></Button>
                </div>
              ))}
            </div>
            <div style={{ marginTop: 16, display: "flex", gap: 8, alignItems: "center" }}>
              <Button onClick={() => void save()} disabled={busy || !draft.title} data-testid="journey-save">
                {draft.id ? "Save changes" : "Create journey"}
              </Button>
              {draft.id ? (
                <>
                  <Button variant="secondary" disabled={busy} data-testid="journey-lock"
                    onClick={async () => {
                      const next = await call(`/api/v1/journeys/${draft.id}/${draft.is_locked ? "unlock" : "lock"}`, "POST");
                      if (next) { setDraft(next); setStatus(next.is_locked ? "Locked — screens are frozen for people in the flow." : "Unlocked."); }
                    }}>
                    {draft.is_locked ? <><Unlock size={14} /> Unlock</> : <><Lock size={14} /> Lock</>}
                  </Button>
                  <Button variant="secondary" disabled={busy} data-testid="journey-duplicate"
                    onClick={async () => {
                      const next = await call(`/api/v1/journeys/${draft.id}/duplicate`, "POST", {});
                      if (next) { setDraft(next); setStatus(`Duplicated as “${next.title}” — unlocked and back in dev.`); }
                    }}>
                    <Copy size={14} /> Duplicate
                  </Button>
                </>
              ) : null}
              {draft.id ? <span style={{ fontSize: 12, color: "var(--rm-dim)", marginLeft: "auto" }}>v{draft.version}</span> : null}
            </div>
          </Card>
        </div>

        <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
          <Card>
            <SectionTitle>Saved journeys</SectionTitle>
            {journeys.length === 0 ? (
              <EmptyState icon={<Smartphone size={20} />} title="No journeys yet"
                hint="Until you author one, devices get the built-in demo flows." />
            ) : (
              <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                {journeys.map((j) => (
                  <button key={j.id} onClick={() => { setDraft(j); setStatus(null); }}
                    data-testid={`journey-row-${j.id}`}
                    style={{ textAlign: "left", border: "1px solid var(--rm-border)", background: draft.id === j.id ? "var(--rm-accent-bg)" : "transparent", borderRadius: 10, padding: "10px 12px", cursor: "pointer" }}>
                    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                      <span style={{ fontSize: 13.5, fontWeight: 600, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.title}</span>
                      {j.is_locked ? <Lock size={12} style={{ color: "var(--rm-warning)" }} /> : null}
                      <Badge tone={statusTone(j)}>{j.status}</Badge>
                    </div>
                    <div style={{ fontSize: 11.5, color: "var(--rm-dim)", marginTop: 3 }}>
                      {j.step_count} screen{j.step_count === 1 ? "" : "s"} · v{j.version}
                      {j.is_active ? "" : " · retired"}
                    </div>
                  </button>
                ))}
              </div>
            )}
          </Card>

          <Card>
            <SectionTitle>On the device</SectionTitle>
            <div style={{ fontSize: 12.5, color: "var(--rm-dim)", lineHeight: 1.55 }}>
              Active journeys ship in the SDK’s experience manifest, so an app picks them up on its
              next sync — no release needed. A workspace with none authored gets the built-in demo
              flows instead.
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
