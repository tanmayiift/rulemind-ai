"use client";

import * as React from "react";

/**
 * Route-segment error boundary. Keeps a crash contained to the current screen —
 * the app shell (nav, header) stays usable and the user can retry — instead of
 * white-screening the whole console. A real deployment forwards `error` to the
 * observability backend (Sentry / OTel) here.
 */
export default function ScreenError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    // eslint-disable-next-line no-console
    console.error("RuleMind screen error:", error);
  }, [error]);

  return (
    <div
      role="alert"
      style={{
        minHeight: "60vh",
        display: "grid",
        placeItems: "center",
        padding: "48px 24px",
        color: "var(--rm-text, #131828)",
        fontFamily: "var(--font-sans)",
      }}
    >
      <div style={{ maxWidth: 440, textAlign: "center" }}>
        <div
          style={{
            width: 52,
            height: 52,
            borderRadius: 14,
            margin: "0 auto 16px",
            display: "grid",
            placeItems: "center",
            background: "var(--rm-danger-soft, #fbe0e0)",
            color: "var(--rm-danger, #dc2626)",
            fontSize: 26,
            fontWeight: 700,
          }}
          aria-hidden
        >
          !
        </div>
        <h2 style={{ margin: 0, fontSize: 18 }}>This screen hit an error</h2>
        <p style={{ color: "var(--rm-muted, #6a7290)", margin: "8px 0 20px", fontSize: 14 }}>
          The rest of the console is still working. Try again, and if it keeps happening the error
          reference below helps support track it down.
        </p>
        <div style={{ display: "flex", gap: 10, justifyContent: "center" }}>
          <button
            onClick={reset}
            style={{
              padding: "9px 16px",
              borderRadius: 8,
              border: "1px solid var(--rm-accent, #5b5bd6)",
              background: "var(--rm-accent, #5b5bd6)",
              color: "#fff",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            Try again
          </button>
          <button
            onClick={() => window.location.reload()}
            style={{
              padding: "9px 16px",
              borderRadius: 8,
              border: "1px solid var(--rm-border, #e4e7f0)",
              background: "var(--rm-card, #fff)",
              color: "var(--rm-text, #131828)",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            Reload
          </button>
        </div>
        {error.digest ? (
          <p style={{ marginTop: 16, fontSize: 12, color: "var(--rm-muted, #6a7290)", fontFamily: "var(--font-mono)" }}>
            ref: {error.digest}
          </p>
        ) : null}
      </div>
    </div>
  );
}
