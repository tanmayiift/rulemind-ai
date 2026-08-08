"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES } from "../../src/v3/theme";

type Presence = { client_id: string; actor: string; cursor?: string | null };
type Version = { version: number; doc: Record<string, unknown>; actor: string; ts: number; label?: string };
type ServerMsg =
  | { type: "init"; doc: Record<string, unknown>; version: number; presence: Presence[] }
  | { type: "edit"; changes: Record<string, unknown>; actor: string; version: number; doc: Record<string, unknown> }
  | { type: "presence"; presence: Presence[] }
  | { type: "pong" };

// The editable document shape — a policy's headline fields, edited collaboratively.
const FIELDS: Array<{ key: string; label: string; kind: "text" | "number" | "select"; options?: string[] }> = [
  { key: "name", label: "Policy name", kind: "text" },
  { key: "description", label: "Description", kind: "text" },
  { key: "threshold", label: "Approval threshold", kind: "number" },
  { key: "defaultOutcome", label: "Default outcome", kind: "select", options: ["approve", "review", "reject"] },
];

const ACTOR_COLORS = ["#635bff", "#0aa678", "#e0a12a", "#d1435b", "#8b5cf6", "#0891b2"];
function colorFor(actor: string): string {
  let h = 0;
  for (let i = 0; i < actor.length; i++) h = (h * 31 + actor.charCodeAt(i)) >>> 0;
  return ACTOR_COLORS[h % ACTOR_COLORS.length];
}

function wsUrlFrom(apiBaseUrl: string, path: string): string {
  const base = apiBaseUrl.replace(/\/$/, "");
  if (base.startsWith("https://")) return "wss://" + base.slice("https://".length) + path;
  if (base.startsWith("http://")) return "ws://" + base.slice("http://".length) + path;
  return (typeof window !== "undefined" && window.location.protocol === "https:" ? "wss://" : "ws://") + base + path;
}

