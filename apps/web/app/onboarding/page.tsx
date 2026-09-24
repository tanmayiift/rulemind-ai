"use client";

import * as React from "react";
import Link from "next/link";
import { Check, ArrowRight, Sparkles, Rocket, Copy } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { humanMessage } from "../../src/lib/errors";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Badge, Dialog, Input, PageHeader, SectionTitle } from "../../src/v3/ui";

type Step = { key: string; label: string; href: string; hint: string; count: number; done: boolean };
type Activation = { steps: Step[]; completed: number; total: number; activated: boolean; has_data: boolean };

type SetupStatus = {
  steps: { org: boolean; dev_key: boolean; verified: boolean; ai: boolean; prod_key: boolean };
  decision_count: number;
  complete: boolean;
};

/**
 * The workspace's own setup (plan 6.6): the backend state machine -- org, dev key, verified
 * integration, AI choice, production key -- that had no screen. The production key is issued only
 * once a decision has been made with the dev key, and is shown exactly once.
 */
function WorkspaceSetup() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [status, setStatus] = React.useState<SetupStatus | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState<string | null>(null);
  const [prodKey, setProdKey] = React.useState<string | null>(null);

  const call = React.useCallback(async <T,>(path: string, init: RequestInit = {}) =>
    apiJson<T>(apiBaseUrl, path, init, apiKey), [apiBaseUrl, apiKey]);
  const refresh = React.useCallback(async () => {
    try { setStatus(await call<SetupStatus>("/api/v1/onboarding/status")); }
    catch (e) { setError(humanMessage(e, "Could not load setup status.")); }
  }, [call]);
  React.useEffect(() => { void refresh(); }, [refresh]);

  const act = async (key: string, run: () => Promise<unknown>) => {
    setBusy(key); setError(null);
    try { await run(); await refresh(); }
    catch (e) { setError(humanMessage(e, "That step failed.")); }
    finally { setBusy(null); }
  };

  const testDecision = () => act("decide", async () => {
    const policies = await call<Array<{ id: string; status: string }>>("/api/v1/policies");
    const policy = policies.find((p) => p.status === "prod") ?? policies[0];
    if (!policy) throw new Error("There is no policy to decide with yet — load the sample data below, or build one.");
    await call("/api/v1/decide", { method: "POST", body: JSON.stringify({ policy_id: policy.id, payload: {} }) });
    await call("/api/v1/onboarding/verify", { method: "POST" });
  });
  const chooseAi = (optedIn: boolean) => act("ai", () =>
    call("/api/v1/onboarding/ai", { method: "POST", body: JSON.stringify({ opted_in: optedIn }) }));
  const issueProdKey = () => act("prod", async () => {
    const issued = await call<{ api_key: string }>("/api/v1/onboarding/request-prod", { method: "POST" });
    setProdKey(issued.api_key);
  });

  if (!status) return error ? <Card><div role="alert" style={{ color: "var(--rm-danger)", fontSize: 13 }}>{error}</div></Card> : null;
  const rows: Array<{ key: keyof SetupStatus["steps"]; label: string; detail: string; action?: React.ReactNode }> = [
    { key: "org", label: "Workspace created", detail: "Your organisation and its owner." },
    { key: "dev_key", label: "Development key issued", detail: "Shown once at signup. Keys are managed under Access & Roles." },
    { key: "verified", label: "Integration verified", detail: status.steps.verified
        ? status.decision_count + " decision(s) made in this workspace."
        : "Make one decision — from your integration with the dev key, or right here.",
      action: status.steps.verified ? null : (
        <Button onClick={testDecision} disabled={busy !== null} data-testid="setup-test-decision">
          {busy === "decide" ? "Deciding…" : "Make a test decision"}
        </Button>) },
    { key: "ai", label: "AI assistance decided", detail: "AI features stay off until a provider key is configured, whatever you choose here.",
      action: status.steps.ai ? null : (
        <span style={{ display: "flex", gap: 8 }}>
          <Button variant="secondary" onClick={() => chooseAi(true)} disabled={busy !== null}>Use AI</Button>
          <Button variant="ghost" onClick={() => chooseAi(false)} disabled={busy !== null} data-testid="setup-ai-decline">Not now</Button>
        </span>) },
    { key: "prod_key", label: "Production key issued", detail: status.steps.verified
        ? "Issued once. Store it in your secrets manager."
        : "Available after the integration is verified.",
      action: status.steps.prod_key ? null : (
        <Button onClick={issueProdKey} disabled={busy !== null || !status.steps.verified} data-testid="setup-prod-key">
          {busy === "prod" ? "Issuing…" : "Issue production key"}
        </Button>) },
  ];

  return (
    <Card>
      <SectionTitle right={<Badge tone={status.complete ? "success" : "neutral"}>{status.complete ? "complete" : "in progress"}</Badge>}>
        Workspace setup
      </SectionTitle>
      <ol style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 8 }} data-testid="workspace-setup">
        {rows.map((row) => {
          const done = status.steps[row.key];
          return (
            <li key={row.key} data-step={row.key} data-done={done}
                style={{ display: "flex", alignItems: "center", gap: 12, padding: "10px 12px", borderRadius: 10,
                         border: "1px solid var(--rm-border)", background: done ? "var(--rm-success-bg)" : "transparent" }}>
              <span aria-hidden style={{ width: 24, height: 24, borderRadius: 999, display: "grid", placeItems: "center", flexShrink: 0,
                                         background: done ? "var(--rm-success)" : "var(--rm-border)", color: done ? "var(--rm-inverse-text)" : "var(--rm-muted)" }}>
                {done ? <Check size={14} /> : null}
              </span>
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13.5, fontWeight: 600, color: "var(--rm-text)" }}>
                  {row.label}<span className="rm-sr-only">{done ? " (done)" : " (to do)"}</span>
                </div>
                <div style={{ fontSize: 12.5, color: "var(--rm-muted)" }}>{row.detail}</div>
              </div>
              {row.action ?? null}
            </li>
          );
        })}
      </ol>
      {error ? <div role="alert" style={{ marginTop: 10, color: "var(--rm-danger)", fontSize: 12.5 }}>{error}</div> : null}
      <Dialog open={prodKey !== null} title="Your production API key" onClose={() => setProdKey(null)}
              footer={<Button onClick={() => setProdKey(null)}>I have stored it</Button>}>
        <div>This is the only time this key is shown. Store it in your secrets manager; revoke and reissue it under Access &amp; Roles if it is lost.</div>
        <div style={{ display: "flex", gap: 8 }}>
          <Input mono readOnly value={prodKey ?? ""} aria-label="Production API key" data-testid="setup-prod-key-value"
                 onFocus={(e) => e.currentTarget.select()} />
          <Button variant="secondary" aria-label="Copy the key" onClick={() => { void navigator.clipboard?.writeText(prodKey ?? ""); }}><Copy size={14} /></Button>
        </div>
      </Dialog>
    </Card>
  );
}

