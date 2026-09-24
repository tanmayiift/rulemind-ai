"use client";

import * as React from "react";
import { createPortal } from "react-dom";

/**
 * Shared design-system primitives (Stripe-refined). Style comes from the CSS
 * component layer in globals.css (token-driven via var(--rm-*)), so these are
 * theme- and branding-reactive with zero props and stay consistent everywhere.
 */

type ButtonVariant = "primary" | "secondary" | "ghost" | "danger";
type ButtonSize = "sm" | "md" | "lg";

export function Button({
  variant = "primary",
  size = "md",
  className = "",
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { variant?: ButtonVariant; size?: ButtonSize }) {
  const cls = ["rm-btn", `rm-btn-${variant}`, size !== "md" ? `rm-btn-${size}` : "", className]
    .filter(Boolean)
    .join(" ");
  return <button className={cls} {...props} />;
}

export function Card({
  pad = true,
  hover = false,
  className = "",
  style,
  children,
  // Forwarded explicitly rather than by spreading the rest of the props, to keep this
  // component's API deliberately narrow. It has to be here at all because React's JSX typing
  // accepts `data-*` on a custom component WITHOUT the component declaring it, so
  // `<Card data-testid="x">` type-checks and is then silently dropped at runtime -- a test
  // written against it fails with "waiting for locator" on an element that is plainly on screen.
  "data-testid": testId,
}: {
  pad?: boolean;
  hover?: boolean;
  className?: string;
  style?: React.CSSProperties;
  children: React.ReactNode;
  "data-testid"?: string;
}) {
  const cls = ["rm-card", pad ? "rm-card-pad" : "", hover ? "rm-card-hover" : "", className].filter(Boolean).join(" ");
  return (
    <div className={cls} style={style} data-testid={testId}>
      {children}
    </div>
  );
}

export function Input({ className = "", mono = false, ...props }: React.InputHTMLAttributes<HTMLInputElement> & { mono?: boolean }) {
  return <input className={["rm-input", mono ? "rm-mono" : "", className].filter(Boolean).join(" ")} {...props} />;
}

export function Textarea({ className = "", mono = false, ...props }: React.TextareaHTMLAttributes<HTMLTextAreaElement> & { mono?: boolean }) {
  return <textarea className={["rm-textarea", mono ? "rm-mono" : "", className].filter(Boolean).join(" ")} {...props} />;
}

export function Select({ className = "", ...props }: React.SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={["rm-select", className].filter(Boolean).join(" ")} {...props} />;
}

export function Field({
  label,
  hint,
  children,
  style,
}: {
  label?: React.ReactNode;
  hint?: React.ReactNode;
  children: React.ReactNode;
  style?: React.CSSProperties;
}) {
  return (
    <label style={{ display: "grid", gap: 6, ...style }}>
      {label ? <span className="rm-label">{label}</span> : null}
      {children}
      {hint ? <span style={{ fontSize: 12, color: "var(--rm-dim)" }}>{hint}</span> : null}
    </label>
  );
}

type Tone = "accent" | "success" | "warning" | "danger" | "neutral";

export function Badge({ tone = "neutral", children }: { tone?: Tone; children: React.ReactNode }) {
  return <span className={`rm-badge rm-badge-${tone}`}>{children}</span>;
}

export function Stat({ label, value, tone }: { label: React.ReactNode; value: React.ReactNode; tone?: "success" | "warning" | "danger" | "accent" }) {
  const color = tone ? `var(--rm-${tone})` : undefined;
  return (
    <div className="rm-stat">
      <div className="rm-stat-label">{label}</div>
      <div className="rm-stat-value" style={color ? { color } : undefined}>
        {value}
      </div>
    </div>
  );
}

export function EmptyState({ icon, title, hint, action }: { icon?: React.ReactNode; title: string; hint?: string; action?: React.ReactNode }) {
  return (
    <div className="rm-empty">
      {icon ? <div className="rm-empty-icon">{icon}</div> : null}
      <div className="rm-empty-title">{title}</div>
      {hint ? <div className="rm-empty-hint">{hint}</div> : null}
      {action ? <div style={{ marginTop: 10 }}>{action}</div> : null}
    </div>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 16, flexWrap: "wrap", marginBottom: 20 }}>
      <div>
        <h1 style={{ margin: 0, fontSize: "var(--rm-fs-hero)", fontWeight: 700, letterSpacing: "var(--rm-letter-tight)", color: "var(--rm-text)" }}>{title}</h1>
        {subtitle ? <p style={{ margin: "6px 0 0", fontSize: 13.5, color: "var(--rm-muted)", maxWidth: 640, lineHeight: 1.5 }}>{subtitle}</p> : null}
      </div>
      {actions ? <div style={{ display: "flex", gap: 10, flexWrap: "wrap" }}>{actions}</div> : null}
    </div>
  );
}

