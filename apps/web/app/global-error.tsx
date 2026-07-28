"use client";

import * as React from "react";

/**
 * Root error boundary — the last line of defence. Only fires when the root
 * layout itself throws (the segment `error.tsx` handles page-level crashes).
 * Must render its own <html>/<body>.
 */
export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  React.useEffect(() => {
    // eslint-disable-next-line no-console
    console.error("RuleMind fatal error:", error);
  }, [error]);

  return (
    <html lang="en">
      <body
        style={{
          margin: 0,
          minHeight: "100vh",
          display: "grid",
          placeItems: "center",
          fontFamily: "system-ui, -apple-system, Segoe UI, sans-serif",
          background: "#f7f8fc",
          color: "#131828",
        }}
      >
        <div style={{ maxWidth: 420, textAlign: "center", padding: 24 }}>
          <h2 style={{ margin: 0, fontSize: 20 }}>RuleMind ran into a problem</h2>
          <p style={{ color: "#6a7290", margin: "8px 0 20px", fontSize: 14 }}>
            Something went wrong loading the app. Reloading usually fixes it.
          </p>
          <button
            onClick={reset}
            style={{
              padding: "9px 18px",
              borderRadius: 8,
              border: "1px solid #5b5bd6",
              background: "#5b5bd6",
              color: "#fff",
              fontWeight: 600,
              cursor: "pointer",
            }}
          >
            Try again
          </button>
          {error.digest ? (
            <p style={{ marginTop: 16, fontSize: 12, color: "#6a7290", fontFamily: "ui-monospace, monospace" }}>
              ref: {error.digest}
            </p>
          ) : null}
        </div>
      </body>
    </html>
  );
}
