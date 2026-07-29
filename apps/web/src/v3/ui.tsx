"use client";

import * as React from "react";

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
}: {
  pad?: boolean;
  hover?: boolean;
  className?: string;
  style?: React.CSSProperties;
  children: React.ReactNode;
}) {
  const cls = ["rm-card", pad ? "rm-card-pad" : "", hover ? "rm-card-hover" : "", className].filter(Boolean).join(" ");
  return (
    <div className={cls} style={style}>
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

export function SectionTitle({ children, right }: { children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 14 }}>
      <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700, color: "var(--rm-text)" }}>{children}</h3>
      {right}
    </div>
  );
}
