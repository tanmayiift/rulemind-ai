"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { THEMES } from "../../../src/v3/theme";
import { useRuleMindStore } from "../../../src/lib/store";

export default function AdminLoginPage() {
  const router = useRouter();
  const themeMode = useRuleMindStore((state) => state.themeMode);
  const apiBaseUrl = useRuleMindStore((state) => state.apiBaseUrl);
  const theme = THEMES[themeMode];
  const [email, setEmail] = React.useState("admin@rulemind.local");
  const [password, setPassword] = React.useState("rulemind-admin");
  const [error, setError] = React.useState<string | null>(null);

  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}>
      <form
        onSubmit={async (event) => {
          event.preventDefault();
          setError(null);
          const response = await fetch(apiBaseUrl + "/api/admin/v1/auth/login", {
            method: "POST",
            credentials: "include",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ email, password }),
          });
          if (!response.ok) {
            const body = await response.json().catch(() => ({}));
            setError(body.detail ?? "Unable to log in.");
            return;
          }
          router.push("/admin");
        }}
        style={{ width: "100%", maxWidth: 420, background: theme.card, border: "1px solid " + theme.border, borderRadius: 18, padding: 24, display: "grid", gap: 14 }}
      >
        <div style={{ display: "grid", gap: 4 }}>
          <h1 style={{ margin: 0, fontSize: 24, fontWeight: 800 }}>RuleMind Admin</h1>
          <div style={{ fontSize: 12, color: theme.muted }}>Platform admin login for tenant lifecycle and API key management.</div>
        </div>
        {error ? <div style={{ padding: 12, borderRadius: 12, background: theme.dangerBg, color: theme.danger }}>{error}</div> : null}
        <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="Email" style={adminInput(theme)} />
        <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password" style={adminInput(theme)} />
        <button type="submit" style={{ border: "none", background: theme.accent, color: theme.inverseText, borderRadius: 12, padding: "12px 16px", fontSize: 13, fontWeight: 800, cursor: "pointer" }}>
          Sign in
        </button>
      </form>
    </div>
  );
}

function adminInput(theme: typeof THEMES.light): React.CSSProperties {
  return { width: "100%", borderRadius: 12, border: "1px solid " + theme.border, background: theme.input, color: theme.text, padding: "11px 12px", fontSize: 13 };
}
