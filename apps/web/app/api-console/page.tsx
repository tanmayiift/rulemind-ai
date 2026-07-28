"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES, type ThemeTokens } from "../../src/v3/theme";

type Provider = {
  id: string;
  name: string;
  category: string;
  description?: string;
  credentials?: Array<{ key?: string; label?: string } | string>;
  action: { url: string; method?: string; headers?: Record<string, string>; bodyTemplate?: unknown; timeoutMs?: number };
};
type SendResult = {
  ok: boolean;
  status?: number;
  success?: boolean;
  latencyMs?: number;
  resolvedUrl?: string;
  resolvedMethod?: string;
  responseHeaders?: Record<string, string>;
  body?: string;
  error?: string;
};

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];

export default function ApiConsolePage() {
  const { apiBaseUrl, apiKey, themeMode } = useRuleMindStore();
  const theme = THEMES[themeMode];

  const [providers, setProviders] = React.useState<Provider[]>([]);
  const [method, setMethod] = React.useState("GET");
  const [url, setUrl] = React.useState("https://api.postcodes.io/postcodes/SW1A1AA");
  const [headersText, setHeadersText] = React.useState("{}");
  const [bodyText, setBodyText] = React.useState("{}");
  const [contextText, setContextText] = React.useState('{\n  "payload": { "custom": {} },\n  "variables": {},\n  "secrets": {}\n}');
  const [tab, setTab] = React.useState<"body" | "headers" | "context">("body");
  const [result, setResult] = React.useState<SendResult | null>(null);
  const [sending, setSending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    (async () => {
      try {
        setProviders(await apiJson<Provider[]>(apiBaseUrl, "/api/v1/providers", {}, apiKey));
      } catch {
        /* provider library optional */
      }
    })();
  }, [apiBaseUrl, apiKey]);

  const loadProvider = (p: Provider) => {
    setMethod((p.action.method ?? "GET").toUpperCase());
    setUrl(p.action.url ?? "");
    setHeadersText(JSON.stringify(p.action.headers ?? {}, null, 2));
    setBodyText(JSON.stringify(p.action.bodyTemplate ?? {}, null, 2));
    setResult(null);
    setError(null);
    if (p.action.bodyTemplate && Object.keys(p.action.bodyTemplate as object).length) setTab("body");
  };

  const send = async () => {
    setError(null);
    setSending(true);
    setResult(null);
    let headers: unknown = {};
    let body: unknown = {};
    let context: unknown = {};
    try {
      headers = headersText.trim() ? JSON.parse(headersText) : {};
      body = bodyText.trim() ? JSON.parse(bodyText) : {};
      context = contextText.trim() ? JSON.parse(contextText) : {};
    } catch {
      setError("Headers, body, and context must be valid JSON.");
      setSending(false);
      return;
    }
    try {
      const res = await apiJson<SendResult>(
        apiBaseUrl,
        "/api/v1/test/action",
        { method: "POST", body: JSON.stringify({ action: { url, method, headers, bodyTemplate: body, timeoutMs: 8000 }, context }) },
        apiKey
      );
      setResult(res);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Request failed.");
    } finally {
      setSending(false);
    }
  };

  const grouped = React.useMemo(() => {
    const m: Record<string, Provider[]> = {};
    for (const p of providers) (m[p.category] ??= []).push(p);
    return m;
  }, [providers]);

  const prettyBody = React.useMemo(() => {
    if (!result?.body) return result?.body ?? "";
    try { return JSON.stringify(JSON.parse(result.body), null, 2); } catch { return result.body; }
  }, [result]);

  return (
    <div style={{ padding: 20, display: "grid", gridTemplateColumns: "230px minmax(0,1fr)", gap: 16, height: "100%", minHeight: 0 }}>
      {/* provider library */}
      <div style={{ ...panel(theme), overflowY: "auto" }}>
        <div style={{ fontSize: 11, textTransform: "uppercase", letterSpacing: 1, color: theme.dim, marginBottom: 10 }}>Provider library</div>
        <div style={{ fontSize: 12, color: theme.muted, marginBottom: 14, lineHeight: 1.5 }}>
          Ready-to-run action templates. Free/open ones work with no key; fintech ones use
          <code style={{ fontFamily: "var(--font-mono)", fontSize: 11 }}> {"{{secrets.*}}"}</code> placeholders.
        </div>
        {Object.entries(grouped).map(([cat, list]) => (
          <div key={cat} style={{ marginBottom: 14 }}>
            <div style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: 1, color: theme.dim, marginBottom: 6 }}>{cat}</div>
            <div style={{ display: "grid", gap: 6 }}>
              {list.map((p) => (
                <button key={p.id} onClick={() => loadProvider(p)} title={p.description}
                  style={{ textAlign: "left", padding: "8px 10px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.card, color: theme.text, fontSize: 12.5, cursor: "pointer" }}>
                  <div style={{ fontWeight: 600 }}>{p.name}</div>
                  {p.credentials && p.credentials.length ? <div style={{ fontSize: 10, color: theme.warning, marginTop: 2 }}>needs key</div> : <div style={{ fontSize: 10, color: theme.success, marginTop: 2 }}>no key</div>}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>

      {/* request/response */}
      <div style={{ display: "grid", gridTemplateRows: "auto auto 1fr", gap: 12, minHeight: 0 }}>
        {/* URL bar */}
        <div style={{ display: "flex", gap: 8, alignItems: "stretch" }}>
          <select value={method} onChange={(e) => setMethod(e.target.value)} style={{ ...selectStyle(theme), width: 100, fontWeight: 700 }}>
            {METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <input value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.example.com/v1/resource"
            style={{ ...inputStyle(theme), flex: 1, fontFamily: "var(--font-mono)" }} />
          <button onClick={send} disabled={sending} style={ctaStyle(theme, sending)}>{sending ? "Sending…" : "Send"}</button>
        </div>
        {error ? <div style={{ color: theme.danger, fontSize: 13 }}>{error}</div> : null}

        {/* request tabs */}
        <div style={{ ...panel(theme), padding: 0, overflow: "hidden" }}>
          <div style={{ display: "flex", borderBottom: "1px solid " + theme.border }}>
            {(["body", "headers", "context"] as const).map((t) => (
              <button key={t} onClick={() => setTab(t)}
                style={{ padding: "9px 16px", border: "none", background: "transparent", color: tab === t ? theme.accent : theme.muted, borderBottom: `2px solid ${tab === t ? theme.accent : "transparent"}`, fontSize: 13, fontWeight: 600, cursor: "pointer", textTransform: "capitalize" }}>
                {t === "context" ? "Test context" : t}
              </button>
            ))}
          </div>
          <div style={{ padding: 12 }}>
            {tab === "body" ? (
              <CodeArea theme={theme} value={bodyText} onChange={setBodyText} placeholder='{ "key": "{{payload.custom.field}}" }' />
            ) : tab === "headers" ? (
              <CodeArea theme={theme} value={headersText} onChange={setHeadersText} placeholder='{ "Authorization": "Bearer {{secrets.api_key}}" }' />
            ) : (
              <>
                <div style={{ fontSize: 12, color: theme.muted, marginBottom: 8 }}>
                  Sample values for template resolution. <strong>secrets here are console-only</strong> — stored tenant secrets are never used by this console.
                </div>
                <CodeArea theme={theme} value={contextText} onChange={setContextText} placeholder='{ "payload": {...}, "variables": {...}, "secrets": {...} }' />
              </>
            )}
          </div>
        </div>

        {/* response */}
        <div style={{ ...panel(theme), overflow: "auto", minHeight: 0 }}>
          {!result ? (
            <div style={{ color: theme.dim, fontSize: 13, textAlign: "center", marginTop: 40 }}>
              Send a request to see the response. Templates like <code style={{ fontFamily: "var(--font-mono)" }}>{"{{payload.custom.x}}"}</code> resolve server-side.
            </div>
          ) : (
            <div>
              <div style={{ display: "flex", gap: 16, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
                {result.ok ? (
                  <span style={{ fontWeight: 700, fontSize: 14, color: result.success ? theme.success : theme.warning }}>
                    {result.status} {result.success ? "OK" : ""}
                  </span>
                ) : (
                  <span style={{ fontWeight: 700, fontSize: 14, color: theme.danger }}>Request error</span>
                )}
                {typeof result.latencyMs === "number" ? <span style={{ fontSize: 12, color: theme.muted }}>{result.latencyMs} ms</span> : null}
                {result.resolvedMethod ? <span style={{ fontSize: 12, color: theme.muted, fontFamily: "var(--font-mono)" }}>{result.resolvedMethod}</span> : null}
              </div>
              {result.resolvedUrl ? (
                <div style={{ fontSize: 12, color: theme.muted, marginBottom: 10, fontFamily: "var(--font-mono)", wordBreak: "break-all" }}>
                  <span style={{ color: theme.dim }}>resolved →</span> {result.resolvedUrl}
                </div>
              ) : null}
              {result.error ? <div style={{ color: theme.danger, fontSize: 13 }}>{result.error}</div> : null}
              {result.body !== undefined ? (
                <pre style={{ margin: 0, padding: 12, background: theme.editor, borderRadius: 8, border: "1px solid " + theme.border, fontFamily: "var(--font-mono)", fontSize: 12, color: theme.codeText, whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 320, overflow: "auto" }}>
                  {prettyBody}
                </pre>
              ) : null}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function CodeArea({ theme, value, onChange, placeholder }: { theme: ThemeTokens; value: string; onChange: (v: string) => void; placeholder?: string }) {
  return (
    <textarea value={value} onChange={(e) => onChange(e.target.value)} placeholder={placeholder} spellCheck={false}
      style={{ width: "100%", boxSizing: "border-box", minHeight: 130, resize: "vertical", padding: 12, borderRadius: 8, border: "1px solid " + theme.border, background: theme.editor, color: theme.codeText, fontFamily: "var(--font-mono)", fontSize: 12.5, outline: "none" }} />
  );
}

function panel(theme: ThemeTokens): React.CSSProperties {
  return { background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 14, minHeight: 0 };
}
function inputStyle(theme: ThemeTokens): React.CSSProperties {
  return { padding: "9px 11px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.input, color: theme.text, fontSize: 13, outline: "none", boxSizing: "border-box" };
}
function selectStyle(theme: ThemeTokens): React.CSSProperties {
  return { ...inputStyle(theme), cursor: "pointer" };
}
function ctaStyle(theme: ThemeTokens, busy: boolean): React.CSSProperties {
  return { background: theme.accent, color: theme.inverseText, border: "none", borderRadius: 8, padding: "9px 20px", fontSize: 13, fontWeight: 700, cursor: busy ? "wait" : "pointer", opacity: busy ? 0.7 : 1 };
}
