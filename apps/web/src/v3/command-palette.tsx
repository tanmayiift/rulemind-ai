"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import type { Route } from "next";
import { Search, CornerDownLeft } from "lucide-react";

export type Command = { label: string; group: string; href: string };

/**
 * ⌘K / Ctrl-K command palette — fast keyboard navigation across the app.
 * Fully token-driven (var(--rm-*)), keyboard-first (↑ ↓ Enter Esc), fuzzy filter.
 */
export function CommandPalette({ commands }: { commands: Command[] }) {
  const router = useRouter();
  const [open, setOpen] = React.useState(false);
  const [query, setQuery] = React.useState("");
  const [active, setActive] = React.useState(0);
  const inputRef = React.useRef<HTMLInputElement>(null);

  React.useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  React.useEffect(() => {
    if (open) {
      setQuery("");
      setActive(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  const filtered = React.useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return commands;
    return commands.filter((c) => (c.label + " " + c.group).toLowerCase().includes(q));
  }, [commands, query]);

  React.useEffect(() => {
    if (active >= filtered.length) setActive(0);
  }, [filtered.length, active]);

  const go = (href: string) => {
    setOpen(false);
    router.push(href as Route);
  };

  if (!open) return null;

  return (
    <div
      onClick={() => setOpen(false)}
      style={{ position: "fixed", inset: 0, zIndex: 2000, background: "rgba(10,15,30,.45)", backdropFilter: "blur(2px)", display: "flex", alignItems: "flex-start", justifyContent: "center", paddingTop: "12vh" }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{ width: "min(560px, 92vw)", background: "var(--rm-card)", border: "1px solid var(--rm-border)", borderRadius: 16, boxShadow: "var(--rm-shadow-lg)", overflow: "hidden" }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, padding: "14px 16px", borderBottom: "1px solid var(--rm-border)" }}>
          <Search size={18} style={{ color: "var(--rm-dim)" }} />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(a + 1, filtered.length - 1)); }
              else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(a - 1, 0)); }
              else if (e.key === "Enter" && filtered[active]) { e.preventDefault(); go(filtered[active].href); }
            }}
            placeholder="Search screens and actions…"
            style={{ flex: 1, border: "none", outline: "none", background: "transparent", color: "var(--rm-text)", fontSize: 15, fontFamily: "var(--font-sans)" }}
          />
          <kbd style={kbdStyle}>ESC</kbd>
        </div>
        <div style={{ maxHeight: 340, overflowY: "auto", padding: 8 }}>
          {filtered.length === 0 ? (
            <div style={{ padding: 24, textAlign: "center", color: "var(--rm-dim)", fontSize: 13 }}>No matches.</div>
          ) : (
            filtered.map((c, i) => (
              <button
                key={c.href}
                onMouseEnter={() => setActive(i)}
                onClick={() => go(c.href)}
                style={{
                  width: "100%", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 10,
                  padding: "10px 12px", borderRadius: 10, border: "none", cursor: "pointer", textAlign: "left",
                  background: i === active ? "var(--rm-accent-bg)" : "transparent",
                }}
              >
                <span style={{ display: "flex", alignItems: "baseline", gap: 10 }}>
                  <span style={{ fontSize: 14, fontWeight: 600, color: i === active ? "var(--rm-accent)" : "var(--rm-text)" }}>{c.label}</span>
                  <span style={{ fontSize: 11.5, color: "var(--rm-dim)" }}>{c.group}</span>
                </span>
                {i === active ? <CornerDownLeft size={15} style={{ color: "var(--rm-accent)" }} /> : null}
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
}

const kbdStyle: React.CSSProperties = {
  fontFamily: "var(--font-mono)", fontSize: 10, fontWeight: 600, color: "var(--rm-dim)",
  background: "var(--rm-hover)", border: "1px solid var(--rm-border)", borderRadius: 6, padding: "2px 6px",
};
