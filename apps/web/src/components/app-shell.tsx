"use client";

import * as React from "react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { Menu, Moon, Sun, X } from "lucide-react";
import { useRuleMindStore } from "../lib/store";
import { ENVIRONMENT_ACCENT, THEMES, themeStyleBlock } from "../v3/theme";
import { PageIcon } from "../v3/icons";

const NAVIGATION: ReadonlyArray<{
  group: string;
  items: Array<{ href: Route; label: string; icon: string }>;
}> = [
  {
    group: "Overview",
    items: [{ href: "/" as Route, label: "Dashboard", icon: "dashboard" }],
  },
  {
    group: "Build",
    items: [
      { href: "/connectors" as Route, label: "Connectors", icon: "connectors" },
      { href: "/variables" as Route, label: "Variables", icon: "variables" },
      { href: "/rules" as Route, label: "Rules", icon: "rules" },
      { href: "/scorecards" as Route, label: "Scorecards", icon: "scorecards" },
      { href: "/policies" as Route, label: "Policies", icon: "policies" },
    ],
  },
  {
    group: "Validate & ship",
    items: [
      { href: "/test-console" as Route, label: "Test Console", icon: "testing" },
      { href: "/deploy" as Route, label: "Deploy", icon: "deploy" },
    ],
  },
  {
    group: "Operate",
    items: [
      { href: "/review-queue" as Route, label: "Review Queue", icon: "audit" },
      { href: "/schedules" as Route, label: "Schedules", icon: "deploy" },
      { href: "/audit" as Route, label: "Audit Logs", icon: "audit" },
      { href: "/exports" as Route, label: "Exports", icon: "exports" },
    ],
  },
  {
    group: "System",
    items: [{ href: "/settings" as Route, label: "Settings", icon: "settings" }],
  },
];

