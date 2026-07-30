"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { KeyRound, Mail, ShieldCheck } from "lucide-react";
import { apiJson } from "../../src/lib/api";
import { useRuleMindStore, type SessionMember } from "../../src/lib/store";
import { Button, Card, Field, Input } from "../../src/v3/ui";

type LoginResp = { token: string; expires_at: string; member: SessionMember };
type OtpReq = { requested: boolean; delivered?: boolean; debug_code?: string };

export default function LoginPage() {
  const router = useRouter();
  const { apiBaseUrl, setSession, member } = useRuleMindStore();
  const [mode, setMode] = React.useState<"password" | "otp">("password");
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [code, setCode] = React.useState("");
  const [otpSent, setOtpSent] = React.useState(false);
  const [hint, setHint] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);
  const [busy, setBusy] = React.useState(false);

  const finish = (resp: LoginResp) => {
    setSession(resp.token, resp.member);
    router.push("/");
  };

  const loginPassword = async () => {
    setBusy(true); setError(null);
    try {
      finish(await apiJson<LoginResp>(apiBaseUrl, "/api/v1/auth/login", {
        method: "POST", body: JSON.stringify({ email, password }),
      }));
    } catch (e) { setError(e instanceof Error ? e.message : "Sign-in failed."); } finally { setBusy(false); }
  };

  const requestOtp = async () => {
    setBusy(true); setError(null); setHint(null);
    try {
      const r = await apiJson<OtpReq>(apiBaseUrl, "/api/v1/auth/otp/request", {
        method: "POST", body: JSON.stringify({ email }),
      });
      setOtpSent(true);
      if (r.debug_code) { setCode(r.debug_code); setHint(`Dev mode: your code is ${r.debug_code} (email delivery isn't configured).`); }
      else if (r.delivered) setHint("We emailed you a 6-digit code. It expires in 10 minutes.");
      else setHint("If that email belongs to a member, a code is on its way.");
    } catch (e) { setError(e instanceof Error ? e.message : "Could not send code."); } finally { setBusy(false); }
  };

  const verifyOtp = async () => {
    setBusy(true); setError(null);
    try {
      finish(await apiJson<LoginResp>(apiBaseUrl, "/api/v1/auth/otp/verify", {
        method: "POST", body: JSON.stringify({ email, code }),
      }));
    } catch (e) { setError(e instanceof Error ? e.message : "Invalid or expired code."); } finally { setBusy(false); }
  };

  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 24 }}>
      <div style={{ width: "100%", maxWidth: 400, display: "flex", flexDirection: "column", gap: 18 }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ width: 46, height: 46, margin: "0 auto 12px", borderRadius: 12, display: "grid", placeItems: "center", background: "var(--rm-accent)", color: "#fff" }}>
            <ShieldCheck size={24} />
          </div>
          <h1 style={{ fontSize: 22, fontWeight: 800, margin: 0 }}>Sign in to RuleMind</h1>
          <p style={{ fontSize: 13, color: "var(--rm-muted)", marginTop: 6 }}>
            Access is scoped to your role. {member ? `You're currently signed in as ${member.email}.` : ""}
          </p>
        </div>

        <Card>
          <div style={{ display: "flex", gap: 6, marginBottom: 16, background: "var(--rm-bg)", padding: 4, borderRadius: 9 }}>
            {(["password", "otp"] as const).map((m) => (
              <button key={m} onClick={() => { setMode(m); setError(null); }}
                style={{ flex: 1, padding: "8px 10px", borderRadius: 7, border: "none", cursor: "pointer", fontSize: 13, fontWeight: 600,
                  background: mode === m ? "var(--rm-card)" : "transparent",
                  color: mode === m ? "var(--rm-text)" : "var(--rm-muted)",
                  boxShadow: mode === m ? "0 1px 3px rgba(0,0,0,0.08)" : "none",
                  display: "inline-flex", alignItems: "center", justifyContent: "center", gap: 6 }}>
                {m === "password" ? <KeyRound size={14} /> : <Mail size={14} />}
                {m === "password" ? "Password" : "Email code"}
              </button>
            ))}
          </div>

          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <Field label="Work email">
              <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@company.com" autoComplete="email" />
            </Field>

            {mode === "password" ? (
              <>
                <Field label="Password">
                  <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                    onKeyDown={(e) => { if (e.key === "Enter") void loginPassword(); }} autoComplete="current-password" />
                </Field>
                <Button onClick={loginPassword} disabled={busy || !email || !password}>{busy ? "Signing in…" : "Sign in"}</Button>
              </>
            ) : (
              <>
                {otpSent ? (
                  <Field label="6-digit code">
                    <Input value={code} onChange={(e) => setCode(e.target.value)} inputMode="numeric" maxLength={6} placeholder="000000"
                      onKeyDown={(e) => { if (e.key === "Enter") void verifyOtp(); }} />
                  </Field>
                ) : null}
                {!otpSent ? (
                  <Button onClick={requestOtp} disabled={busy || !email}>{busy ? "Sending…" : "Email me a code"}</Button>
                ) : (
                  <div style={{ display: "flex", gap: 8 }}>
                    <Button onClick={verifyOtp} disabled={busy || code.length < 6}>{busy ? "Verifying…" : "Verify & sign in"}</Button>
                    <Button variant="secondary" onClick={requestOtp} disabled={busy}>Resend</Button>
                  </div>
                )}
              </>
            )}

            {hint ? <div style={{ fontSize: 12, color: "var(--rm-muted)", background: "var(--rm-bg)", padding: "8px 10px", borderRadius: 7 }}>{hint}</div> : null}
            {error ? <div style={{ fontSize: 12.5, color: "var(--rm-danger)" }}>{error}</div> : null}
          </div>
        </Card>

        <p style={{ textAlign: "center", fontSize: 11.5, color: "var(--rm-muted)" }}>
          No account yet? Ask a workspace admin to add you under Access &amp; Roles.
        </p>
      </div>
    </div>
  );
}
