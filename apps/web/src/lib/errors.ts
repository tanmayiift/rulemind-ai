/**
 * Turn a thrown API error into a sentence a person can read.
 *
 * `apiJson` rejects with an `ApiError` whose message is the raw response TEXT, so a 401 arrives
 * here as the literal string `{"detail":"Invalid email or password."}`. Rendering `e.message`
 * printed that JSON on the sign-in screen -- the first screen a new user sees. A helper rather
 * than a change to `apiJson`: at least one caller (decision-tables/page.tsx) parses `e.message`
 * as JSON on purpose to recover a per-row `problems` list. Shared by sign-in and signup.
 */
export function humanMessage(error: unknown, fallback: string): string {
  const raw = error instanceof Error ? error.message : "";
  if (!raw) return fallback;
  try {
    const detail = (JSON.parse(raw) as { detail?: unknown })?.detail;
    if (typeof detail === "string" && detail) return detail;
    // FastAPI validation errors are a list of {loc, msg} objects, not a string.
    if (Array.isArray(detail)) {
      const parts = detail
        .map((d) => (d && typeof (d as { msg?: unknown }).msg === "string" ? (d as { msg: string }).msg : null))
        .filter((m): m is string => Boolean(m));
      if (parts.length) return parts.join("; ");
    }
  } catch {
    /* Not JSON -- e.g. apiJson's own "Unable to reach the API server…". Show it as-is. */
  }
  return raw;
}
