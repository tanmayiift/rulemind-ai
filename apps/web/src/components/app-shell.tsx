"use client";

import * as React from "react";
import type { Route } from "next";
import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { Menu, Moon, Sun, X, Search } from "lucide-react";
import { CommandPalette, type Command } from "../v3/command-palette";
import { AiCopilotFab } from "./ai-copilot-fab";
import { useRuleMindStore } from "../lib/store";
import { ApiError, apiJson } from "../lib/api";
import { ENVIRONMENT_ACCENT, THEMES, themeStyleBlock } from "../v3/theme";
import {
  type Branding,
  withBranding,
  brandingStyleBlock,
  brandName as resolveBrandName,
  logoText as resolveLogoText,
  isNavHidden,
} from "../v3/branding";
import { PageIcon } from "../v3/icons";

const NAVIGATION: ReadonlyArray<{
  group: string;
  items: Array<{ href: Route; label: string; icon: string }>;
}> = [
  {
    group: "Overview",
    items: [
      { href: "/" as Route, label: "Dashboard", icon: "dashboard" },
      { href: "/onboarding" as Route, label: "Get started", icon: "testing" },
      { href: "/ai" as Route, label: "AI Copilot", icon: "variables" },
    ],
  },
  {
    group: "Build",
    items: [
      // The policy canvas. Was "Build Studio" (/studio), which has been merged into Workflow
      // Builder; /studio now redirects here. Workflow Builder moved up to take its slot as the
      // first thing under Build, and its old duplicate entry lower down is gone.
      { href: "/workflow-builder" as Route, label: "Workflow Builder", icon: "policies" },
      { href: "/connectors" as Route, label: "Connectors", icon: "connectors" },
      { href: "/variables" as Route, label: "Variables", icon: "variables" },
      { href: "/rules" as Route, label: "Rules", icon: "rules" },
      { href: "/decision-tables" as Route, label: "Decision Tables", icon: "rules" },
      { href: "/scorecards" as Route, label: "Scorecards", icon: "scorecards" },
      { href: "/models" as Route, label: "ML Models", icon: "variables" },
      // Model Sets sits directly under ML Models because it is a grouping OF them. It had a
      // PAGE_COPY entry and no route or link at all, so a fully tested 7-endpoint backend
      // (routing between candidate models, and comparing them on one input) was reachable only
      // by calling the API by hand.
      { href: "/model-sets" as Route, label: "Model Sets", icon: "variables" },
      { href: "/evaluations" as Route, label: "Model Evaluation", icon: "testing" },
      // Policies were reachable only by typing the URL, which is the worst possible gap: a
      // policy is the thing that CHAINS connectors, variables, rules and scorecards into a
      // decision, the dashboard counts them on its front page, and nothing linked to them.
      { href: "/policies" as Route, label: "Policies", icon: "policies" },
      // In Build, not Operate: a journey is authored alongside the policy that decides it, and
      // the screens ARE part of the decision's input contract.
      { href: "/journeys" as Route, label: "Journeys", icon: "connectors" },
    ],
  },
  {
    group: "Validate & ship",
    items: [
      { href: "/test-console" as Route, label: "Test Console", icon: "testing" },
      { href: "/api-console" as Route, label: "API Console", icon: "connectors" },
      { href: "/simulation" as Route, label: "Simulation", icon: "testing" },
      { href: "/ab-tests" as Route, label: "A/B Tests", icon: "testing" },
      { href: "/lifecycle" as Route, label: "Lifecycle", icon: "policies" },
      { href: "/collab" as Route, label: "Collab Editor", icon: "policies" },
      { href: "/deploy" as Route, label: "Deploy", icon: "deploy" },
    ],
  },
  {
    group: "Operate",
    items: [
      { href: "/decision-explorer" as Route, label: "Decision Explorer", icon: "audit" },
      { href: "/debugger" as Route, label: "Debugger", icon: "testing" },
      { href: "/review-queue" as Route, label: "Review Queue", icon: "audit" },
      { href: "/schedules" as Route, label: "Schedules", icon: "deploy" },
      // Operate, not Build: a predictor observes what PRODUCTION decisions produce, and the
      // question it answers ("has the population moved?") is an operational one. Same story as
      // Model Sets — page metadata existed, the route did not.
      { href: "/predictors" as Route, label: "Predictors", icon: "audit" },
      { href: "/audit" as Route, label: "Audit Logs", icon: "audit" },
      { href: "/reports" as Route, label: "Reports", icon: "exports" },
      { href: "/exports" as Route, label: "Exports", icon: "exports" },
    ],
  },
  {
    group: "System",
    items: [
      { href: "/settings" as Route, label: "Settings", icon: "settings" },
      { href: "/access" as Route, label: "Access & Roles", icon: "settings" },
      { href: "/branding" as Route, label: "Branding", icon: "settings" },
    ],
  },
];

