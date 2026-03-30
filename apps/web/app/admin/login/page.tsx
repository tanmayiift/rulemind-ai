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
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
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
          <h1 style={{ margin: 0, fontSize: "var(--rm-fs-hero)", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>RuleMind Admin</h1>
          <div style={{ fontSize: "var(--rm-fs-body)", color: theme.muted }}>Platform admin login for tenant lifecycle and API key management.</div>
        </div>
        {error ? <div style={{ padding: 12, borderRadius: 12, background: theme.dangerBg, color: theme.danger }}>{error}</div> : null}
        <label style={{ display: "grid", gap: 4 }}>
          <span style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-normal)" as unknown as number, color: theme.muted }}>Email</span>
          <input value={email} onChange={(event) => setEmail(event.target.value)} placeholder="Email" style={adminInput(theme)} />
        </label>
        <label style={{ display: "grid", gap: 4 }}>
          <span style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-normal)" as unknown as number, color: theme.muted }}>Password</span>
          <input type="password" value={password} onChange={(event) => setPassword(event.target.value)} placeholder="Password" style={adminInput(theme)} />
        </label>
        <button type="submit" style={{ border: "none", background: theme.accent, color: theme.inverseText, borderRadius: 12, padding: "12px 16px", fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, cursor: "pointer" }}>
          Sign in
        </button>
      </form>
    </div>
  );
}

function adminInput(theme: typeof THEMES.light): React.CSSProperties {
  return { width: "100%", borderRadius: 12, border: "1px solid " + theme.border, background: theme.input, color: theme.text, padding: "11px 12px", fontSize: "var(--rm-fs-heading)" };
}
