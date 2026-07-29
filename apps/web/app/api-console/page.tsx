"use client";

import * as React from "react";
import { Upload, Play, Library, FolderTree, Plus, Trash2 } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Badge, EmptyState, PageHeader } from "../../src/v3/ui";

type Provider = {
  id: string; name: string; category: string; description?: string;
  credentials?: Array<{ key?: string; label?: string } | string>;
  action: { url: string; method?: string; headers?: Record<string, string>; bodyTemplate?: unknown; timeoutMs?: number };
};
type SendResult = { ok: boolean; status?: number; success?: boolean; latencyMs?: number; resolvedUrl?: string; resolvedMethod?: string; responseHeaders?: Record<string, string>; body?: string; error?: string };
type SavedRequest = { id: string; name: string; method: string; url: string; headers: Record<string, string>; body: string; folder?: string };
type EnvVar = { key: string; value: string };
type RunRow = { name: string; method: string; status?: number; ok: boolean; latencyMs?: number; error?: string };

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"];
const METHOD_TONE: Record<string, "success" | "accent" | "warning" | "danger" | "neutral"> = { GET: "success", POST: "accent", PUT: "warning", PATCH: "warning", DELETE: "danger" };

// ---- Postman v2.1 collection parsing -----------------------------------------

function urlToString(u: unknown): string {
  if (typeof u === "string") return u;
  if (u && typeof u === "object" && "raw" in (u as Record<string, unknown>)) return String((u as { raw?: string }).raw ?? "");
  return "";
}
function headersToMap(h: unknown): Record<string, string> {
  const map: Record<string, string> = {};
  if (Array.isArray(h)) for (const item of h) { if (item && !item.disabled && item.key) map[item.key] = String(item.value ?? ""); }
  return map;
}
function flattenPostman(items: unknown[], folder: string, out: SavedRequest[]) {
  for (const raw of items as Array<Record<string, unknown>>) {
    if (Array.isArray(raw.item)) { flattenPostman(raw.item, String(raw.name ?? folder), out); continue; }
    const req = raw.request as Record<string, unknown> | undefined;
    if (!req) continue;
    const body = req.body as { raw?: string } | undefined;
    out.push({
      id: `${out.length}-${String(raw.name ?? "request")}`,
      name: String(raw.name ?? "Untitled"),
      method: String(req.method ?? "GET").toUpperCase(),
      url: urlToString(req.url),
      headers: headersToMap(req.header),
      body: body?.raw ?? "",
      folder,
    });
  }
}
function parseCollection(json: unknown): SavedRequest[] {
  const out: SavedRequest[] = [];
  const root = json as { item?: unknown[] };
  if (Array.isArray(root.item)) flattenPostman(root.item, "", out);
  return out;
}
function parseEnvironment(json: unknown): EnvVar[] {
  const root = json as { values?: Array<{ key?: string; value?: string; enabled?: boolean }> };
  if (Array.isArray(root.values)) return root.values.filter((v) => v.enabled !== false && v.key).map((v) => ({ key: String(v.key), value: String(v.value ?? "") }));
  return [];
}

// substitute {{key}} using the environment map (Postman semantics), client-side.
function applyEnv(str: string, env: Record<string, string>): string {
  return str.replace(/\{\{\s*([\w.-]+)\s*\}\}/g, (m, k) => (k in env ? env[k] : m));
}