const COMMANDS: Command[] = NAVIGATION.flatMap((g) => g.items.map((i) => ({ label: i.label, group: g.group, href: i.href })));

const PAGE_COPY: Record<string, { title: string; subtitle: string }> = {
  "/": { title: "RuleMind", subtitle: "Generic decisioning engine for variables, rules, scorecards, policies, and audit trace." },
  "/ai": { title: "AI Copilot", subtitle: "Bring your own OpenAI/Anthropic key — draft rules from plain English, with a token-saving scope guardrail." },
  "/onboarding": { title: "Get started", subtitle: "A guided path to your first live decision — connect a source, create a variable, author a rule, assemble a policy, and run it." },
  "/connectors": { title: "Connectors", subtitle: "Data source connectivity, schemas, auth setup, and sample payload controls." },
  "/variables": { title: "Variables", subtitle: "Sandboxed Python feature creation with testing, lifecycle, and dependency visibility." },
  "/rules": { title: "Rules", subtitle: "Simple and advanced rule authoring with nested logic and inline execution results." },
  "/scorecards": { title: "Scorecards", subtitle: "Points-based scoring with editable ranges and live factor breakdown." },
  "/policies": { title: "Policies", subtitle: "Execution pipelines across connectors, rules, scorecards, and final decisions." },
  "/test-console": { title: "Test Console", subtitle: "Single-run, batch simulation, and production API preview workflows." },
  "/api-console": { title: "API Console", subtitle: "Postman-style console for workflow action steps — resolve templates and send requests server-side." },
  "/review-queue": { title: "Review Queue", subtitle: "Pending human-in-the-loop reviews with decision context and queue state." },
  "/schedules": { title: "Schedules", subtitle: "Cron-driven policy runs, payload sources, and batch execution history." },
  "/deploy": { title: "Deploy", subtitle: "Promote tested assets through DEV, UAT, and PROD with audit-safe controls." },
  "/audit": { title: "Audit Logs", subtitle: "Decision history, promotion history, and operational error telemetry." },
  "/exports": { title: "Exports", subtitle: "Download, validate, and restore complete RuleMind configurations." },
  "/settings": { title: "Settings", subtitle: "Persist API, engine, audit, source, SDK, and environment defaults." },
  "/branding": { title: "Branding", subtitle: "Admin-only white-label theming — CTA colour, backgrounds, brand name, and visible tabs." },
  "/access": { title: "Access & Roles", subtitle: "Issue role-scoped API keys (admin, policy maker, reviewer, viewer) and control what each teammate or integration can do." },
  "/reports": { title: "Reports", subtitle: "Build reports over your decisions — dynamic columns from inputs/outputs, filters, timezone, and scheduled email delivery." },
  "/workflow-builder": { title: "Workflow Builder", subtitle: "Build a decision policy on a canvas — create it, drag or reorder its steps, author rules inline, and wire every step type the engine runs." },
  "/models": { title: "ML Models", subtitle: "Upload, host, test, and run Python models (.pkl / joblib) as policy steps." },
  // Every route below was falling through to PAGE_COPY["/"], so the header showed
  // "RuleMind — Generic decisioning engine…" while you stood on Decision Explorer or Approvals.
  // A header that names the wrong page is worse than one that says nothing: it is the one part
  // of the chrome a user trusts to tell them where they are.
  "/decision-explorer": { title: "Decision Explorer", subtitle: "Find any decision and see exactly why it was made — node by node, with the inputs it saw." },
  "/debugger": { title: "Step-through Debugger", subtitle: "Pause before a step, read what the policy knows there, then advance one step. Nothing is logged or billed." },
  "/journeys": { title: "Journeys", subtitle: "The screens a customer fills in, and the policy that decides what they submit. Active ones ship to devices on the next sync." },
  // Alias routes: these render another page outright, so the header names what you are
  // actually looking at. They are deliberately NOT in the sidebar -- advertising
  // "Approvals" and opening Deploy would be a broken promise.
  "/ab-tests": { title: "A/B Tests", subtitle: "Champion vs challenger experiments with traffic ramps, guardrails, and significance." },
  "/simulation": { title: "Simulation", subtitle: "Replay historical payloads against a draft policy to see what would have changed." },
  "/lifecycle": { title: "Lifecycle", subtitle: "What is in DEV, UAT and PROD right now, and what changed between them." },
  "/collab": { title: "Collab Editor", subtitle: "Edit a draft with teammates in real time, with presence and version history." },
  "/evaluations": { title: "Model Evaluation", subtitle: "Score a hosted model against a labelled set — accuracy, drift, and confusion matrix." },
  "/model-sets": { title: "Model Sets", subtitle: "Group models into a versioned set, route between them by rule, and compare candidates on one input." },
  "/predictors": { title: "Predictors", subtitle: "Track a number a decision already produces, and watch its population drift over time." },
  "/decision-tables": { title: "Decision Tables", subtitle: "Grid-authored decision logic — condition columns, rows, hit policies, with a live optimiser flagging conflicts, gaps, unreachable rows, and invalid values." },
};