export default function OnboardingPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [act, setAct] = React.useState<Activation | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [loadingSamples, setLoadingSamples] = React.useState(false);

  const load = React.useCallback(async () => {
    try { setAct(await apiJson<Activation>(apiBaseUrl, "/api/v1/onboarding/activation", {}, apiKey)); setError(null); }
    catch (e) { setError(e instanceof Error ? e.message : "Unable to load onboarding."); }
  }, [apiBaseUrl, apiKey]);
  React.useEffect(() => { void load(); }, [load]);

  const loadSamples = async () => {
    setLoadingSamples(true);
    try { setAct(await apiJson<Activation>(apiBaseUrl, "/api/v1/onboarding/load-samples", { method: "POST" }, apiKey)); }
    catch (e) { setError(e instanceof Error ? e.message : "Could not load samples."); }
    finally { setLoadingSamples(false); }
  };

  const pct = act ? Math.round((act.completed / act.total) * 100) : 0;
  const nextStep = act?.steps.find((s) => !s.done);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20, maxWidth: 860 }}>
      <PageHeader title="Get started" subtitle="Build your first decision flow end to end. Each step unlocks the next — you'll have a working policy and a live decision in minutes." />

      <WorkspaceSetup />

      {error ? <Card><div style={{ color: "var(--rm-danger)", fontSize: 13 }}>{error}</div></Card> : null}

      {act ? (
        <>
          <Card>
            <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: 16, flexWrap: "wrap" }}>
              <div>
                <div style={{ fontSize: 15, fontWeight: 700, color: "var(--rm-text)" }}>
                  {act.activated ? "🎉 You're activated — every step is done." : `Activation ${act.completed}/${act.total}`}
                </div>
                {!act.activated && nextStep ? <div style={{ fontSize: 13, color: "var(--rm-muted)", marginTop: 4 }}>Next: {nextStep.label}</div> : null}
              </div>
              {!act.has_data ? (
                <Button variant="secondary" onClick={loadSamples} disabled={loadingSamples}>
                  <Sparkles size={15} /> {loadingSamples ? "Loading…" : "Load sample data to explore"}
                </Button>
              ) : null}
            </div>
            <div style={{ marginTop: 12, height: 8, borderRadius: 999, background: "var(--rm-border)", overflow: "hidden" }}>
              <div style={{ width: `${pct}%`, height: "100%", background: "var(--rm-accent)", transition: "width .3s" }} />
            </div>
          </Card>

          <Card>
            <SectionTitle>Your first decision flow</SectionTitle>
            <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              {act.steps.map((s, i) => {
                const isNext = s.key === nextStep?.key;
                return (
                  <div key={s.key} style={{ display: "flex", alignItems: "center", gap: 14, padding: "12px 14px", borderRadius: 10,
                    border: "1px solid " + (isNext ? "var(--rm-accent)" : "var(--rm-border)"),
                    background: s.done ? "var(--rm-success-bg)" : isNext ? "var(--rm-accent-bg)" : "transparent" }}>
                    <span style={{ width: 28, height: 28, borderRadius: 999, flexShrink: 0, display: "grid", placeItems: "center",
                      background: s.done ? "var(--rm-success)" : "var(--rm-border)", color: s.done ? "var(--rm-inverse-text)" : "var(--rm-muted)", fontWeight: 700, fontSize: 13 }}>
                      {s.done ? <Check size={16} /> : i + 1}
                    </span>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 14, fontWeight: 600, color: "var(--rm-text)", display: "flex", gap: 8, alignItems: "center" }}>
                        {s.label}
                        {s.done ? <Badge tone="success">done · {s.count}</Badge> : null}
                      </div>
                      <div style={{ fontSize: 12.5, color: "var(--rm-muted)", marginTop: 2 }}>{s.hint}</div>
                    </div>
                    <Link href={s.href as never} style={{ textDecoration: "none" }}>
                      <Button variant={isNext ? "primary" : "secondary"}>{s.done ? "Open" : "Start"} <ArrowRight size={14} /></Button>
                    </Link>
                  </div>
                );
              })}
            </div>
          </Card>

          {act.activated ? (
            <Card>
              <div style={{ display: "flex", gap: 12, alignItems: "center" }}>
                <Rocket size={20} color="var(--rm-accent)" />
                <div style={{ fontSize: 13.5, color: "var(--rm-text)" }}>
                  Nicely done. Promote your policy through Dev → UAT → Prod on the <Link href={"/deploy" as never} style={{ color: "var(--rm-accent)" }}>Deploy</Link> screen, then wire your API key into your app.
                </div>
              </div>
            </Card>
          ) : null}
        </>
      ) : null}
    </div>
  );
}
