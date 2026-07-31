"use client";

import * as React from "react";
import { Plus, Trash2, Copy, ShieldCheck, KeyRound, UserPlus, Users, Building2 } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { Button, Card, Field, Input, Select, Badge, EmptyState, PageHeader, SectionTitle } from "../../src/v3/ui";

type ApiKey = { id: string; kid: string; masked_key: string; role: string; label?: string; environment?: string; is_active: boolean; is_current?: boolean; created_at?: string };
type SsoConfig = {
  provider: string; enabled: boolean; issuer?: string; client_id?: string; client_secret_set?: boolean;
  redirect_uri?: string; authorization_endpoint?: string; token_endpoint?: string; jwks_uri?: string;
  sp_entity_id?: string; sso_url?: string; acs_url?: string; idp_entity_id?: string; idp_cert?: string;
  allowed_domains?: string[]; default_role?: string; jit_provisioning?: boolean;
};
type Member = { id: string; email: string; name: string; role: string; is_active: boolean; has_password: boolean; auth_provider: string; last_login_at?: string | null };
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
  const [members, setMembers] = React.useState<Member[]>([]);
  const [mEmail, setMEmail] = React.useState("");
  const [mName, setMName] = React.useState("");
  const [mRole, setMRole] = React.useState("viewer");
  const [mPassword, setMPassword] = React.useState("");
  const [sso, setSso] = React.useState<SsoConfig | null>(null);
  const [ssoSecret, setSsoSecret] = React.useState("");
  const [ssoSaved, setSsoSaved] = React.useState(false);

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
      try { setMembers(await apiJson<Member[]>(apiBaseUrl, "/api/v1/access/members", {}, apiKey)); } catch { /* read may be limited */ }
      try { setSso(await apiJson<SsoConfig>(apiBaseUrl, "/api/v1/access/sso", {}, apiKey)); } catch { /* read may be limited */ }
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

  const createMember = async () => {
    setError(null);
    try {
      await apiJson(apiBaseUrl, "/api/v1/access/members", {
        method: "POST",
        body: JSON.stringify({ email: mEmail, name: mName || undefined, role: mRole, password: mPassword || undefined }),
      }, apiKey);
      setMEmail(""); setMName(""); setMPassword("");
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Could not add member."); }
  };

  const changeMemberRole = async (id: string, role: string) => {
    setError(null);
    try {
      await apiJson(apiBaseUrl, `/api/v1/access/members/${id}`, { method: "PATCH", body: JSON.stringify({ role }) }, apiKey);
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Role change failed."); }
  };

  const deactivateMember = async (id: string) => {
    setError(null);
    try {
      await apiJson(apiBaseUrl, `/api/v1/access/members/${id}`, { method: "DELETE" }, apiKey);
      await load();
    } catch (e) { setError(e instanceof Error ? e.message : "Deactivate failed."); }
  };

  const saveSso = async () => {
    if (!sso) return;
    setError(null); setSsoSaved(false);
    const body: Record<string, unknown> = {
      provider: sso.provider, enabled: sso.enabled, default_role: sso.default_role, jit_provisioning: sso.jit_provisioning,
      allowed_domains: sso.allowed_domains ?? [],
      issuer: sso.issuer, client_id: sso.client_id, redirect_uri: sso.redirect_uri,
      authorization_endpoint: sso.authorization_endpoint, token_endpoint: sso.token_endpoint, jwks_uri: sso.jwks_uri,
      sp_entity_id: sso.sp_entity_id, sso_url: sso.sso_url, acs_url: sso.acs_url, idp_entity_id: sso.idp_entity_id, idp_cert: sso.idp_cert,
    };
    if (ssoSecret) body.client_secret = ssoSecret;
    try {
      setSso(await apiJson<SsoConfig>(apiBaseUrl, "/api/v1/access/sso", { method: "PUT", body: JSON.stringify(body) }, apiKey));
      setSsoSecret(""); setSsoSaved(true);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not save SSO config."); }
  };
  const patchSso = (patch: Partial<SsoConfig>) => setSso((s) => (s ? { ...s, ...patch } : s));

  const activeKeys = keys.filter((k) => k.is_active);
  const assignable = roles?.assignable ?? [];

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

      {/* Team members (human accounts with roles) */}
      <Card>
        <SectionTitle
          right={<span style={{ fontSize: 12, color: "var(--rm-muted)" }}>{members.filter((m) => m.is_active).length} active</span>}
        >
          <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><Users size={15} /> Team members</span>
        </SectionTitle>
        <div style={{ fontSize: 12.5, color: "var(--rm-muted)", marginBottom: 12 }}>
          People who sign in to this workspace. Each member carries a role; they log in with a password or an emailed one-time code at <code style={{ fontFamily: "monospace" }}>/login</code>.
        </div>
        {canManage ? (
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 150px 1fr auto", gap: 10, alignItems: "end", marginBottom: 14 }}>
            <Field label="Email"><Input value={mEmail} onChange={(e) => setMEmail(e.target.value)} placeholder="teammate@company.com" /></Field>
            <Field label="Name"><Input value={mName} onChange={(e) => setMName(e.target.value)} placeholder="Full name" /></Field>
            <Field label="Role">
              <Select value={mRole} onChange={(e) => setMRole(e.target.value)}>
                {assignable.map((r) => <option key={r} value={r}>{ROLE_LABEL[r] ?? r}</option>)}
              </Select>
            </Field>
            <Field label="Temp password (optional)"><Input type="password" value={mPassword} onChange={(e) => setMPassword(e.target.value)} placeholder="blank = OTP-only" /></Field>
            <Button onClick={createMember} disabled={!mEmail}><UserPlus size={15} /> Add</Button>
          </div>
        ) : null}
        {members.length === 0 ? (
          <EmptyState icon={<Users size={20} />} title="No members yet" hint="Add a teammate so they can sign in with their own role." />
        ) : (
          <div style={{ overflowX: "auto" }}>
            <table style={{ width: "100%", borderCollapse: "collapse", fontSize: 13 }}>
              <thead>
                <tr>{["Member", "Role", "Sign-in", "Status", ""].map((h) => (
                  <th key={h} style={{ textAlign: "left", padding: "8px 6px", fontSize: 11, fontWeight: 700, color: "var(--rm-muted)", textTransform: "uppercase", letterSpacing: 0.4, borderBottom: "2px solid var(--rm-border)" }}>{h}</th>
                ))}</tr>
              </thead>
              <tbody>
                {members.map((m) => (
                  <tr key={m.id} style={{ borderBottom: "1px solid var(--rm-border)", opacity: m.is_active ? 1 : 0.5 }}>
                    <td style={{ padding: "8px 6px" }}>
                      <div style={{ fontWeight: 600 }}>{m.name}</div>
                      <div style={{ fontSize: 12, color: "var(--rm-muted)" }}>{m.email}</div>
                    </td>
                    <td style={{ padding: "8px 6px" }}>
                      {canManage && m.is_active ? (
                        <Select value={m.role} onChange={(e) => changeMemberRole(m.id, e.target.value)} style={{ minWidth: 130 }}>
                          {assignable.map((r) => <option key={r} value={r}>{ROLE_LABEL[r] ?? r}</option>)}
                        </Select>
                      ) : <Badge tone={ROLE_TONE[m.role] ?? "neutral"}>{ROLE_LABEL[m.role] ?? m.role}</Badge>}
                    </td>
                    <td style={{ padding: "8px 6px", color: "var(--rm-muted)", fontSize: 12 }}>{m.has_password ? "Password + OTP" : "OTP only"}</td>
                    <td style={{ padding: "8px 6px" }}>{m.is_active ? <Badge tone="success">Active</Badge> : <Badge tone="neutral">Disabled</Badge>}</td>
                    <td style={{ padding: "8px 6px", textAlign: "right" }}>
                      {canManage && m.is_active ? (
                        <button onClick={() => deactivateMember(m.id)} title="Deactivate" style={{ background: "none", border: "none", cursor: "pointer", color: "var(--rm-muted)" }}><Trash2 size={15} /></button>
                      ) : null}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      {/* Enterprise SSO (OIDC / SAML) */}
      {sso ? (
        <Card>
          <SectionTitle
            right={<Badge tone={sso.enabled ? "success" : "neutral"}>{sso.enabled ? "Enabled" : "Off"}</Badge>}
          >
            <span style={{ display: "inline-flex", alignItems: "center", gap: 8 }}><Building2 size={15} /> Single sign-on</span>
          </SectionTitle>
          <div style={{ fontSize: 12.5, color: "var(--rm-muted)", marginBottom: 14 }}>
            Let members sign in through your identity provider. New users are provisioned just-in-time at the default role you choose below.
          </div>
          {!canManage ? (
            <div style={{ fontSize: 12.5, color: "var(--rm-muted)" }}>Read-only — ask an admin to configure SSO.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
              <div style={{ display: "grid", gridTemplateColumns: "160px 160px 1fr", gap: 10, alignItems: "end" }}>
                <Field label="Protocol">
                  <Select value={sso.provider} onChange={(e) => patchSso({ provider: e.target.value })}>
                    <option value="oidc">OIDC (OpenID Connect)</option>
                    <option value="saml">SAML 2.0</option>
                  </Select>
                </Field>
                <Field label="Default role for new users">
                  <Select value={sso.default_role ?? "viewer"} onChange={(e) => patchSso({ default_role: e.target.value })}>
                    {assignable.map((r) => <option key={r} value={r}>{ROLE_LABEL[r] ?? r}</option>)}
                  </Select>
                </Field>
                <Field label="Allowed email domains (comma-separated; blank = any)">
                  <Input value={(sso.allowed_domains ?? []).join(", ")}
                    onChange={(e) => patchSso({ allowed_domains: e.target.value.split(",").map((d) => d.trim()).filter(Boolean) })}
                    placeholder="acme.com, acme.co.uk" />
                </Field>
              </div>

              {sso.provider === "oidc" ? (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                  <Field label="Issuer URL"><Input value={sso.issuer ?? ""} onChange={(e) => patchSso({ issuer: e.target.value })} placeholder="https://login.company.com" /></Field>
                  <Field label="Redirect URI"><Input value={sso.redirect_uri ?? ""} onChange={(e) => patchSso({ redirect_uri: e.target.value })} placeholder="https://app.rulemind.com/login" /></Field>
                  <Field label="Client ID"><Input value={sso.client_id ?? ""} onChange={(e) => patchSso({ client_id: e.target.value })} /></Field>
                  <Field label={`Client secret${sso.client_secret_set ? " (set — leave blank to keep)" : ""}`}>
                    <Input type="password" value={ssoSecret} onChange={(e) => setSsoSecret(e.target.value)} placeholder={sso.client_secret_set ? "••••••••" : ""} />
                  </Field>
                </div>
              ) : (
                <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 10 }}>
                  <Field label="SP entity ID"><Input value={sso.sp_entity_id ?? ""} onChange={(e) => patchSso({ sp_entity_id: e.target.value })} placeholder="rulemind" /></Field>
                  <Field label="IdP SSO URL"><Input value={sso.sso_url ?? ""} onChange={(e) => patchSso({ sso_url: e.target.value })} placeholder="https://idp.company.com/sso" /></Field>
                  <Field label="ACS URL (this app)"><Input value={sso.acs_url ?? ""} onChange={(e) => patchSso({ acs_url: e.target.value })} placeholder="https://app.rulemind.com/api/v1/auth/sso/saml/acs" /></Field>
                  <Field label="IdP entity ID"><Input value={sso.idp_entity_id ?? ""} onChange={(e) => patchSso({ idp_entity_id: e.target.value })} /></Field>
                  <div style={{ gridColumn: "1 / -1" }}>
                    <Field label="IdP signing certificate (X.509 / PEM)">
                      <textarea value={sso.idp_cert ?? ""} onChange={(e) => patchSso({ idp_cert: e.target.value })}
                        rows={4} className="rm-input rm-mono" style={{ resize: "vertical", width: "100%" }} placeholder="-----BEGIN CERTIFICATE-----" />
                    </Field>
                  </div>
                </div>
              )}

              <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 13, cursor: "pointer" }}>
                  <input type="checkbox" checked={sso.enabled} onChange={(e) => patchSso({ enabled: e.target.checked })} /> Enable SSO login
                </label>
                <label style={{ display: "inline-flex", alignItems: "center", gap: 8, fontSize: 13, cursor: "pointer" }}>
                  <input type="checkbox" checked={sso.jit_provisioning ?? true} onChange={(e) => patchSso({ jit_provisioning: e.target.checked })} /> Just-in-time provision new users
                </label>
                <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 10 }}>
                  {ssoSaved ? <span style={{ fontSize: 12, color: "var(--rm-success)" }}>Saved</span> : null}
                  <Button onClick={saveSso}>Save SSO</Button>
                </div>
              </div>
            </div>
          )}
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
