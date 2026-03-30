"use client";

import * as React from "react";
import Link from "next/link";
import { THEMES } from "../../src/v3/theme";
import { useRuleMindStore } from "../../src/lib/store";

type TenantRecord = {
  id: string;
  name: string;
  plan: string;
  is_active: boolean;
};

type KeyRecord = {
  id: string;
  kid: string;
  masked_key: string;
  is_active: boolean;
};

export default function AdminHomePage() {
  const themeMode = useRuleMindStore((state) => state.themeMode);
  const apiBaseUrl = useRuleMindStore((state) => state.apiBaseUrl);
  const isMobile = useRuleMindStore((state) => state.isMobile);
  const theme = THEMES[themeMode];
  const [tenants, setTenants] = React.useState<TenantRecord[]>([]);
  const [selectedTenant, setSelectedTenant] = React.useState<TenantRecord | null>(null);
  const [keys, setKeys] = React.useState<KeyRecord[]>([]);
  const [error, setError] = React.useState<string | null>(null);

  const loadTenants = React.useCallback(async () => {
    const me = await fetch(apiBaseUrl + "/api/admin/v1/auth/me", { credentials: "include" });
    if (!me.ok) {
      window.location.href = "/admin/login";
      return;
    }
    const response = await fetch(apiBaseUrl + "/api/admin/v1/tenants", { credentials: "include" });
    if (!response.ok) {
      setError("Unable to load tenants.");
      return;
    }
    const body = (await response.json()) as TenantRecord[];
    setTenants(body);
    setSelectedTenant((current) => current ?? body[0] ?? null);
  }, [apiBaseUrl]);

  React.useEffect(() => {
    void loadTenants();
  }, [loadTenants]);

  React.useEffect(() => {
    if (!selectedTenant) {
      setKeys([]);
      return;
    }
    fetch(apiBaseUrl + "/api/admin/v1/tenants/" + selectedTenant.id + "/keys", { credentials: "include" })
      .then((response) => response.json())
      .then((body) => setKeys(body as KeyRecord[]))
      .catch(() => setKeys([]));
  }, [apiBaseUrl, selectedTenant]);

  return (
    <div style={{ padding: isMobile ? 12 : 24, display: "grid", gap: 18 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
        <div style={{ display: "grid", gap: 4 }}>
          <h1 style={{ margin: 0, fontSize: "var(--rm-fs-hero)", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>Platform Admin</h1>
          <div style={{ fontSize: "var(--rm-fs-body)", color: theme.muted }}>Tenants, plans, status, and API keys.</div>
        </div>
        <Link href="/admin/login" style={{ fontSize: "var(--rm-fs-body)", color: theme.accent }}>Switch account</Link>
      </div>
      {error ? <div style={{ padding: 12, borderRadius: 12, background: theme.dangerBg, color: theme.danger }}>{error}</div> : null}
      <div style={{ display: "grid", gridTemplateColumns: isMobile ? "1fr" : "320px 1fr", gap: 16 }}>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 14, overflow: "hidden" }}>
          <div style={{ padding: 14, borderBottom: "1px solid " + theme.border, fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>Tenants</div>
          {tenants.map((tenant) => (
            <button
              key={tenant.id}
              type="button"
              onClick={() => setSelectedTenant(tenant)}
              style={{
                width: "100%",
                border: "none",
                borderTop: "1px solid " + theme.border,
                background: selectedTenant?.id === tenant.id ? theme.accentBg : "transparent",
                color: theme.text,
                textAlign: "left",
                padding: 14,
                display: "grid",
                gap: 4,
                cursor: "pointer",
              }}
            >
              <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>{tenant.name}</div>
              <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{tenant.plan} · {tenant.is_active ? "active" : "disabled"}</div>
            </button>
          ))}
        </div>
        <div style={{ background: theme.card, border: "1px solid " + theme.border, borderRadius: 14, padding: 16, display: "grid", gap: 12 }}>
          {!selectedTenant ? (
            <div style={{ fontSize: "var(--rm-fs-body)", color: theme.dim }}>Select a tenant to view keys.</div>
          ) : (
            <>
              <div style={{ display: "grid", gap: 4 }}>
                <div style={{ fontSize: "var(--rm-fs-title)", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>{selectedTenant.name}</div>
                <div style={{ fontSize: "var(--rm-fs-body)", color: theme.muted }}>{selectedTenant.id}</div>
              </div>
              <div style={{ display: "grid", gap: 10 }}>
                {keys.map((key) => (
                  <div key={key.id} style={{ background: theme.hover, borderRadius: 12, padding: 12, border: "1px solid " + theme.border }}>
                    <div style={{ fontSize: "var(--rm-fs-body)", fontWeight: "var(--rm-fw-bold)" as unknown as number }}>{key.kid}</div>
                    <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted }}>{key.masked_key}</div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
