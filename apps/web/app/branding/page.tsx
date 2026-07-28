"use client";

import * as React from "react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore } from "../../src/lib/store";
import { THEMES } from "../../src/v3/theme";
import { type Branding, EMPTY_BRANDING, isColor, withBranding, logoText as resolveLogoText, brandName as resolveBrandName } from "../../src/v3/branding";

// Nav items an admin may hide. Dashboard/Settings/Branding are intentionally omitted
// so an admin can never lock themselves out of the console.
const HIDEABLE_NAV: Array<{ href: string; label: string; group: string }> = [
  { href: "/connectors", label: "Connectors", group: "Build" },
  { href: "/variables", label: "Variables", group: "Build" },
  { href: "/rules", label: "Rules", group: "Build" },
  { href: "/scorecards", label: "Scorecards", group: "Build" },
  { href: "/policies", label: "Policies", group: "Build" },
  { href: "/test-console", label: "Test Console", group: "Validate & ship" },
  { href: "/simulation", label: "Simulation", group: "Validate & ship" },
  { href: "/lifecycle", label: "Lifecycle", group: "Validate & ship" },
  { href: "/deploy", label: "Deploy", group: "Validate & ship" },
  { href: "/decision-explorer", label: "Decision Explorer", group: "Operate" },
  { href: "/review-queue", label: "Review Queue", group: "Operate" },
  { href: "/schedules", label: "Schedules", group: "Operate" },
  { href: "/audit", label: "Audit Logs", group: "Operate" },
  { href: "/exports", label: "Exports", group: "Operate" },
];

type AdminUser = { email: string };

export default function BrandingPage() {
  const { apiBaseUrl, apiKey, themeMode } = useRuleMindStore();
  const theme = THEMES[themeMode];

  const [authState, setAuthState] = React.useState<"checking" | "anon" | "admin">("checking");
  const [adminEmail, setAdminEmail] = React.useState("");

  React.useEffect(() => {
    (async () => {
      try {
        const res = await fetch(`${apiBaseUrl.replace(/\/$/, "")}/api/admin/v1/auth/me`, { credentials: "include", cache: "no-store" });
        if (res.ok) {
          const data = (await res.json()) as { user?: AdminUser };
          setAdminEmail(data.user?.email ?? "");
          setAuthState("admin");
        } else {
          setAuthState("anon");
        }
      } catch {
        setAuthState("anon");
      }
    })();
  }, [apiBaseUrl]);

  if (authState === "checking") {
    return <div style={{ padding: 24, color: theme.muted }}>Checking admin session…</div>;
  }
  if (authState === "anon") {
    return <AdminGate apiBaseUrl={apiBaseUrl} theme={theme} onAuthed={(email) => { setAdminEmail(email); setAuthState("admin"); }} />;
  }
  return <BrandingEditor apiBaseUrl={apiBaseUrl} apiKey={apiKey} theme={theme} themeMode={themeMode} adminEmail={adminEmail} />;
}

// ---- Admin sign-in gate (config is visible to admins only) --------------------

