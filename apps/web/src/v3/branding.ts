import type { ThemeMode } from "../lib/store";
import { THEMES, type ThemeTokens } from "./theme";

/**
 * Admin-only, config-driven white-label branding. Stored tenant-wide on the backend
 * (`settings.branding`) so an enterprise can host RuleMind on their internal browser
 * and restyle it — CTA/accent colour, canvas + sidebar background, brand name/logo,
 * and which nav tabs are visible — without touching code. Empty fields fall back to
 * the active theme, so an untouched install looks exactly like stock RuleMind.
 */
export interface Branding {
  brandName?: string;
  logoText?: string;
  accent?: string;
  accentText?: string;
  background?: string;
  sidebar?: string;
  hiddenNav?: string[];
}

export const EMPTY_BRANDING: Branding = {
  brandName: "",
  logoText: "",
  accent: "",
  accentText: "",
  background: "",
  sidebar: "",
  hiddenNav: [],
};

const HEX = /^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$/;

export function isColor(value?: string): boolean {
  return !!value && HEX.test(value.trim());
}

/** Convert #rgb / #rrggbb to rgba(...) with the given alpha, for tint backgrounds. */
export function hexToRgba(hex: string, alpha: number): string {
  let h = hex.trim().replace("#", "");
  if (h.length === 3) h = h.split("").map((c) => c + c).join("");
  const int = parseInt(h, 16);
  const r = (int >> 16) & 255;
  const g = (int >> 8) & 255;
  const b = int & 255;
  return `rgba(${r},${g},${b},${alpha})`;
}

/** Merge branding overrides onto a theme so app-shell's inline styles pick them up. */
export function withBranding(base: ThemeTokens, branding?: Branding): ThemeTokens {
  if (!branding) return base;
  const next: ThemeTokens = { ...base };
  if (isColor(branding.accent)) {
    next.accent = branding.accent!.trim();
    next.accentBg = hexToRgba(next.accent, 0.1);
    next.borderFocus = next.accent;
    next.sidebarActive = hexToRgba(next.accent, 0.1);
  }
  if (isColor(branding.background)) next.bg = branding.background!.trim();
  if (isColor(branding.sidebar)) next.sidebar = branding.sidebar!.trim();
  if (isColor(branding.accentText)) next.inverseText = branding.accentText!.trim();
  return next;
}

/**
 * CSS-variable overrides appended after `themeStyleBlock` so the rest of the app
 * (which reads `var(--rm-*)`) reflects the brand. Returns "" when nothing is set.
 */
export function brandingStyleBlock(branding?: Branding): string {
  if (!branding) return "";
  const decls: string[] = [];
  if (isColor(branding.accent)) {
    const a = branding.accent!.trim();
    decls.push("--rm-accent:" + a);
    decls.push("--rm-accent-bg:" + hexToRgba(a, 0.1));
    decls.push("--rm-border-focus:" + a);
    decls.push("--rm-sidebar-active:" + hexToRgba(a, 0.1));
  }
  if (isColor(branding.accentText)) decls.push("--rm-accent-text:" + branding.accentText!.trim());
  if (isColor(branding.background)) decls.push("--rm-bg:" + branding.background!.trim());
  if (isColor(branding.sidebar)) decls.push("--rm-sidebar:" + branding.sidebar!.trim());
  return decls.join(";");
}

export function brandName(branding?: Branding): string {
  return branding?.brandName?.trim() || "RuleMind";
}

export function logoText(branding?: Branding): string {
  const explicit = branding?.logoText?.trim();
  if (explicit) return explicit.slice(0, 2).toUpperCase();
  return brandName(branding).slice(0, 1).toUpperCase();
}

export function isNavHidden(branding: Branding | undefined, href: string): boolean {
  return !!branding?.hiddenNav?.includes(href);
}

/** Full style attribute for a themed root: base theme vars + branding overrides. */
export function rootStyle(mode: ThemeMode, branding?: Branding, baseBlock?: string): string {
  const base = baseBlock ?? "";
  const brand = brandingStyleBlock(branding);
  return brand ? `${base};${brand}` : base;
}

export const _internal = { THEMES };
