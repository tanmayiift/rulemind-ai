/** Where to go after signing in: the page the auth gate sent us from, if it is a path on this
 *  site. "//evil.example" and "https://…" are not -- `?next=` is attacker-controllable, so only a
 *  single-slash relative path is followed. */
export function safeNext(search: string): string {
  const next = new URLSearchParams(search).get("next") ?? "";
  return /^\/(?!\/)[^\s\\]*$/.test(next) && !next.startsWith("/login") ? next : "/";
}