// Pages that must render for someone who is not signed in.
const PUBLIC_PREFIXES = ["/login", "/signup", "/admin"];

/**
 * Is the caller authenticated? One probe (GET /api/v1/access/me), which the API answers for an
 * x-api-key, a session cookie, or AUTH_MODE=none alike. Before this there was no gate: a visitor
 * who was not signed in got the full navigation and 41 pages that each failed on their own 401,
 * with no way to tell that signing in was the fix.
 *
 * Only a 401 means "not signed in". A network failure or a 5xx is an outage, and redirecting to a
 * sign-in page the user cannot get past would hide it -- so those let the page render and report
 * its own error.
 */
function useAuthGate(active: boolean): "checking" | "ok" | "anonymous" {
  const { hydrated, apiBaseUrl, apiKey, sessionToken } = useRuleMindStore();
  // The answer is KEYED to the credentials it was computed for. Holding a bare state let a stale
  // "anonymous" outlive the redirect to /login: after signing in, the first render back in the
  // app still read "anonymous", the redirect effect fired, cleared the brand-new session and
  // bounced the user back to sign-in. A changed key now means "checking" until a fresh probe says
  // otherwise.
  const credentials = apiBaseUrl + "|" + apiKey + "|" + sessionToken;
  const [answer, setAnswer] = React.useState<{ credentials: string; state: "ok" | "anonymous" } | null>(null);
  React.useEffect(() => {
    if (!active || !hydrated) return;
    let cancelled = false;
    apiJson(apiBaseUrl, "/api/v1/access/me", {}, apiKey)
      .then(() => { if (!cancelled) setAnswer({ credentials, state: "ok" }); })
      .catch((error) => {
        if (cancelled) return;
        setAnswer({ credentials, state: error instanceof ApiError && error.status === 401 ? "anonymous" : "ok" });
      });
    return () => { cancelled = true; };
  }, [active, hydrated, apiBaseUrl, apiKey, credentials]);
  if (!active) return "ok";
  return answer && answer.credentials === credentials ? answer.state : "checking";
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const {
    hydrated,
    apiBaseUrl,
    apiKey,
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
    member,
    sessionToken,
    clearSession,
  } = useRuleMindStore();
  const router = useRouter();
  const [branding, setBranding] = React.useState<Branding | undefined>(undefined);
  // AI features stay hidden until the tenant configures an OpenAI/Anthropic key
  // (and the admin hasn't switched AI off). Undefined = not yet known.
  const [aiEnabled, setAiEnabled] = React.useState<boolean | undefined>(undefined);
  const theme = withBranding(THEMES[themeMode], branding);
  const page = PAGE_COPY[pathname ?? "/"] ?? PAGE_COPY["/"];

  // Load tenant-wide, admin-configured branding once; apply it as CSS-var overrides.
  // Gate on store rehydration + a present key so we don't fire (and 401) with the pre-hydration key.
  React.useEffect(() => {
    if (!hydrated || !apiKey) {
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const settings = await apiJson<{ branding?: Branding }>(apiBaseUrl, "/api/v1/settings", {}, apiKey);
        if (!cancelled) setBranding(settings.branding ?? undefined);
      } catch {
        /* branding is optional — stock theme is a fine fallback */
      }
      try {
        const ai = await apiJson<{ enabled?: boolean }>(apiBaseUrl, "/api/v1/ai/config", {}, apiKey);
        if (!cancelled) setAiEnabled(Boolean(ai.enabled));
      } catch {
        if (!cancelled) setAiEnabled(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [hydrated, apiBaseUrl, apiKey]);

  React.useEffect(() => {
    document.documentElement.dataset.theme = themeMode;
    const base = themeStyleBlock(themeMode);
    const brand = brandingStyleBlock(branding);
    document.documentElement.setAttribute("style", brand ? base + ";" + brand : base);
  }, [themeMode, branding]);

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

  const isPublic = PUBLIC_PREFIXES.some((prefix) => (pathname ?? "").startsWith(prefix));
  const auth = useAuthGate(!isPublic);
  React.useEffect(() => {
    if (auth === "anonymous") {
      // The persisted member is display state only; once the API says there is no session it is
      // a lie, and the sign-in page would greet the user as someone who is signed in.
      clearSession();
      router.replace(("/login?next=" + encodeURIComponent(pathname ?? "/")) as Route);
    }
  }, [auth, pathname, router, clearSession]);

  if (isPublic) {
    return (
      <div style={{ minHeight: "100vh", background: theme.bg, color: theme.text }}>
        {children}
      </div>
    );
  }

  // Not the app until the session is known: rendering the nav and the page first is exactly the
  // "41 pages that quietly fail" this gate exists to stop.
  if (auth !== "ok") {
    return (
      <div role="status" data-testid="auth-gate"
           style={{ minHeight: "100vh", display: "grid", placeItems: "center", background: theme.bg, color: theme.muted, fontSize: 13 }}>
        {auth === "anonymous" ? "Redirecting to sign in…" : "Checking your session…"}
      </div>
    );
  }

  const sidebarVisible = isMobile ? mobileMenuOpen : true;
  const sidebarWidth = isMobile ? 260 : sidebarOpen ? 224 : 64;
  const showLabels = isMobile ? true : sidebarOpen;

  return (
    <div
      style={{
        // Desktop: pin the shell to the viewport and let <main> scroll internally.
        // With minHeight the grid grew to content height, so the static sidebar
        // scrolled away with the page and the lower nav groups (Audit, Reports,
        // Review Queue, Settings…) sat below the fold on a 720px-tall screen.
        ...(isMobile ? { minHeight: "100vh" } : { height: "100vh" }),
        display: "grid",
        gridTemplateColumns: isMobile ? "1fr" : `${sidebarOpen ? 224 : 64}px minmax(0, 1fr)`,
        // Rows default to auto, so a tall sidebar stretched its own row past the
        // container and body{overflow:hidden} then clipped the bottom nav groups
        // outright. minmax(0,1fr) caps the row at the container height and lets the
        // sidebar's inner <nav> scroll instead.
        ...(isMobile ? {} : { gridTemplateRows: "minmax(0, 1fr)" }),
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
            // minHeight:0 lets the flex child <nav> actually shrink, so its own
            // overflowY:auto engages instead of the column stretching to fit.
            minHeight: 0,
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
              <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
                <span
                  aria-hidden
                  style={{
                    width: 26,
                    height: 26,
                    borderRadius: 7,
                    background: theme.accent,
                    color: theme.inverseText,
                    display: "grid",
                    placeItems: "center",
                    fontSize: 13,
                    fontWeight: 700,
                    flexShrink: 0,
                  }}
                >
                  {resolveLogoText(branding)}
                </span>
                <div style={{ display: "grid", gap: 2 }}>
                  <span style={{ fontSize: "var(--rm-fs-heading)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text, letterSpacing: -0.3 }}>{resolveBrandName(branding)}</span>
                  <span style={{ fontSize: "var(--rm-fs-caption)", color: theme.sidebarGroup }}>Enterprise decisioning</span>
                </div>
              </div>
            ) : null}
          </div>

          <nav style={{ flex: 1, overflowY: "auto", padding: "8px" }}>
            {NAVIGATION.map((group) => ({ ...group, items: group.items.filter((item) => !isNavHidden(branding, item.href) && !((item.href as string) === "/ai" && aiEnabled === false)) }))
              .filter((group) => group.items.length > 0)
              .map((group) => (
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
                      // Collapsed, the link renders an icon and no text, so without
                      // these it has no accessible name at all (WCAG 2.4.4 / 4.1.2)
                      // and a screen reader announces a bare "link".
                      aria-label={item.label}
                      title={item.label}
                      aria-current={active ? "page" : undefined}
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
            {/* ONE HEADING PER PAGE, AND IT IS NOT THIS ONE.
              *
              * This slot used to render `page.title` AND `page.subtitle · API <url>`, both
              * `nowrap` with an ellipsis, in a flex child competing with a fixed-width control
              * group. Every page body ALREADY renders the same title as its <h1> and the same
              * subtitle beneath it -- so the header was a second, truncated copy of the page's
              * own heading. On /ai it rendered as "AI C…" over "Bring y…", which is not a
              * hierarchy, it is the same information twice with the second copy broken.
              *
              * The title stays: it is the only thing that tells you where you are once the page
              * is scrolled past its own <h1>. The subtitle goes -- it is prose, it belongs with
              * the heading it describes, and it was the part that truncated. The API URL moves
              * to the control group as a chip, next to the environment switcher, because it is
              * environment context rather than page copy -- and because it being wrong is a real
              * failure mode worth seeing at a glance (a stale persisted :8100 pointed the whole
              * dashboard at nothing while every page looked fine).
              */}
            {/* A <div>, not an <h1>. The page body owns the document heading; this is persistent
              * chrome that repeats it. Marking both as <h1> gave every page two level-one
              * headings, which is a worse outline than the one <h1> it replaced. */}
            <div style={{ display: "grid", minWidth: 0 }}>
              <div style={{ margin: 0, fontSize: "var(--rm-fs-title)", fontWeight: "var(--rm-fw-bold)" as unknown as number, color: theme.text, letterSpacing: -0.4, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{page.title}</div>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: isMobile ? 6 : 10, flexShrink: 0 }}>
            {!isMobile && (
              <button
                type="button"
                aria-label="Open command palette"
                onClick={() => window.dispatchEvent(new KeyboardEvent("keydown", { key: "k", metaKey: true }))}
                style={{
                  display: "inline-flex", alignItems: "center", gap: 8,
                  background: theme.hover, border: "1px solid " + theme.border, borderRadius: 8,
                  padding: "6px 10px 6px 11px", color: theme.dim, cursor: "pointer", fontSize: 12.5,
                }}
              >
                <Search size={14} />
                <span style={{ color: theme.muted }}>Search</span>
                <kbd style={{ fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 600, color: theme.dim, background: theme.card, border: "1px solid " + theme.border, borderRadius: 5, padding: "1px 5px" }}>⌘K</kbd>
              </button>
            )}
            {/* Which API this dashboard is actually talking to. Host:port only, with the full URL
              * on hover -- enough to notice it is wrong, small enough not to compete with the
              * environment switcher it sits beside. */}
            {!isMobile && (
              <span
                data-testid="topbar-api-host"
                title={apiBaseUrl}
                style={{
                  fontFamily: "var(--font-mono)",
                  fontSize: 10,
                  color: theme.dim,
                  background: theme.card,
                  border: "1px solid " + theme.border,
                  borderRadius: 6,
                  padding: "3px 7px",
                  whiteSpace: "nowrap",
                }}
              >
                {(() => { try { const u = new URL(apiBaseUrl); return u.host; } catch { return apiBaseUrl; } })()}
              </span>
            )}
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

            {!isMobile && member ? (
              <div style={{ display: "inline-flex", flexDirection: "column", alignItems: "flex-end", lineHeight: 1.2 }}>
                <span style={{ fontSize: "var(--rm-fs-small)", fontWeight: 600, color: theme.text }}>{member.name || member.email}</span>
                <button
                  type="button"
                  onClick={async () => {
                    try { await apiJson(apiBaseUrl, "/api/v1/auth/logout", { method: "POST" }, apiKey); } catch { /* best effort */ }
                    clearSession();
                    router.push("/login");
                  }}
                  style={{ background: "none", border: "none", padding: 0, cursor: "pointer", color: theme.muted, fontSize: 11 }}
                >
                  Sign out
                </button>
              </div>
            ) : !isMobile && !sessionToken ? (
              <button
                type="button"
                onClick={() => router.push("/login")}
                style={{ background: theme.hover, border: "1px solid " + theme.border, borderRadius: 8, padding: "6px 11px", color: theme.text, fontSize: "var(--rm-fs-small)", cursor: "pointer" }}
              >
                Sign in
              </button>
            ) : null}

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
              {(member?.name || member?.email || "U").charAt(0).toUpperCase()}
            </div>
          </div>
        </header>

        <main style={{ flex: 1, minHeight: 0, overflow: "auto" }}>{children}</main>
      </div>
      <CommandPalette commands={COMMANDS} />
      {/* AI Copilot — only mounts once a provider key is configured and AI is on. */}
      {aiEnabled && apiKey ? <AiCopilotFab theme={theme} apiBaseUrl={apiBaseUrl} apiKey={apiKey} /> : null}
    </div>
  );
}
