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
  borderFocus: string;
  text: string;
  muted: string;
  dim: string;
  accent: string;
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
}

export const THEMES: Record<ThemeMode, ThemeTokens> = {
  light: {
    bg: "#f4f5f7",
    card: "#ffffff",
    cardAlt: "#fafbfc",
    sidebar: "#ffffff",
    header: "#ffffff",
    hover: "#f0f1f4",
    input: "#f4f5f7",
    editor: "#f8f9fc",
    border: "#e2e5ec",
    borderFocus: "#3b82f6",
    text: "#111827",
    muted: "#374151",
    dim: "#9ca3af",
    accent: "#3b82f6",
    accentBg: "rgba(59,130,246,.08)",
    success: "#059669",
    successBg: "rgba(5,150,105,.08)",
    warning: "#d97706",
    warningBg: "rgba(217,119,6,.08)",
    danger: "#dc2626",
    dangerBg: "rgba(220,38,38,.08)",
    purple: "#7c3aed",
    purpleBg: "rgba(124,58,237,.08)",
    sidebarText: "#374151",
    sidebarGroup: "#9ca3af",
    sidebarActive: "rgba(59,130,246,.08)",
    scrollbar: "#cbd5e1",
    codeText: "#1e293b",
    inverseText: "#ffffff",
    toggleKnob: "#ffffff",
  },
  dark: {
    bg: "#0f1117",
    card: "#171923",
    cardAlt: "#1e2130",
    sidebar: "#131620",
    header: "#171923",
    hover: "#232738",
    input: "#1e2130",
    editor: "#0d0f14",
    border: "#282d3e",
    borderFocus: "#3b82f6",
    text: "#e5e7eb",
    muted: "#d1d5db",
    dim: "#6b7280",
    accent: "#60a5fa",
    accentBg: "rgba(59,130,246,.12)",
    success: "#34d399",
    successBg: "rgba(52,211,153,.12)",
    warning: "#fbbf24",
    warningBg: "rgba(251,191,36,.12)",
    danger: "#f87171",
    dangerBg: "rgba(248,113,113,.12)",
    purple: "#a78bfa",
    purpleBg: "rgba(167,139,250,.12)",
    sidebarText: "#d1d5db",
    sidebarGroup: "#6b7280",
    sidebarActive: "rgba(59,130,246,.14)",
    scrollbar: "#334155",
    codeText: "#e2e8f0",
    inverseText: "#ffffff",
    toggleKnob: "#ffffff",
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
    "--rm-border-focus:" + theme.borderFocus,
    "--rm-text:" + theme.text,
    "--rm-muted:" + theme.muted,
    "--rm-dim:" + theme.dim,
    "--rm-accent:" + theme.accent,
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
  ].join(";");
}