function AdminGate({ apiBaseUrl, theme, onAuthed }: { apiBaseUrl: string; theme: (typeof THEMES)["light"]; onAuthed: (email: string) => void }) {
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${apiBaseUrl.replace(/\/$/, "")}/api/admin/v1/auth/login`, {
        method: "POST",
        credentials: "include",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ email, password }),
      });
      if (!res.ok) {
        setError(res.status === 401 ? "Invalid email or password." : "Sign-in failed.");
        return;
      }
      onAuthed(email);
    } catch {
      setError("Unable to reach the admin API.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ maxWidth: 400, margin: "48px auto", padding: 24 }}>
      <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 24 }}>
        <h2 style={{ margin: "0 0 6px", color: theme.text, fontSize: 18 }}>Admin sign-in required</h2>
        <p style={{ margin: "0 0 18px", color: theme.muted, fontSize: 13, lineHeight: 1.5 }}>
          White-label branding is visible to platform admins only. Sign in with your admin
          account to change the CTA colour, backgrounds, brand name, and visible tabs.
        </p>
        <form onSubmit={submit} style={{ display: "grid", gap: 12 }}>
          <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" placeholder="admin@rulemind.local" autoComplete="username"
            style={inputStyle(theme)} />
          <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" placeholder="Password" autoComplete="current-password"
            style={inputStyle(theme)} />
          {error ? <div style={{ color: theme.danger, fontSize: 13 }}>{error}</div> : null}
          <button type="submit" disabled={busy} style={ctaStyle(theme, busy)}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
      </div>
    </div>
  );
}

// ---- Branding editor ----------------------------------------------------------

function BrandingEditor({
  apiBaseUrl, apiKey, theme, themeMode, adminEmail,
}: {
  apiBaseUrl: string; apiKey: string; theme: (typeof THEMES)["light"]; themeMode: "light" | "dark"; adminEmail: string;
}) {
  const [branding, setBranding] = React.useState<Branding>(EMPTY_BRANDING);
  const [loaded, setLoaded] = React.useState(false);
  const [saving, setSaving] = React.useState(false);
  const [saved, setSaved] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);

  React.useEffect(() => {
    (async () => {
      try {
        const settings = await apiJson<{ branding?: Branding }>(apiBaseUrl, "/api/v1/settings", {}, apiKey);
        setBranding({ ...EMPTY_BRANDING, ...(settings.branding ?? {}) });
      } catch (e) {
        setError(e instanceof Error ? e.message : "Unable to load settings.");
      } finally {
        setLoaded(true);
      }
    })();
  }, [apiBaseUrl, apiKey]);

  const patch = (p: Partial<Branding>) => { setBranding((b) => ({ ...b, ...p })); setSaved(false); };
  const toggleNav = (href: string) => {
    setSaved(false);
    setBranding((b) => {
      const hidden = new Set(b.hiddenNav ?? []);
      if (hidden.has(href)) hidden.delete(href); else hidden.add(href);
      return { ...b, hiddenNav: [...hidden] };
    });
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await apiJson(apiBaseUrl, "/api/v1/settings", { method: "PUT", body: JSON.stringify({ branding }) }, apiKey);
      setSaved(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Unable to save.");
    } finally {
      setSaving(false);
    }
  };

  const resetDefaults = () => { setBranding(EMPTY_BRANDING); setSaved(false); };

  if (!loaded) return <div style={{ padding: 24, color: theme.muted }}>Loading branding…</div>;

  const previewTheme = withBranding(THEMES[themeMode], branding);

  return (
    <div style={{ padding: 24, maxWidth: 980, margin: "0 auto" }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 18, flexWrap: "wrap", gap: 12 }}>
        <div>
          <h1 style={{ margin: 0, color: theme.text, fontSize: 22 }}>Branding</h1>
          <p style={{ margin: "4px 0 0", color: theme.muted, fontSize: 13 }}>
            Config-driven white-label theming for this workspace · admin <strong>{adminEmail}</strong>
          </p>
        </div>
        <div style={{ display: "flex", gap: 10 }}>
          <button onClick={resetDefaults} style={ghostStyle(theme)}>Reset to defaults</button>
          <button onClick={save} disabled={saving} style={ctaStyle(theme, saving)}>{saving ? "Saving…" : "Save branding"}</button>
        </div>
      </div>

      {error ? <div style={{ color: theme.danger, fontSize: 13, marginBottom: 12 }}>{error}</div> : null}
      {saved ? <div style={{ color: theme.success, fontSize: 13, marginBottom: 12 }}>Saved — reload any open tab to see the new branding everywhere.</div> : null}

      <div style={{ display: "grid", gridTemplateColumns: "minmax(0,1fr) 320px", gap: 20, alignItems: "start" }}>
        <div style={{ display: "grid", gap: 16 }}>
          <Section theme={theme} title="Identity">
            <Field theme={theme} label="Brand name" hint='Sidebar wordmark. Empty → "RuleMind".'>
              <input style={inputStyle(theme)} value={branding.brandName ?? ""} onChange={(e) => patch({ brandName: e.target.value })} placeholder="RuleMind" />
            </Field>
            <Field theme={theme} label="Logo initials" hint="1–2 characters shown in the square badge.">
              <input style={inputStyle(theme)} maxLength={2} value={branding.logoText ?? ""} onChange={(e) => patch({ logoText: e.target.value })} placeholder="R" />
            </Field>
          </Section>

          <Section theme={theme} title="Colours">
            <ColorField theme={theme} label="Accent / CTA" hint="Primary buttons, active nav, focus rings." value={branding.accent ?? ""} onChange={(v) => patch({ accent: v })} />
            <ColorField theme={theme} label="CTA text" hint="Text/icon colour on accent buttons." value={branding.accentText ?? ""} onChange={(v) => patch({ accentText: v })} />
            <ColorField theme={theme} label="Background" hint="App canvas background." value={branding.background ?? ""} onChange={(v) => patch({ background: v })} />
            <ColorField theme={theme} label="Sidebar" hint="Left navigation background." value={branding.sidebar ?? ""} onChange={(v) => patch({ sidebar: v })} />
          </Section>

          <Section theme={theme} title="Visible tabs" subtitle="Hide modules your teams don't use. Dashboard, Settings, and Branding always stay visible.">
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              {HIDEABLE_NAV.map((item) => {
                const hidden = (branding.hiddenNav ?? []).includes(item.href);
                return (
                  <label key={item.href} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 13, color: theme.text, cursor: "pointer", padding: "4px 2px" }}>
                    <input type="checkbox" checked={!hidden} onChange={() => toggleNav(item.href)} />
                    <span style={{ opacity: hidden ? 0.5 : 1 }}>{item.label}</span>
                  </label>
                );
              })}
            </div>
          </Section>
        </div>

        {/* Live preview */}
        <div style={{ position: "sticky", top: 16 }}>
          <div style={{ fontSize: 12, textTransform: "uppercase", letterSpacing: 1, color: theme.dim, marginBottom: 8 }}>Live preview</div>
          <div style={{ borderRadius: 12, overflow: "hidden", border: "1px solid " + previewTheme.border, display: "flex", height: 260, background: previewTheme.bg }}>
            <div style={{ width: 108, background: previewTheme.sidebar, borderRight: "1px solid " + previewTheme.border, padding: 10 }}>
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 12 }}>
                <span style={{ width: 20, height: 20, borderRadius: 6, background: previewTheme.accent, color: previewTheme.inverseText, display: "grid", placeItems: "center", fontSize: 11, fontWeight: 700 }}>
                  {resolveLogoText(branding)}
                </span>
                <span style={{ fontSize: 11, fontWeight: 700, color: previewTheme.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{resolveBrandName(branding)}</span>
              </div>
              {["Dashboard", "Rules", "Policies"].map((l, i) => (
                <div key={l} style={{ fontSize: 11, padding: "5px 6px", borderRadius: 6, marginBottom: 3, color: i === 0 ? previewTheme.accent : previewTheme.sidebarText, background: i === 0 ? previewTheme.sidebarActive : "transparent" }}>{l}</div>
              ))}
            </div>
            <div style={{ flex: 1, padding: 12 }}>
              <div style={{ fontSize: 12, fontWeight: 700, color: previewTheme.text, marginBottom: 8 }}>Decision review</div>
              <div style={{ background: previewTheme.card, border: "1px solid " + previewTheme.border, borderRadius: 8, padding: 10, marginBottom: 10 }}>
                <div style={{ fontSize: 10, color: previewTheme.muted }}>credit_score ≥ 700</div>
              </div>
              <button style={{ background: previewTheme.accent, color: previewTheme.inverseText, border: "none", borderRadius: 8, padding: "7px 12px", fontSize: 12, fontWeight: 600 }}>Approve</button>
            </div>
          </div>
          <p style={{ fontSize: 12, color: theme.dim, marginTop: 10, lineHeight: 1.5 }}>
            Leave a colour blank to inherit the active {themeMode} theme. Colours apply to every user of this workspace once saved.
          </p>
        </div>
      </div>
    </div>
  );
}

// ---- small presentational helpers --------------------------------------------

function Section({ theme, title, subtitle, children }: { theme: (typeof THEMES)["light"]; title: string; subtitle?: string; children: React.ReactNode }) {
  return (
    <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 12, padding: 18 }}>
      <div style={{ fontSize: 14, fontWeight: 700, color: theme.text, marginBottom: subtitle ? 2 : 12 }}>{title}</div>
      {subtitle ? <div style={{ fontSize: 12, color: theme.muted, marginBottom: 12 }}>{subtitle}</div> : null}
      <div style={{ display: "grid", gap: 14 }}>{children}</div>
    </div>
  );
}

function Field({ theme, label, hint, children }: { theme: (typeof THEMES)["light"]; label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "grid", gap: 5 }}>
      <span style={{ fontSize: 13, fontWeight: 600, color: theme.text }}>{label}</span>
      {hint ? <span style={{ fontSize: 12, color: theme.dim }}>{hint}</span> : null}
      {children}
    </label>
  );
}

function ColorField({ theme, label, hint, value, onChange }: { theme: (typeof THEMES)["light"]; label: string; hint?: string; value: string; onChange: (v: string) => void }) {
  const valid = isColor(value);
  return (
    <Field theme={theme} label={label} hint={hint}>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <input type="color" value={valid ? value : "#3b82f6"} onChange={(e) => onChange(e.target.value)}
          style={{ width: 38, height: 34, border: "1px solid " + theme.border, borderRadius: 8, background: theme.input, padding: 2, cursor: "pointer" }} />
        <input value={value} onChange={(e) => onChange(e.target.value)} placeholder="inherit theme" style={{ ...inputStyle(theme), flex: 1 }} />
        {value && !valid ? <span style={{ fontSize: 11, color: theme.warning }}>hex only</span> : null}
        {value ? <button onClick={() => onChange("")} title="Clear" style={{ ...ghostStyle(theme), padding: "6px 8px" }}>✕</button> : null}
      </div>
    </Field>
  );
}

function inputStyle(theme: (typeof THEMES)["light"]): React.CSSProperties {
  return { padding: "9px 11px", borderRadius: 8, border: "1px solid " + theme.border, background: theme.input, color: theme.text, fontSize: 13, outline: "none", width: "100%" };
}
function ctaStyle(theme: (typeof THEMES)["light"], busy: boolean): React.CSSProperties {
  return { background: theme.accent, color: theme.inverseText, border: "none", borderRadius: 8, padding: "9px 16px", fontSize: 13, fontWeight: 600, cursor: busy ? "wait" : "pointer", opacity: busy ? 0.7 : 1 };
}
function ghostStyle(theme: (typeof THEMES)["light"]): React.CSSProperties {
  return { background: "transparent", color: theme.muted, border: "1px solid " + theme.border, borderRadius: 8, padding: "9px 14px", fontSize: 13, fontWeight: 600, cursor: "pointer" };
}
