"use client";

import * as React from "react";
import { Plus, Trash2, Copy, ShieldCheck, KeyRound } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type ApiKey = { id: string; kid: string; masked_key: string; role: string; label?: string; environment?: string; is_active: boolean; is_current?: boolean; created_at?: string };
type RoleRef = { role: string; capabilities: string[]; description: string };
type Me = { role: string; capabilities: string[] };
type RolesResp = { assignable: string[]; roles: RoleRef[] };

const ROLE_TONE: Record<string, "success" | "accent" | "warning" | "danger" | "neutral"> = {
  owner: "danger", admin: "danger", policy_maker: "accent", reviewer: "warning", viewer: "neutral",
};
const ROLE_LABEL: Record<string, string> = {
  owner: "Owner", admin: "Admin", policy_maker: "Policy maker", reviewer: "Reviewer", viewer: "Viewer",
};

export default function AccessPage() {
  const { apiBaseUrl, apiKey } = useRuleMindStore();
  const [me, setMe] = React.useState<Me | null>(null);
  const [roles, setRoles] = React.useState<RolesResp | null>(null);
  const [keys, setKeys] = React.useState<ApiKey[]>([]);
  const [error, setError] = React.useState<string | null>(null);
  const [newRole, setNewRole] = React.useState("viewer");
  const [newLabel, setNewLabel] = React.useState("");
  const [newEnv, setNewEnv] = React.useState("prod");
  const [issued, setIssued] = React.useState<{ plaintext: string; role: string } | null>(null);

  const canManage = me?.capabilities?.includes("manage_access") ?? false;

  const load = React.useCallback(async () => {
    try {
      const [m, r] = await Promise.all([
        apiJson<Me>(apiBaseUrl, "/api/v1/access/me", {}, apiKey),
        apiJson<RolesResp>(apiBaseUrl, "/api/v1/access/roles", {}, apiKey),
      ]);
      setMe(m); setRoles(r);
      setNewRole(r.assignable[r.assignable.length - 1] ?? "viewer");
      try { setKeys(await apiJson<ApiKey[]>(apiBaseUrl, "/api/v1/access/keys", {}, apiKey)); } catch { /* read may be limited */ }
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to load access settings.");
    }
  }, [apiBaseUrl, apiKey]);

  React.useEffect(() => { void load(); }, [load]);

  const createKey = async () => {
    setError(null); setIssued(null);
    try {
      const k = await apiJson<{ plaintext: string; role: string }>(apiBaseUrl, "/api/v1/access/keys", {
        method: "POST", body: JSON.stringify({ role: newRole, label: newLabel || undefined, environment: newEnv }),
      }, apiKey);
      setIssued(k); setNewLabel("");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not create key."); }
  };

  const revoke = async (kid: string) => {
    setError(null);
    try {
      await apiJson(apiBaseUrl, `/api/v1/access/keys/${kid}`, { method: "DELETE" }, apiKey);
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Revoke failed."); }
  };

  const activeKeys = keys.filter((k) => k.is_active);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 20 }}>
      <PageHeader
        title="Access & Roles"
        subtitle="Issue role-scoped API keys and control what each teammate or integration can do."
        actions={me ? <Badge tone={ROLE_TONE[me.role] ?? "neutral"}>Your role: {ROLE_LABEL[me.role] ?? me.role}</Badge> : undefined}
      />

      {error ? <Card><div style={{ color: "var(--rm-danger)", fontSize: 13 }}>{error}</div></Card> : null}

      {!canManage ? (
        <Card>
          <div style={{ display: "flex", gap: 10, alignItems: "center", color: "var(--rm-muted)", fontSize: 13 }}>
            <ShieldCheck size={16} />
            Your role ({ROLE_LABEL[me?.role ?? ""] ?? me?.role}) is read-only for access management. Ask an admin to issue or revoke keys.
          </div>
        </Card>
      ) : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 340px", gap: 20, alignItems: "start" }}>
        {/* Keys */}
        <div style={{ display: "flex", flexDirection: "column", gap: 16, minWidth: 0 }}>
          {canManage ? (
            <Card>
              <SectionTitle>Issue a key</SectionTitle>
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 120px auto", gap: 10, alignItems: "end" }}>
                <Field label="Role">
                  <Select value={newRole} onChange={(e) => setNewRole(e.target.value)}>
                    {(roles?.assignable ?? []).map((r) => <option key={r} value={r}>{ROLE_LABEL[r] ?? r}</option>)}
                  </Select>
                </Field>
                <Field label="Label"><Input value={newLabel} onChange={(e) => setNewLabel(e.target.value)} placeholder="e.g. Risk team – read only" /></Field>
                <Field label="Environment">
                  <Select value={newEnv} onChange={(e) => setNewEnv(e.target.value)}>
                    {["prod", "dev", "sandbox"].map((x) => <option key={x} value={x}>{x}</option>)}
                  </Select>
                </Field>
                <Button onClick={createKey}><Plus size={15} /> Create</Button>
              </div>
              {issued ? (
                <div style={{ marginTop: 14, padding: 12, borderRadius: 8, background: "var(--rm-success-bg)", border: "1px solid var(--rm-border)" }}>
                  <div style={{ fontSize: 12, color: "var(--rm-success)", fontWeight: 700, marginBottom: 6 }}>New {ROLE_LABEL[issued.role] ?? issued.role} key — copy it now, it won't be shown again</div>
                  <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                    <code style={{ flex: 1, fontSize: 12, padding: "8px 10px", background: "var(--rm-bg)", borderRadius: 6, border: "1px solid var(--rm-border)", overflowX: "auto" }}>{issued.plaintext}</code>
                    <Button variant="secondary" onClick={() => navigator.clipboard?.writeText(issued.plaintext)}><Copy size={14} /> Copy</Button>
                  </div>
                </div>
              ) : null}
            </Card>
          ) : null}

          <Card>
            <SectionTitle right={<span style={{ fontSize: 12, color: "var(--rm-muted)" }}>{activeKeys.length} active</span>}>API keys</SectionTitle>
            {activeKeys.length === 0 ? (
              <EmptyState icon={<KeyRound size={20} />} title="No active keys" hint="Issue a role-scoped key to get started." />
            ) : (
              <div style={{ overflowX: "auto" }}>
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
                  <thead>
                    <tr>{["Key", "Role", "Label", "Env", ""].map((h) => (
                      <th key={h} style={{ textAlign: "left", padding: "8px 6px", fontSize: 11, fontWeight: 700, color: "var(--rm-muted)", textTransform: "uppercase", letterSpacing: 0.4, borderBottom: "2px solid var(--rm-border)" }}>{h}</th>
                    ))}</tr>
                  </thead>
                  <tbody>
                    {activeKeys.map((k) => (
                      <tr key={k.kid} style={{ borderBottom: "1px solid var(--rm-border)" }}>
                        <td style={{ padding: "8px 6px", fontFamily: "monospace", fontSize: 12 }}>
                          {k.masked_key}{k.is_current ? <Badge tone="success">this key</Badge> : null}
                        </td>
                        <td style={{ padding: "8px 6px" }}><Badge tone={ROLE_TONE[k.role] ?? "neutral"}>{ROLE_LABEL[k.role] ?? k.role}</Badge></td>
                        <td style={{ padding: "8px 6px", color: "var(--rm-muted)" }}>{k.label || "—"}</td>
                        <td style={{ padding: "8px 6px", color: "var(--rm-muted)" }}>{k.environment || "prod"}</td>
                        <td style={{ padding: "8px 6px", textAlign: "right" }}>
                          {canManage && !k.is_current ? (
                            <button onClick={() => revoke(k.kid)} title="Revoke" style={{ background: "none", border: "none", cursor: "pointer", color: "var(--rm-muted)" }}><Trash2 size={15} /></button>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </div>

        {/* Roles reference */}
        <Card>
          <SectionTitle>Roles</SectionTitle>
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            {(roles?.roles ?? []).map((r) => (
              <div key={r.role} style={{ padding: "10px 12px", borderRadius: 8, border: "1px solid var(--rm-border)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
                  <Badge tone={ROLE_TONE[r.role] ?? "neutral"}>{ROLE_LABEL[r.role] ?? r.role}</Badge>
                </div>
                <div style={{ fontSize: 12.5, color: "var(--rm-text)", lineHeight: 1.5, marginBottom: 8 }}>{r.description}</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4 }}>
                  {r.capabilities.map((c) => (
                    <span key={c} style={{ fontSize: 10.5, fontFamily: "monospace", padding: "2px 7px", borderRadius: 999, background: "var(--rm-accent-bg)", color: "var(--rm-accent)" }}>{c}</span>
                  ))}
                </div>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