/**
 * A CARD/PANEL title -- the first level of section beneath [PageHeader]'s `h1`, so `h2`.
 *
 * It was an `h3`, which skipped a level on every page that used it: h1 -> h3, no h2 anywhere.
 * Measured on 2026-09-19 with qa/e2e-full/frontend_audit.mjs: 14 routes flagged `heading-skip`,
 * and this component is used on exactly 14. Screen-reader users navigating by heading level, and
 * anything reading the document outline, saw a gap that implied a missing section.
 *
 * The visual size is unchanged -- it is `fontSize: 15`, set here and not by the tag.
 */
export function SectionTitle({ children, right }: { children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
      <h2 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: "var(--rm-text)" }}>{children}</h2>
      {right}
    </div>
  );
}

/**
 * A modal dialog. The app had none: consequential actions (promotion, rollback) went through
 * `window.confirm()`, which cannot show structured context, cannot ask for a reason, is blocked
 * outright in some embedded browsers, and gives a screen reader nothing but a string.
 *
 * Accessible by construction: `role="dialog"` + `aria-modal`, labelled by its title, focus moves
 * into it on open and back to the opener on close, Tab cycles inside it, and Escape closes it
 * (unless `busy`, so a request in flight cannot be orphaned by a keystroke).
 */
export function Dialog({
  open,
  title,
  onClose,
  children,
  footer,
  busy = false,
  width = 560,
}: {
  open: boolean;
  title: string;
  onClose: () => void;
  children: React.ReactNode;
  footer?: React.ReactNode;
  busy?: boolean;
  width?: number;
}) {
  const panel = React.useRef<HTMLDivElement>(null);
  const titleId = React.useId();
  React.useEffect(() => {
    if (!open) return;
    const opener = document.activeElement as HTMLElement | null;
    const focusables = () =>
      Array.from(panel.current?.querySelectorAll<HTMLElement>(
        'button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), [href], [tabindex]:not([tabindex="-1"])'
      ) ?? []);
    (focusables().find((el) => el.tagName !== "BUTTON") ?? focusables()[0] ?? panel.current)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !busy) {
        event.preventDefault();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusables();
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
      opener?.focus?.();
    };
  }, [open, busy, onClose]);
  if (!open || typeof document === "undefined") return null;
  // Portalled to <body>: a fixed overlay rendered inside a card is clipped by any ancestor with a
  // transform or overflow, and pages render the dialog next to the button that opens it.
  return createPortal(
    <div
      style={{ position: "fixed", inset: 0, zIndex: 1000, display: "grid", placeItems: "center", padding: 16,
               background: "color-mix(in srgb, var(--rm-text) 38%, transparent)" }}
      onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        style={{ width: "100%", maxWidth: width, maxHeight: "calc(100vh - 32px)", overflow: "auto",
                 background: "var(--rm-card)", color: "var(--rm-text)", borderRadius: 14,
                 border: "1px solid var(--rm-border)", boxShadow: "0 24px 64px rgba(0,0,0,0.28)", padding: 22 }}
      >
        <h2 id={titleId} style={{ margin: "0 0 12px", fontSize: 17, fontWeight: 700 }}>{title}</h2>
        <div style={{ display: "grid", gap: 12, fontSize: 13.5, lineHeight: 1.5 }}>{children}</div>
        {footer ? <div style={{ display: "flex", justifyContent: "flex-end", gap: 10, marginTop: 18 }}>{footer}</div> : null}
      </div>
    </div>,
    document.body
  );
}