export default function CollabEditorPage() {
  const { apiBaseUrl, apiKey, themeMode, isMobile } = useRuleMindStore();
  const theme = THEMES[themeMode];

  const [docId, setDocId] = React.useState("policy-shared-draft");
  const [actor, setActor] = React.useState("");
  const [connected, setConnected] = React.useState(false);
  const [doc, setDoc] = React.useState<Record<string, unknown>>({});
  const [version, setVersion] = React.useState(0);
  const [presence, setPresence] = React.useState<Presence[]>([]);
  const [history, setHistory] = React.useState<Version[]>([]);
  const [travelTo, setTravelTo] = React.useState<number | null>(null); // null = live
  const [error, setError] = React.useState<string | null>(null);

  const wsRef = React.useRef<WebSocket | null>(null);
  const actorRef = React.useRef<string>("");

  // Stable per-tab actor name.
  React.useEffect(() => {
    let a = actor;
    if (!a) {
      const stored = typeof window !== "undefined" ? window.localStorage.getItem("rm_collab_actor") : null;
      a = stored || "editor-" + Math.random().toString(36).slice(2, 6);
      window.localStorage.setItem("rm_collab_actor", a);
      setActor(a);
    }
    actorRef.current = a;
  }, [actor]);

  const loadHistory = React.useCallback(async () => {
    try {
      const res = await apiJson<{ history: Version[] }>(apiBaseUrl, `/api/v1/collab/${encodeURIComponent(docId)}/history`, {}, apiKey);
      setHistory(res.history || []);
    } catch {
      /* history is best-effort */
    }
  }, [apiBaseUrl, apiKey, docId]);

  const connect = React.useCallback(() => {
    if (!actorRef.current) return;
    wsRef.current?.close();
    setError(null);
    const url = wsUrlFrom(apiBaseUrl, `/ws/v1/collab/${encodeURIComponent(docId)}?actor=${encodeURIComponent(actorRef.current)}`);
    let ws: WebSocket;
    try {
      ws = new WebSocket(url);
    } catch {
      setError("Could not open a WebSocket to " + url);
      return;
    }
    wsRef.current = ws;
    ws.onopen = () => { setConnected(true); setError(null); };
    ws.onclose = () => setConnected(false);
    ws.onerror = () => { if (ws.readyState !== WebSocket.OPEN) setError("WebSocket error — is the backend running at " + apiBaseUrl + "?"); };
    ws.onmessage = (ev) => {
      let msg: ServerMsg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === "init") {
        setDoc(msg.doc || {});
        setVersion(msg.version);
        setPresence(msg.presence || []);
        void loadHistory();
      } else if (msg.type === "edit") {
        setDoc(msg.doc || {});
        setVersion(msg.version);
        void loadHistory();
      } else if (msg.type === "presence") {
        setPresence(msg.presence || []);
      }
    };
  }, [apiBaseUrl, docId, loadHistory]);

  React.useEffect(() => {
    // Defer the connect a tick so React StrictMode's throw-away first mount cancels before any
    // socket is opened (avoids the dev-only "WebSocket closed during CONNECTING" console noise).
    const t = setTimeout(() => connect(), 60);
    return () => {
      clearTimeout(t);
      wsRef.current?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [docId, actor]);

  const send = (payload: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
  };

  const editField = (key: string, value: unknown) => {
    setDoc((d) => ({ ...d, [key]: value }));       // optimistic local update
    send({ type: "edit", changes: { [key]: value } });
  };
  const setCursor = (key: string | null) => send({ type: "cursor", field: key });

  const isTraveling = travelTo !== null;
  const [travelDoc, setTravelDoc] = React.useState<Record<string, unknown> | null>(null);
  React.useEffect(() => {
    if (travelTo === null) {
      setTravelDoc(null);
      return;
    }
    apiJson<{ doc: Record<string, unknown> }>(apiBaseUrl, `/api/v1/collab/${encodeURIComponent(docId)}/as-of/${travelTo}`, {}, apiKey)
      .then((r) => setTravelDoc(r.doc || {}))
      .catch(() => setTravelDoc(null));
  }, [travelTo, apiBaseUrl, apiKey, docId]);

  const restore = (v: number) => {
    send({ type: "restore", version: v });
    setTravelTo(null);
  };

  const shown = isTraveling ? travelDoc || {} : doc;
  const card: React.CSSProperties = { background: theme.card, border: "1px solid " + theme.border, borderRadius: 12 };
  const others = presence.filter((p) => p.actor !== actor);
  const cursorsByField: Record<string, string[]> = {};
  for (const p of others) if (p.cursor) (cursorsByField[p.cursor] ||= []).push(p.actor);

  return (
    <div style={{ padding: isMobile ? 16 : 24, color: theme.text }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12, flexWrap: "wrap" }}>
        <h1 style={{ fontSize: 22, letterSpacing: "-0.02em", margin: 0 }}>Collaborative Editor</h1>
        <span
          data-testid="conn-status"
          style={{ fontSize: 11.5, fontWeight: 700, textTransform: "uppercase", padding: "3px 9px", borderRadius: 999,
            color: connected ? theme.success : theme.danger, background: (connected ? theme.success : theme.danger) + "1e" }}
        >
          {connected ? "● Live" : "○ Offline"}
        </span>
      </div>
      <p style={{ color: theme.muted, margin: "4px 0 20px", fontSize: 13.5 }}>
        Multiple people edit the same policy draft at once — changes merge conflict-free (CRDT), presence is live,
        and you can time-travel to any past version and restore it.
      </p>

      {/* Document + actor controls */}
      <div style={{ ...card, padding: 12, marginBottom: 16, display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
        <label style={{ fontSize: 12, color: theme.muted }}>Document</label>
        <input
          value={docId}
          onChange={(e) => setDocId(e.target.value)}
          style={{ border: "1px solid " + theme.border, background: theme.bg, color: theme.text, borderRadius: 8, padding: "6px 10px", fontSize: 13, fontFamily: "var(--font-mono)" }}
        />
        <label style={{ fontSize: 12, color: theme.muted, marginLeft: 8 }}>You</label>
        <input
          value={actor}
          onChange={(e) => { setActor(e.target.value); window.localStorage.setItem("rm_collab_actor", e.target.value); }}
          style={{ border: "1px solid " + theme.border, background: theme.bg, color: colorFor(actor), fontWeight: 700, borderRadius: 8, padding: "6px 10px", fontSize: 13, width: 140 }}
        />
        <button
          onClick={connect}
          style={{ marginLeft: "auto", border: 0, background: theme.accent, color: "#fff", borderRadius: 8, padding: "7px 14px", fontSize: 13, fontWeight: 600, cursor: "pointer" }}
        >
          Reconnect
        </button>
      </div>

      {error ? <div style={{ ...card, padding: 14, color: theme.danger, marginBottom: 16 }}>{error}</div> : null}

      {/* Presence bar */}
      <div style={{ ...card, padding: "10px 14px", marginBottom: 16, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <span style={{ fontSize: 12, color: theme.muted }}>Editing now:</span>
        {presence.length === 0 ? <span style={{ fontSize: 12.5, color: theme.muted }}>just you</span> : null}
        {presence.map((p) => (
          <span key={p.client_id} data-testid="presence-chip"
            style={{ display: "inline-flex", alignItems: "center", gap: 6, fontSize: 12, fontWeight: 600, padding: "3px 10px", borderRadius: 999, color: colorFor(p.actor), background: colorFor(p.actor) + "1e" }}>
            <span style={{ width: 7, height: 7, borderRadius: 999, background: colorFor(p.actor) }} />
            {p.actor}{p.actor === actor ? " (you)" : ""}{p.cursor ? ` · ${p.cursor}` : ""}
          </span>
        ))}
      </div>

      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "1.4fr 1fr", gap: 16, alignItems: "start" }}>
        {/* Editor */}
        <div style={{ ...card, padding: 18 }}>
          {isTraveling ? (
            <div style={{ marginBottom: 14, padding: "8px 12px", borderRadius: 8, background: theme.warning + "1e", color: theme.warning, fontSize: 12.5, fontWeight: 600, display: "flex", alignItems: "center", gap: 10 }}>
              Viewing version {travelTo} (read-only)
              <button onClick={() => restore(travelTo!)} style={{ border: 0, background: theme.warning, color: "#fff", borderRadius: 7, padding: "4px 10px", fontSize: 12, fontWeight: 600, cursor: "pointer" }}>Restore this version</button>
              <button onClick={() => setTravelTo(null)} style={{ border: "1px solid " + theme.border, background: "transparent", color: theme.text, borderRadius: 7, padding: "4px 10px", fontSize: 12, cursor: "pointer" }}>Back to live</button>
            </div>
          ) : null}
          {FIELDS.map((f) => {
            const editors = cursorsByField[f.key] || [];
            const highlight = editors.length ? colorFor(editors[0]) : theme.border;
            return (
              <div key={f.key} style={{ marginBottom: 16 }}>
                <label style={{ display: "flex", justifyContent: "space-between", fontSize: 12, color: theme.muted, marginBottom: 5 }}>
                  <span>{f.label}</span>
                  {editors.length ? <span style={{ color: colorFor(editors[0]), fontWeight: 600 }}>{editors.join(", ")} editing…</span> : null}
                </label>
                {f.kind === "select" ? (
                  <select
                    data-testid={`field-${f.key}`} disabled={isTraveling}
                    value={String(shown[f.key] ?? "")}
                    onFocus={() => setCursor(f.key)} onBlur={() => setCursor(null)}
                    onChange={(e) => editField(f.key, e.target.value)}
                    style={{ width: "100%", border: "1px solid " + highlight, background: theme.bg, color: theme.text, borderRadius: 8, padding: "9px 11px", fontSize: 14 }}
                  >
                    <option value=""></option>
                    {f.options!.map((o) => <option key={o} value={o}>{o}</option>)}
                  </select>
                ) : (
                  <input
                    data-testid={`field-${f.key}`} disabled={isTraveling}
                    type={f.kind === "number" ? "number" : "text"}
                    value={String(shown[f.key] ?? "")}
                    onFocus={() => setCursor(f.key)} onBlur={() => setCursor(null)}
                    onChange={(e) => editField(f.key, f.kind === "number" ? Number(e.target.value) : e.target.value)}
                    style={{ width: "100%", border: "1px solid " + highlight, background: theme.bg, color: theme.text, borderRadius: 8, padding: "9px 11px", fontSize: 14, fontFamily: f.kind === "number" ? "var(--font-mono)" : undefined }}
                  />
                )}
              </div>
            );
          })}
          <div style={{ fontSize: 11.5, color: theme.muted, marginTop: 4 }}>Live version: v{version}</div>
        </div>

        {/* Time-travel history */}
        <div style={{ ...card, padding: 18 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 12 }}>
            <h2 style={{ fontSize: 15, margin: 0 }}>Time travel</h2>
            <span style={{ fontSize: 11.5, color: theme.muted }}>{history.length} version{history.length === 1 ? "" : "s"}</span>
          </div>
          {history.length > 1 ? (
            <div style={{ marginBottom: 14 }}>
              <input
                data-testid="time-slider" type="range"
                min={history[0].version} max={history[history.length - 1].version}
                value={travelTo ?? history[history.length - 1].version}
                onChange={(e) => {
                  const v = Number(e.target.value);
                  setTravelTo(v >= history[history.length - 1].version ? null : v);
                }}
                style={{ width: "100%", accentColor: theme.accent }}
              />
              <div style={{ fontSize: 11.5, color: theme.muted, textAlign: "center" }}>
                {isTraveling ? `version ${travelTo}` : "live (latest)"}
              </div>
            </div>
          ) : null}
          <div style={{ display: "flex", flexDirection: "column", gap: 6, maxHeight: 320, overflowY: "auto" }}>
            {[...history].reverse().map((h) => (
              <button
                key={h.version} data-testid={`version-${h.version}`}
                onClick={() => setTravelTo(h.version === history[history.length - 1].version ? null : h.version)}
                style={{ textAlign: "left", border: "1px solid " + (travelTo === h.version ? theme.accent : theme.border), background: travelTo === h.version ? theme.accentBg : "transparent", color: theme.text, borderRadius: 8, padding: "8px 11px", cursor: "pointer" }}
              >
                <div style={{ display: "flex", justifyContent: "space-between", fontSize: 12.5, fontWeight: 600 }}>
                  <span>v{h.version} · {h.label}</span>
                  <span style={{ color: colorFor(h.actor) }}>{h.actor}</span>
                </div>
                <div style={{ fontSize: 11, color: theme.muted, marginTop: 2 }}>{new Date(h.ts * 1000).toLocaleString()}</div>
              </button>
            ))}
            {history.length === 0 ? <div style={{ color: theme.muted, fontSize: 12.5 }}>No edits yet.</div> : null}
          </div>
        </div>
      </div>
    </div>
  );
}
