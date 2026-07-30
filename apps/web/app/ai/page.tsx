"use client";

import * as React from "react";
import { Sparkles, KeyRound, Wand2, ShieldCheck } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Badge, PageHeader, SectionTitle } from "../../src/v3/ui";

type MaskedConfig = {
  default_provider: string;
  providers: Record<string, { configured: boolean; model: string }>;
};
type GenResult = {
  in_scope: boolean;
  message?: string;
  reason?: string;
  provider?: string;
  valid?: boolean;
  validation_error?: string | null;
  draft?: { name?: string; tree?: unknown; steps?: Array<Record<string, unknown>> };
};

const PROVIDER_LABEL: Record<string, string> = { anthropic: "Anthropic (Claude)", openai: "OpenAI (GPT)" };
const DEFAULT_MODEL: Record<string, string> = { anthropic: "claude-sonnet-5", openai: "gpt-4o" };

export default function AICopilotPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();

  const [config, setConfig] = React.useState<MaskedConfig | null>(null);
  const [provider, setProvider] = React.useState("anthropic");
  const [key, setKey] = React.useState("");
  const [model, setModel] = React.useState("");
  const [modelList, setModelList] = React.useState<{ models: string[]; default: string; live: boolean }>({ models: [], default: "", live: false });
  const [modelsBusy, setModelsBusy] = React.useState(false);
  const [savedMsg, setSavedMsg] = React.useState<string | null>(null);
  const [testMsg, setTestMsg] = React.useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = React.useState(false);

  const [genMode, setGenMode] = React.useState<"rule" | "policy">("rule");
  const [prompt, setPrompt] = React.useState("Approve when the bureau score is at least 720 and DTI ratio is below 0.4, otherwise send to review.");
  const [gen, setGen] = React.useState<GenResult | null>(null);
  const [generating, setGenerating] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const loadConfig = React.useCallback(async () => {
    try {
      const c = await apiJson<MaskedConfig>(apiBaseUrl, "/api/v1/ai/config", {}, apiKey);
      setConfig(c);
      setProvider(c.default_provider || "anthropic");
      setModel(c.providers?.[c.default_provider || "anthropic"]?.model || "");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load AI config.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void loadConfig(); }, [loadConfig]);

  // Load the selectable models for the current provider (live from the provider's
  // /models API when a key is set, else curated) whenever the provider changes.
  const loadModels = React.useCallback(async (prov: string) => {
    setModelsBusy(true);
    try {
      const r = await apiJson<{ models: string[]; default: string; live: boolean }>(apiBaseUrl, `/api/v1/ai/models?provider=${prov}`, {}, apiKey);
      setModelList(r);
      setModel((m) => (m && r.models.includes(m) ? m : (r.default || r.models[0] || "")));
    } catch { /* keep whatever we have */ }
    finally { setModelsBusy(false); }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void loadModels(provider); }, [loadModels, provider]);

  const saveKey = async () => {
    setBusy(true); setSavedMsg(null); setTestMsg(null); setError(null);
    try {
      const payload: Record<string, unknown> = { default_provider: provider, [provider]: { model: model || DEFAULT_MODEL[provider] } };
      if (key.trim()) (payload[provider] as Record<string, unknown>).key = key.trim();
      await apiJson(apiBaseUrl, "/api/v1/ai/config", { method: "PUT", body: JSON.stringify(payload) }, apiKey);
      setKey("");
      setSavedMsg("Saved. Key is encrypted at rest and never returned.");
      await loadConfig();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Save failed.");
    } finally { setBusy(false); }
  };

  const testConnection = async () => {
    setBusy(true); setTestMsg(null);
    try {
      const r = await apiJson<{ ok: boolean; model?: string; error?: string; sample?: string }>(
        apiBaseUrl, "/api/v1/ai/test", { method: "POST", body: JSON.stringify({ provider }) }, apiKey);
      setTestMsg(r.ok ? { ok: true, text: `Connected · ${r.model} · reply: “${r.sample}”` } : { ok: false, text: r.error || "Failed" });
    } catch (e) {
      setTestMsg({ ok: false, text: e instanceof Error ? e.message : "Failed" });
    } finally { setBusy(false); }
  };

  const generate = async () => {
    setGenerating(true); setGen(null); setError(null);
    try {
      const path = genMode === "policy" ? "/api/v1/ai/generate-policy" : "/api/v1/ai/generate-rule";
      const r = await apiJson<GenResult>(apiBaseUrl, path, { method: "POST", body: JSON.stringify({ prompt, provider }) }, apiKey);
      setGen(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Generation failed.");
    } finally { setGenerating(false); }
  };

  const configured = config?.providers?.[provider]?.configured;

  return (
    <div style={{ padding: 24, display: "grid", gap: 18, maxWidth: 1100 }}>
      <PageHeader title="AI Copilot" subtitle="Bring your own OpenAI or Anthropic key and draft rules from plain English. Keys are encrypted at rest and used server-side; off-topic prompts are refused before any token is spent." />

      {error ? <div style={{ padding: 12, borderRadius: 10, background: "var(--rm-danger-bg)", color: "var(--rm-danger)", fontSize: 13 }}>{error}</div> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(320px, 380px) minmax(0,1fr)", gap: 18, alignItems: "start" }}>
        {/* provider config */}
        <Card>
          <SectionTitle right={<KeyRound size={16} style={{ color: "var(--rm-dim)" }} />}>Provider key</SectionTitle>
          <div style={{ display: "grid", gap: 14 }}>
            <Field label="Provider">
              <Select value={provider} onChange={(e) => { setProvider(e.target.value); setModel(config?.providers?.[e.target.value]?.model || ""); }}>
                {["anthropic", "openai"].map((p) => (
                  <option key={p} value={p}>{PROVIDER_LABEL[p]}{config?.providers?.[p]?.configured ? " · configured" : ""}</option>
                ))}
              </Select>
            </Field>
            <Field label="Model" hint={modelList.live ? "Live from provider — new models appear automatically." : "Curated list (add a key to fetch the live list)."}>
              <div style={{ display: "flex", gap: 8 }}>
                <Select value={model} onChange={(e) => setModel(e.target.value)} style={{ flex: 1 }}>
                  {modelList.models.length === 0 ? <option value="">{DEFAULT_MODEL[provider]}</option> : null}
                  {modelList.models.map((m) => <option key={m} value={m}>{m}{m === modelList.default ? " · default" : ""}</option>)}
                </Select>
                <Button variant="secondary" onClick={() => loadModels(provider)} disabled={modelsBusy} title="Refresh model list">{modelsBusy ? "…" : "↻"}</Button>
              </div>
            </Field>
            <Field label={configured ? "Replace API key" : "API key"} hint={configured ? "Leave blank to keep the current key." : "Stored encrypted; never shown again."}>
              <Input type="password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={provider === "anthropic" ? "sk-ant-…" : "sk-…"} autoComplete="off" />
            </Field>
            <div style={{ display: "flex", gap: 8 }}>
              <Button variant="primary" onClick={saveKey} disabled={busy}>Save</Button>
              <Button variant="secondary" onClick={testConnection} disabled={busy || !configured}><ShieldCheck size={14} /> Test connection</Button>
            </div>
            {savedMsg ? <div style={{ fontSize: 12.5, color: "var(--rm-success)" }}>{savedMsg}</div> : null}
            {testMsg ? <div style={{ fontSize: 12.5, color: testMsg.ok ? "var(--rm-success)" : "var(--rm-danger)" }}>{testMsg.text}</div> : null}
            {config ? (
              <div style={{ display: "flex", gap: 6, marginTop: 4 }}>
                {["anthropic", "openai"].map((p) => (
                  <Badge key={p} tone={config.providers?.[p]?.configured ? "success" : "neutral"}>{PROVIDER_LABEL[p].split(" ")[0]} {config.providers?.[p]?.configured ? "✓" : "—"}</Badge>
                ))}
              </div>
            ) : null}
          </div>
        </Card>

        {/* rule generator */}
        <Card>
          <SectionTitle right={<Wand2 size={16} style={{ color: "var(--rm-accent)" }} />}>Draft from English</SectionTitle>
          <div style={{ display: "grid", gap: 12 }}>
            <div style={{ display: "flex", gap: 4, padding: 4, background: "var(--rm-hover)", borderRadius: 10, width: "fit-content" }}>
              {(["rule", "policy"] as const).map((m) => (
                <button key={m} onClick={() => { setGenMode(m); setGen(null); }}
                  style={{ padding: "6px 16px", borderRadius: 8, border: "none", cursor: "pointer", fontSize: 12.5, fontWeight: 600, textTransform: "capitalize",
                    background: genMode === m ? "var(--rm-card)" : "transparent", color: genMode === m ? "var(--rm-text)" : "var(--rm-muted)", boxShadow: genMode === m ? "var(--rm-shadow-sm)" : "none" }}>
                  {m}
                </button>
              ))}
            </div>
            <textarea className="rm-textarea" value={prompt} onChange={(e) => setPrompt(e.target.value)} rows={3} placeholder={genMode === "policy" ? "e.g. Pull the loan connector, run the bureau and KYC gates, then approve." : "e.g. Reject if there are any delinquencies in the last 2 years, else approve."} />
            <div>
              <Button variant="primary" onClick={generate} disabled={generating || !configured}><Sparkles size={15} /> {generating ? "Generating…" : "Generate draft"}</Button>
              {!configured ? <span style={{ marginLeft: 10, fontSize: 12, color: "var(--rm-dim)" }}>Add a key first.</span> : null}
            </div>

            {gen ? (
              gen.in_scope === false ? (
                <div style={{ padding: 14, borderRadius: 10, background: "var(--rm-warning-bg)", color: "var(--rm-warning)", fontSize: 13 }}>
                  <strong>Out of scope</strong> — no token spent. {gen.message}
                </div>
              ) : (
                <div style={{ display: "grid", gap: 10 }}>
                  <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                    <strong style={{ fontSize: 14, color: "var(--rm-text)" }}>{gen.draft?.name || (genMode === "policy" ? "Draft policy" : "Draft rule")}</strong>
                    <Badge tone={gen.valid ? "success" : "danger"}>{gen.valid ? "valid draft" : "needs fixes"}</Badge>
                    {gen.provider ? <Badge tone="accent">{gen.provider}</Badge> : null}
                  </div>
                  {gen.validation_error ? <div style={{ fontSize: 12.5, color: "var(--rm-danger)" }}>{gen.validation_error}</div> : null}
                  {genMode === "policy" && gen.draft?.steps ? (
                    <div style={{ display: "grid", gap: 6 }}>
                      {gen.draft.steps.map((s, i) => (
                        <div key={i} style={{ display: "flex", gap: 10, alignItems: "center", padding: "8px 10px", background: "var(--rm-card-alt)", borderRadius: 8, fontSize: 12.5 }}>
                          <span style={{ color: "var(--rm-dim)", width: 16, fontFamily: "var(--font-mono)" }}>{i + 1}</span>
                          <Badge tone="accent">{String(s.type)}</Badge>
                          <span style={{ color: "var(--rm-text)" }}>{String(s.label || s.ref_id || s.outcome || "")}</span>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <pre style={{ margin: 0, padding: 12, background: "var(--rm-editor)", border: "1px solid var(--rm-border)", borderRadius: 10, fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--rm-code-text)", whiteSpace: "pre-wrap", maxHeight: 300, overflow: "auto" }}>
                      {JSON.stringify(gen.draft?.tree ?? {}, null, 2)}
                    </pre>
                  )}
                  <div style={{ fontSize: 12, color: "var(--rm-dim)" }}>This is a draft — review it, then recreate it in the Rules builder and run its tests before promoting. AI never deploys on its own.</div>
                </div>
              )
            ) : null}
          </div>
        </Card>
      </div>
    </div>
  );
}
