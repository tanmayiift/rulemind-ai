import type { ThemeMode } from "../lib/store";

export interface ThemeTokens {
  bg: string;
  card: string;
  cardAlt: string;
  sidebar: string;
  header: string;
  hover: string;
  input: string;
  editor: string;
  border: string;
  borderStrong: string;
  borderFocus: string;
  text: string;
  muted: string;
  dim: string;
  accent: string;
  accentHover: string;
  accentBg: string;
  success: string;
  successBg: string;
  warning: string;
  warningBg: string;
  danger: string;
  dangerBg: string;
  purple: string;
  purpleBg: string;
  sidebarText: string;
  sidebarGroup: string;
  sidebarActive: string;
  scrollbar: string;
  codeText: string;
  inverseText: string;
  toggleKnob: string;
  // Elevation — Stripe-style layered shadows for premium depth.
  shadowSm: string;
  shadowMd: string;
  shadowLg: string;
  ring: string;
}

// Palette is built on Stripe's neutral ramp + blurple accent for a refined,
// premium-but-approachable enterprise feel (the chosen "Stripe-refined" direction).
export const THEMES: Record<ThemeMode, ThemeTokens> = {
  light: {
    bg: "#f5f7fb",
    card: "#ffffff",
    cardAlt: "#f7fafc",
    sidebar: "#ffffff",
    header: "#ffffff",
    hover: "#eef2f8",
    input: "#ffffff",
    editor: "#f6f9fc",
    border: "#e6ebf1",
    borderStrong: "#d5dbe4",
    borderFocus: "#635bff",
    text: "#1a1f36",
    muted: "#4f566b",
    dim: "#8792a2",
    accent: "#635bff",
    accentHover: "#4b45cc",
    accentBg: "rgba(99,91,255,.09)",
    success: "#067647",
    successBg: "rgba(6,118,71,.09)",
    warning: "#b54708",
    warningBg: "rgba(181,71,8,.09)",
    danger: "#df1b41",
    dangerBg: "rgba(223,27,65,.08)",
    purple: "#7a5af8",
    purpleBg: "rgba(122,90,248,.09)",
    sidebarText: "#3c4257",
    sidebarGroup: "#8792a2",
    sidebarActive: "rgba(99,91,255,.10)",
    scrollbar: "#cbd2dc",
    codeText: "#1a1f36",
    inverseText: "#ffffff",
    toggleKnob: "#ffffff",
    shadowSm: "0 1px 2px rgba(10,37,64,.05), 0 1px 1px rgba(10,37,64,.04)",
    shadowMd: "0 2px 5px -1px rgba(50,50,93,.10), 0 1px 3px -1px rgba(10,37,64,.07)",
    shadowLg: "0 13px 27px -5px rgba(50,50,93,.14), 0 8px 16px -8px rgba(10,37,64,.10)",
    ring: "0 0 0 3px rgba(99,91,255,.18)",
  },
  dark: {
    bg: "#0b0e18",
    card: "#141a2b",
    cardAlt: "#1a2138",
    sidebar: "#0e1220",
    header: "#111726",
    hover: "#1e2740",
    input: "#161d31",
    editor: "#0c101c",
    border: "#242c44",
    borderStrong: "#323c5a",
    borderFocus: "#8b85ff",
    text: "#e7eaf3",
    muted: "#a4adc6",
    dim: "#6b7799",
    accent: "#8b85ff",
    accentHover: "#a29cff",
    accentBg: "rgba(139,133,255,.14)",
    success: "#3ecf8e",
    successBg: "rgba(62,207,142,.13)",
    warning: "#f5a623",
    warningBg: "rgba(245,166,35,.13)",
    danger: "#ff5c78",
    dangerBg: "rgba(255,92,120,.13)",
    purple: "#a29cff",
    purpleBg: "rgba(162,156,255,.14)",
    sidebarText: "#c3cade",
    sidebarGroup: "#6b7799",
    sidebarActive: "rgba(139,133,255,.16)",
    scrollbar: "#2c3552",
    codeText: "#dfe4f2",
    inverseText: "#0b0e18",
    toggleKnob: "#e7eaf3",
    shadowSm: "0 1px 2px rgba(0,0,0,.4)",
    shadowMd: "0 2px 6px -1px rgba(0,0,0,.5), 0 1px 3px -1px rgba(0,0,0,.4)",
    shadowLg: "0 16px 32px -8px rgba(0,0,0,.6), 0 8px 16px -8px rgba(0,0,0,.5)",
    ring: "0 0 0 3px rgba(139,133,255,.28)",
  },
};

export const ENVIRONMENT_ACCENT: Record<string, string> = {
  dev: THEMES.light.purple,
  uat: THEMES.light.warning,
  prod: THEMES.light.accent,
};

export function themeStyleBlock(mode: ThemeMode): string {
  const theme = THEMES[mode];
  return [
    "--rm-bg:" + theme.bg,
    "--rm-card:" + theme.card,
    "--rm-card-alt:" + theme.cardAlt,
    "--rm-sidebar:" + theme.sidebar,
    "--rm-header:" + theme.header,
    "--rm-hover:" + theme.hover,
    "--rm-input:" + theme.input,
    "--rm-editor:" + theme.editor,
    "--rm-border:" + theme.border,
    "--rm-border-strong:" + theme.borderStrong,
    "--rm-border-focus:" + theme.borderFocus,
    "--rm-text:" + theme.text,
    "--rm-muted:" + theme.muted,
    "--rm-dim:" + theme.dim,
    "--rm-accent:" + theme.accent,
    "--rm-accent-hover:" + theme.accentHover,
    "--rm-accent-bg:" + theme.accentBg,
    "--rm-success:" + theme.success,
    "--rm-success-bg:" + theme.successBg,
    "--rm-warning:" + theme.warning,
    "--rm-warning-bg:" + theme.warningBg,
    "--rm-danger:" + theme.danger,
    "--rm-danger-bg:" + theme.dangerBg,
    "--rm-purple:" + theme.purple,
    "--rm-purple-bg:" + theme.purpleBg,
    "--rm-sidebar-text:" + theme.sidebarText,
    "--rm-sidebar-group:" + theme.sidebarGroup,
    "--rm-sidebar-active:" + theme.sidebarActive,
    "--rm-scrollbar:" + theme.scrollbar,
    "--rm-code-text:" + theme.codeText,
    "--rm-inverse-text:" + theme.inverseText,
    "--rm-shadow-sm:" + theme.shadowSm,
    "--rm-shadow-md:" + theme.shadowMd,
    "--rm-shadow-lg:" + theme.shadowLg,
    "--rm-ring:" + theme.ring,
  ].join(";");
}
