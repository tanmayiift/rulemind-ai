"use client";

import * as React from "react";
import Link from "next/link";
import { Check, ArrowRight, Sparkles, Rocket } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Badge, PageHeader, SectionTitle } from "../../src/v3/ui";

type Step = { key: string; label: string; href: string; hint: string; count: number; done: boolean };
type Activation = { steps: Step[]; completed: number; total: number; activated: boolean; has_data: boolean };

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