const PAGE_COPY: Record<string, { title: string; subtitle: string }> = {
  "/": { title: "RuleMind", subtitle: "Generic decisioning engine for variables, rules, scorecards, policies, and audit trace." },
  "/connectors": { title: "Connectors", subtitle: "Data source connectivity, schemas, auth setup, and sample payload controls." },
  "/variables": { title: "Variables", subtitle: "Sandboxed Python feature creation with testing, lifecycle, and dependency visibility." },
  "/rules": { title: "Rules", subtitle: "Simple and advanced rule authoring with nested logic and inline execution results." },
  "/scorecards": { title: "Scorecards", subtitle: "Points-based scoring with editable ranges and live factor breakdown." },
  "/policies": { title: "Policies", subtitle: "Execution pipelines across connectors, rules, scorecards, and final decisions." },
  "/test-console": { title: "Test Console", subtitle: "Single-run, batch simulation, and production API preview workflows." },
  "/review-queue": { title: "Review Queue", subtitle: "Pending human-in-the-loop reviews with decision context and queue state." },
  "/schedules": { title: "Schedules", subtitle: "Cron-driven policy runs, payload sources, and batch execution history." },
  "/deploy": { title: "Deploy", subtitle: "Promote tested assets through DEV, UAT, and PROD with audit-safe controls." },
  "/audit": { title: "Audit Logs", subtitle: "Decision history, promotion history, and operational error telemetry." },
  "/exports": { title: "Exports", subtitle: "Download, validate, and restore complete RuleMind configurations." },
  "/settings": { title: "Settings", subtitle: "Persist API, engine, audit, source, SDK, and environment defaults." },
};

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const {
    apiBaseUrl,
    environment,
    setEnvironment,
    themeMode,
    toggleThemeMode,
    sidebarOpen,
    toggleSidebar,
    isMobile,
    mobileMenuOpen,
    setIsMobile,
    setMobileMenuOpen,
    setSidebarOpen,
  } = useRuleMindStore();
  const theme = THEMES[themeMode];
  const page = PAGE_COPY[pathname ?? "/"] ?? PAGE_COPY["/"];

  React.useEffect(() => {
    document.documentElement.dataset.theme = themeMode;
    document.documentElement.setAttribute("style", themeStyleBlock(themeMode));
  }, [themeMode]);

  React.useEffect(() => {
    const mq = window.matchMedia("(max-width: 768px)");
    const handler = (e: MediaQueryListEvent | MediaQueryList) => {
      setIsMobile(e.matches);
      if (e.matches) setSidebarOpen(false);
    };
    handler(mq);
    mq.addEventListener("change", handler as (e: MediaQueryListEvent) => void);
    return () => mq.removeEventListener("change", handler as (e: MediaQueryListEvent) => void);
  }, [setIsMobile, setSidebarOpen]);

  if ((pathname ?? "").startsWith("/admin")) {
    return (
      <div style={{ minHeight: "100vh", background: theme.bg, color: theme.text }}>
        {children}
      </div>
    );
  }

  const sidebarVisible = isMobile ? mobileMenuOpen : true;
  const sidebarWidth = isMobile ? 260 : sidebarOpen ? 224 : 64;
  const showLabels = isMobile ? true : sidebarOpen;

  return (
    <div
      style={{
        minHeight: "100vh",
        display: "grid",
        gridTemplateColumns: isMobile ? "1fr" : `${sidebarOpen ? 224 : 64}px minmax(0, 1fr)`,
        background: theme.bg,
        color: theme.text,
        transition: isMobile ? "none" : "grid-template-columns 0.2s ease",
      }}
    >
      {/* Mobile backdrop */}
      {isMobile && mobileMenuOpen && (
        <div
          onClick={() => setMobileMenuOpen(false)}
          style={{
            position: "fixed",
            inset: 0,
            background: "rgba(0,0,0,0.45)",
            zIndex: 999,
          }}
        />
      )}

      {/* Sidebar */}
      {sidebarVisible && (
        <aside
          data-testid="sidebar-root"
          style={{
            background: theme.sidebar,
            borderRight: "1px solid " + theme.border,
            display: "flex",
            flexDirection: "column",
            overflow: "hidden",
            ...(isMobile
              ? {
                  position: "fixed",
                  top: 0,
                  left: 0,
                  bottom: 0,
                  width: sidebarWidth,
                  zIndex: 1000,
                  boxShadow: "4px 0 24px rgba(0,0,0,0.18)",
                }
              : {}),
          }}
        >
          <div
            style={{
              minHeight: 56,
              padding: showLabels ? "14px 16px" : "14px 12px",
              display: "flex",
              alignItems: "center",
              gap: 10,
              borderBottom: "1px solid " + theme.border,
            }}
          >
            <button
              type="button"
              onClick={() => {
                if (isMobile) setMobileMenuOpen(false);
                else toggleSidebar();
              }}
              data-testid="sidebar-toggle"
              aria-label={showLabels ? "Collapse sidebar" : "Expand sidebar"}
              style={{
                border: "none",
                background: "transparent",
                color: theme.sidebarGroup,
                cursor: "pointer",
                padding: 4,
                borderRadius: 8,
                display: "grid",
                placeItems: "center",
              }}
            >
              {showLabels ? <X size={18} /> : <Menu size={18} />}
            </button>
            {showLabels ? (
              <div style={{ display: "grid", gap: 2 }}>
                <span style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text, letterSpacing: -0.3 }}>RuleMind</span>
                <span style={{ fontSize: "var(--rm-fs-caption)", color: theme.sidebarGroup }}>Enterprise decisioning</span>
              </div>
            ) : null}
          </div>

          <nav style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
            {NAVIGATION.map((group) => (
              <div key={group.group} style={{ marginBottom: 10 }}>
                {showLabels ? (
                  <div
                    style={{
                      padding: "8px 8px 4px",
                      color: theme.sidebarGroup,
                      fontSize: "var(--rm-fs-caption)",
                      fontWeight: "var(--rm-fw-bold)" as unknown as number,
                      letterSpacing: 1.2,
                      textTransform: "uppercase",
                    }}
                  >
                    {group.group}
                  </div>
                ) : null}
                {group.items.map((item) => {
                  const active = pathname === item.href;
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      data-testid={"nav-" + item.label.toLowerCase().replace(/\s+/g, "-")}
                      onClick={() => {
                        if (isMobile) setMobileMenuOpen(false);
                      }}
                      style={{
                        display: "flex",
                        alignItems: "center",
                        justifyContent: showLabels ? "flex-start" : "center",
                        gap: 10,
                        padding: isMobile ? "12px 14px" : showLabels ? "8px 10px" : "10px 0",
                        marginBottom: 4,
                        borderRadius: 10,
                        background: active ? theme.sidebarActive : "transparent",
                        color: active ? theme.accent : theme.sidebarText,
                        fontSize: "var(--rm-fs-body)",
                        fontWeight: active ? "var(--rm-fw-semibold)" as unknown as number : "var(--rm-fw-normal)" as unknown as number,
                        opacity: active ? 1 : 0.82,
                      }}
                    >
                      <PageIcon name={item.icon} size={16} color={active ? theme.accent : theme.sidebarText} />
                      {showLabels ? item.label : null}
                    </Link>
                  );
                })}
              </div>
            ))}
          </nav>

          {showLabels ? (
            <div
              style={{
                padding: "12px 16px",
                borderTop: "1px solid " + theme.border,
                fontSize: "var(--rm-fs-caption)",
                color: theme.sidebarGroup,
              }}
            >
              v4 enterprise shell
            </div>
          ) : null}
        </aside>
      )}

      <div style={{ minWidth: 0, display: "flex", flexDirection: "column" }}>
        <header
          data-testid="topbar-root"
          style={{
            minHeight: 58,
            padding: isMobile ? "0 12px" : "0 18px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: isMobile ? 8 : 16,
            background: theme.header,
            borderBottom: "1px solid " + theme.border,
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 10, minWidth: 0 }}>
            {isMobile && (
              <button
                type="button"
                onClick={() => setMobileMenuOpen(true)}
                aria-label="Open menu"
                style={{
                  border: "none",
                  background: "transparent",
                  color: theme.text,
                  cursor: "pointer",
                  padding: 6,
                  borderRadius: 8,
                  display: "grid",
                  placeItems: "center",
                  flexShrink: 0,
                }}
              >
                <Menu size={20} />
              </button>
            )}
            <div style={{ display: "grid", gap: 2, minWidth: 0 }}>
              <div style={{ fontSize: "var(--rm-fs-title)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text, letterSpacing: -0.4, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{page.title}</div>
              {!isMobile && (
                <div style={{ fontSize: "var(--rm-fs-small)", color: theme.muted, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{page.subtitle} · API {apiBaseUrl}</div>
              )}
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: isMobile ? 6 : 10, flexShrink: 0 }}>
            <div style={{ display: "flex", gap: 4 }}>
              {(["dev", "uat", "prod"] as const).map((item) => (
                <button
                  key={item}
                  type="button"
                  data-testid={"env-" + item}
                  onClick={() => setEnvironment(item)}
                  style={{
                    padding: isMobile ? "8px 10px" : "5px 13px",
                    borderRadius: 7,
                    border: environment === item ? "none" : "1px solid " + theme.border,
                    background: environment === item ? ENVIRONMENT_ACCENT[item] : "transparent",
                    color: environment === item ? theme.inverseText : theme.muted,
                    fontSize: "var(--rm-fs-caption)",
                    fontWeight: "var(--rm-fw-bold)" as unknown as number,
                    cursor: "pointer",
                    letterSpacing: 0.45,
                  }}
                >
                  {item.toUpperCase()}
                </button>
              ))}
            </div>

            {!isMobile && (
              <button
                type="button"
                data-testid="theme-toggle"
                onClick={toggleThemeMode}
                aria-label="Toggle dark mode"
                style={{
                  background: theme.hover,
                  border: "1px solid " + theme.border,
                  borderRadius: 8,
                  padding: "6px 11px",
                  color: theme.text,
                  fontSize: "var(--rm-fs-small)",
                  fontWeight: "var(--rm-fw-normal)" as unknown as number,
                  cursor: "pointer",
                  display: "inline-flex",
                  alignItems: "center",
                  gap: 6,
                }}
              >
                {themeMode === "dark" ? <Sun size={14} /> : <Moon size={14} />}
                {themeMode === "dark" ? "Light" : "Dark"}
              </button>
            )}

            <div
              onClick={isMobile ? toggleThemeMode : undefined}
              style={{
                width: 32,
                height: 32,
                borderRadius: "50%",
                display: "grid",
                placeItems: "center",
                background: themeMode === "dark" ? "linear-gradient(135deg,#60a5fa,#a78bfa)" : "linear-gradient(135deg,#3b82f6,#7c3aed)",
                color: theme.inverseText,
                fontSize: "var(--rm-fs-body)",
                fontWeight: "var(--rm-fw-semibold)" as unknown as number,
                cursor: isMobile ? "pointer" : "default",
              }}
            >
              U
            </div>
          </div>
        </header>

        <main style={{ flex: 1, minHeight: 0, overflow: "auto" }}>{children}</main>
      </div>
    </div>
  );
}