export default function ApiConsolePage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();

  const [providers, setProviders] = React.useState<Provider[]>([]);
  const [tab, setTab] = React.useState<"providers" | "collection">("providers");
  const [collection, setCollection] = React.useState<SavedRequest[]>([]);
  const [collectionName, setCollectionName] = React.useState<string | null>(null);
  const [env, setEnv] = React.useState<EnvVar[]>([]);
  const [showEnv, setShowEnv] = React.useState(false);

  const [method, setMethod] = React.useState("GET");
  const [url, setUrl] = React.useState("https://api.postcodes.io/postcodes/SW1A1AA");
  const [headersText, setHeadersText] = React.useState("{}");
  const [bodyText, setBodyText] = React.useState("{}");
  const [reqTab, setReqTab] = React.useState<"body" | "headers">("body");
  const [result, setResult] = React.useState<SendResult | null>(null);
  const [sending, setSending] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  const [runRows, setRunRows] = React.useState<RunRow[] | null>(null);
  const [running, setRunning] = React.useState(false);

  React.useEffect(() => {
    (async () => { try { setProviders(await apiJson<Provider[]>(apiBaseUrl, "/api/v1/providers", {}, apiKey)); } catch { /* optional */ } })();
  }, [apiBaseUrl, apiKey]);

  const envMap = React.useMemo(() => Object.fromEntries(env.filter((e) => e.key).map((e) => [e.key, e.value])), [env]);

  const loadProvider = (p: Provider) => {
    setMethod((p.action.method ?? "GET").toUpperCase());
    setUrl(p.action.url ?? "");
    setHeadersText(JSON.stringify(p.action.headers ?? {}, null, 2));
    setBodyText(JSON.stringify(p.action.bodyTemplate ?? {}, null, 2));
    setResult(null); setError(null);
  };
  const loadRequest = (r: SavedRequest) => {
    setMethod(r.method); setUrl(r.url);
    setHeadersText(JSON.stringify(r.headers ?? {}, null, 2));
    setBodyText(r.body || "{}");
    setResult(null); setError(null);
  };

  const importFile = async (file: File, kind: "collection" | "env") => {
    setError(null);
    try {
      const json = JSON.parse(await file.text());
      if (kind === "collection") {
        const reqs = parseCollection(json);
        if (!reqs.length) throw new Error("No requests found — is this a Postman v2.1 collection?");
        setCollection(reqs); setCollectionName(json?.info?.name ?? file.name); setTab("collection");
      } else {
        const vars = parseEnvironment(json);
        if (!vars.length) throw new Error("No variables found in that environment file.");
        setEnv(vars); setShowEnv(true);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not parse that file.");
    }
  };

  const buildAction = (m: string, u: string, headers: Record<string, string>, body: unknown) => ({
    url: applyEnv(u, envMap),
    method: m,
    headers: Object.fromEntries(Object.entries(headers).map(([k, v]) => [k, applyEnv(String(v), envMap)])),
    bodyTemplate: body, timeoutMs: 10000,
  });

  const send = async () => {
    setError(null); setSending(true); setResult(null);
    let headers: Record<string, string> = {}; let body: unknown = {};
    try {
      headers = headersText.trim() ? JSON.parse(headersText) : {};
      const bt = applyEnv(bodyText, envMap).trim();
      body = bt ? JSON.parse(bt) : {};
    } catch { setError("Headers and body must be valid JSON."); setSending(false); return; }
    try {
      const res = await apiJson<SendResult>(apiBaseUrl, "/api/v1/test/action", { method: "POST", body: JSON.stringify({ action: buildAction(method, url, headers, body), context: {} }) }, apiKey);
      setResult(res);
    } catch (e) { setError(e instanceof Error ? e.message : "Request failed."); } finally { setSending(false); }
  };

  const runCollection = async () => {
    if (!collection.length) return;
    setRunning(true); setRunRows([]);
    const rows: RunRow[] = [];
    for (const r of collection) {
      let body: unknown = {};
      try { const bt = applyEnv(r.body, envMap).trim(); body = bt ? JSON.parse(bt) : {}; } catch { body = {}; }
      try {
        const res = await apiJson<SendResult>(apiBaseUrl, "/api/v1/test/action", { method: "POST", body: JSON.stringify({ action: buildAction(r.method, r.url, r.headers, body), context: {} }) }, apiKey);
        rows.push({ name: r.name, method: r.method, status: res.status, ok: !!res.success, latencyMs: res.latencyMs, error: res.error });
      } catch (e) {
        rows.push({ name: r.name, method: r.method, ok: false, error: e instanceof Error ? e.message : "failed" });
      }
      setRunRows([...rows]);
    }
    setRunning(false);
  };

  const grouped = React.useMemo(() => { const m: Record<string, Provider[]> = {}; for (const p of providers) (m[p.category] ??= []).push(p); return m; }, [providers]);
  const prettyBody = React.useMemo(() => { if (!result?.body) return result?.body ?? ""; try { return JSON.stringify(JSON.parse(result.body), null, 2); } catch { return result.body; } }, [result]);
  const passed = runRows ? runRows.filter((r) => r.ok).length : 0;

  return (
    <div style={{ padding: 24, height: "100%", display: "flex", flexDirection: "column", minHeight: 0 }}>
      <PageHeader title="API Console" subtitle="Build, import, and run API calls for workflow action steps — resolve templates and send server-side (no CORS). Import a Postman collection and run the whole thing." />

      <div style={{ display: "grid", gridTemplateColumns: "260px minmax(0,1fr)", gap: 18, flex: 1, minHeight: 0 }}>
        {/* left rail */}
        <Card pad={false} style={{ display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <div style={{ display: "flex", padding: 6, gap: 4, borderBottom: "1px solid var(--rm-border)" }}>
            {([["providers", "Library", Library], ["collection", "Collection", FolderTree]] as const).map(([t, label, Icon]) => (
              <button key={t} onClick={() => setTab(t)} style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", gap: 6, padding: "8px", borderRadius: 8, border: "none", cursor: "pointer", fontSize: 12.5, fontWeight: 600, background: tab === t ? "var(--rm-hover)" : "transparent", color: tab === t ? "var(--rm-text)" : "var(--rm-muted)" }}>
                <Icon size={14} /> {label}
              </button>
            ))}
          </div>

          <div style={{ overflowY: "auto", flex: 1, padding: 12 }}>
            {tab === "providers" ? (
              Object.entries(grouped).map(([cat, list]) => (
                <div key={cat} style={{ marginBottom: 14 }}>
                  <div className="rm-label" style={{ marginBottom: 6 }}>{cat}</div>
                  <div style={{ display: "grid", gap: 6 }}>
                    {list.map((p) => (
                      <button key={p.id} onClick={() => loadProvider(p)} title={p.description} style={railItem}>
                        <div style={{ fontWeight: 600, color: "var(--rm-text)", fontSize: 12.5 }}>{p.name}</div>
                        <div style={{ marginTop: 3 }}>{p.credentials && p.credentials.length ? <Badge tone="warning">needs key</Badge> : <Badge tone="success">no key</Badge>}</div>
                      </button>
                    ))}
                  </div>
                </div>
              ))
            ) : (
              <div>
                <label style={{ display: "block", marginBottom: 10 }}>
                  <input type="file" accept=".json" style={{ display: "none" }} onChange={(e) => { const f = e.target.files?.[0]; if (f) importFile(f, "collection"); }} />
                  <span className="rm-btn rm-btn-secondary rm-btn-sm" style={{ width: "100%", cursor: "pointer" }}><Upload size={14} /> Import Postman collection</span>
                </label>
                {collection.length ? (
                  <>
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
                      <span style={{ fontSize: 12, color: "var(--rm-muted)", fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{collectionName}</span>
                      <Badge tone="accent">{collection.length}</Badge>
                    </div>
                    <Button variant="primary" size="sm" onClick={runCollection} disabled={running} style={{ width: "100%", marginBottom: 10 }}><Play size={13} /> {running ? "Running…" : "Run all"}</Button>
                    <div style={{ display: "grid", gap: 5 }}>
                      {collection.map((r) => (
                        <button key={r.id} onClick={() => loadRequest(r)} style={{ ...railItem, display: "flex", alignItems: "center", gap: 8 }}>
                          <Badge tone={METHOD_TONE[r.method] ?? "neutral"}>{r.method}</Badge>
                          <span style={{ fontSize: 12, color: "var(--rm-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</span>
                        </button>
                      ))}
                    </div>
                  </>
                ) : (
                  <EmptyState icon={<FolderTree size={20} />} title="No collection" hint="Import a Postman v2.1 collection to load, edit, and run every request." />
                )}
              </div>
            )}
          </div>
        </Card>

        {/* main */}
        <div style={{ display: "grid", gridTemplateRows: "auto auto auto 1fr", gap: 12, minHeight: 0 }}>
          {/* url bar */}
          <div style={{ display: "flex", gap: 8 }}>
            <select className="rm-select" value={method} onChange={(e) => setMethod(e.target.value)} style={{ width: 108, fontWeight: 700 }}>
              {METHODS.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
            <input className="rm-input rm-mono" value={url} onChange={(e) => setUrl(e.target.value)} placeholder="https://api.example.com/v1/resource" style={{ flex: 1 }} />
            <Button variant="secondary" onClick={() => setShowEnv((s) => !s)}>Env{env.length ? ` · ${env.length}` : ""}</Button>
            <Button variant="primary" onClick={send} disabled={sending}>{sending ? "Sending…" : "Send"}</Button>
          </div>
          {error ? <div style={{ color: "var(--rm-danger)", fontSize: 13 }}>{error}</div> : null}

          {/* environment editor */}
          {showEnv ? (
            <Card>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 10 }}>
                <span className="rm-label">Environment · {"{{variables}}"} substituted before send</span>
                <label>
                  <input type="file" accept=".json" style={{ display: "none" }} onChange={(e) => { const f = e.target.files?.[0]; if (f) importFile(f, "env"); }} />
                  <span className="rm-btn rm-btn-ghost rm-btn-sm" style={{ cursor: "pointer" }}><Upload size={13} /> Import env</span>
                </label>
              </div>
              <div style={{ display: "grid", gap: 6 }}>
                {env.map((v, i) => (
                  <div key={i} style={{ display: "flex", gap: 8 }}>
                    <input className="rm-input rm-mono" value={v.key} placeholder="key" onChange={(e) => setEnv((arr) => arr.map((x, j) => (j === i ? { ...x, key: e.target.value } : x)))} style={{ flex: 1 }} />
                    <input className="rm-input rm-mono" value={v.value} placeholder="value" onChange={(e) => setEnv((arr) => arr.map((x, j) => (j === i ? { ...x, value: e.target.value } : x)))} style={{ flex: 2 }} />
                    <Button variant="ghost" size="sm" onClick={() => setEnv((arr) => arr.filter((_, j) => j !== i))}><Trash2 size={14} /></Button>
                  </div>
                ))}
                <Button variant="ghost" size="sm" onClick={() => setEnv((arr) => [...arr, { key: "", value: "" }])} style={{ justifySelf: "start" }}><Plus size={14} /> Add variable</Button>
              </div>
            </Card>
          ) : null}

          {/* request tabs */}
          <Card pad={false} style={{ overflow: "hidden" }}>
            <div style={{ display: "flex", borderBottom: "1px solid var(--rm-border)" }}>
              {(["body", "headers"] as const).map((t) => (
                <button key={t} onClick={() => setReqTab(t)} style={{ padding: "10px 18px", border: "none", background: "transparent", color: reqTab === t ? "var(--rm-accent)" : "var(--rm-muted)", borderBottom: `2px solid ${reqTab === t ? "var(--rm-accent)" : "transparent"}`, fontSize: 13, fontWeight: 600, cursor: "pointer", textTransform: "capitalize" }}>{t}</button>
              ))}
            </div>
            <div style={{ padding: 12 }}>
              <textarea className="rm-textarea rm-mono" spellCheck={false} value={reqTab === "body" ? bodyText : headersText}
                onChange={(e) => (reqTab === "body" ? setBodyText(e.target.value) : setHeadersText(e.target.value))}
                placeholder={reqTab === "body" ? '{ "key": "{{value}}" }' : '{ "Authorization": "Bearer {{token}}" }'}
                style={{ minHeight: 110, fontSize: 12.5, border: "none", boxShadow: "none", padding: 0, background: "transparent" }} />
            </div>
          </Card>

          {/* response / runner */}
          <Card style={{ overflow: "auto", minHeight: 0 }}>
            {runRows ? (
              <div>
                <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
                  <strong style={{ fontSize: 14, color: "var(--rm-text)" }}>Collection run</strong>
                  <Badge tone={passed === runRows.length ? "success" : "warning"}>{passed}/{runRows.length} passed</Badge>
                </div>
                <div style={{ display: "grid", gap: 4 }}>
                  {runRows.map((r, i) => (
                    <div key={i} style={{ display: "flex", alignItems: "center", gap: 10, padding: "8px 10px", borderRadius: 8, background: "var(--rm-card-alt)", fontSize: 12.5 }}>
                      <Badge tone={METHOD_TONE[r.method] ?? "neutral"}>{r.method}</Badge>
                      <span style={{ flex: 1, color: "var(--rm-text)", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{r.name}</span>
                      {r.status ? <span className="rm-mono" style={{ color: r.ok ? "var(--rm-success)" : "var(--rm-danger)" }}>{r.status}</span> : <span style={{ color: "var(--rm-danger)", fontSize: 11 }}>{r.error ?? "error"}</span>}
                      {typeof r.latencyMs === "number" ? <span className="rm-mono" style={{ color: "var(--rm-dim)", width: 60, textAlign: "right" }}>{Math.round(r.latencyMs)}ms</span> : null}
                    </div>
                  ))}
                </div>
                <Button variant="ghost" size="sm" onClick={() => setRunRows(null)} style={{ marginTop: 10 }}>Back to response</Button>
              </div>
            ) : !result ? (
              <EmptyState icon={<Play size={22} />} title="Send a request" hint="Templates like {{payload.custom.x}} resolve server-side; {{env}} vars substitute client-side. Or import a collection and Run all." />
            ) : (
              <div>
                <div style={{ display: "flex", gap: 14, alignItems: "center", marginBottom: 12, flexWrap: "wrap" }}>
                  <Badge tone={result.ok ? (result.success ? "success" : "warning") : "danger"}>{result.ok ? `${result.status} ${result.success ? "OK" : ""}` : "Error"}</Badge>
                  {typeof result.latencyMs === "number" ? <span style={{ fontSize: 12.5, color: "var(--rm-muted)" }}>{result.latencyMs} ms</span> : null}
                  {result.resolvedMethod ? <span className="rm-mono" style={{ fontSize: 12, color: "var(--rm-dim)" }}>{result.resolvedMethod}</span> : null}
                </div>
                {result.resolvedUrl ? <div className="rm-mono" style={{ fontSize: 12, color: "var(--rm-muted)", marginBottom: 10, wordBreak: "break-all" }}><span style={{ color: "var(--rm-dim)" }}>resolved →</span> {result.resolvedUrl}</div> : null}
                {result.error ? <div style={{ color: "var(--rm-danger)", fontSize: 13 }}>{result.error}</div> : null}
                {result.body !== undefined ? <pre style={{ margin: 0, padding: 12, background: "var(--rm-editor)", borderRadius: 10, border: "1px solid var(--rm-border)", fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--rm-code-text)", whiteSpace: "pre-wrap", wordBreak: "break-word", maxHeight: 320, overflow: "auto" }}>{prettyBody}</pre> : null}
              </div>
            )}
          </Card>
        </div>
      </div>
    </div>
  );
}

const railItem: React.CSSProperties = {
  width: "100%", textAlign: "left", padding: "8px 10px", borderRadius: 9,
  border: "1px solid var(--rm-border)", background: "var(--rm-card)", cursor: "pointer",
};
