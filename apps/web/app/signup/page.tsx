"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { Building2, Copy, KeyRound } from "lucide-react";

import { apiJson } from "../../src/lib/api";
import { humanMessage } from "../../src/lib/errors";
import { useRuleMindStore, type SessionMember } from "../../src/lib/store";
import { Button, Card, Field, Input, Select } from "../../src/v3/ui";

type SignupResponse = {
  tenant_id: string;
  company: string;
  api_key: string;
  environment: string;
  session?: { token: string; expires_at: string; member: SessionMember };
};

const MIN_PASSWORD = 12;

/**
 * Create a workspace (plan 6.6). The backend's onboarding state machine -- signup, dev key, verify,
 * AI choice, production key -- was complete and had no screen, so a prospect could not start
 * without someone provisioning a tenant by hand. Signup also creates the workspace's owner and
 * signs them in; the dev API key is shown exactly once, here.
 */
export default function SignupPage() {
  const router = useRouter();
  const { apiBaseUrl, setSession } = useRuleMindStore();
  const [company, setCompany] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [name, setName] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [useCase, setUseCase] = React.useState("lending");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [created, setCreated] = React.useState<SignupResponse | null>(null);
  const [copied, setCopied] = React.useState(false);

  const valid = company.trim() && /^[^@\s]+@[^@\s]+$/.test(email.trim()) && password.length >= MIN_PASSWORD;

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      const response = await apiJson<SignupResponse>(apiBaseUrl, "/api/v1/onboarding/signup", {
        method: "POST",
        body: JSON.stringify({ company: company.trim(), contact_email: email.trim(), name: name.trim() || undefined,
                               password, use_case: useCase }),
      });
      if (response.session) setSession(response.session.token, response.session.member);
      setCreated(response);
    } catch (e) {
      setError(humanMessage(e, "Could not create the workspace."));
    } finally {
      setBusy(false);
    }
  };

  const copyKey = async () => {
    if (!created) return;
    try {
      await navigator.clipboard.writeText(created.api_key);
      setCopied(true);
    } catch {
      /* the key is visible and selectable; copying is a convenience */
    }
  };

  return (
    <div style={{ minHeight: "100vh", display: "grid", placeItems: "center", padding: 16, background: "var(--rm-bg)" }}>
      <div style={{ width: "100%", maxWidth: 440, display: "grid", gap: 16 }}>
        <div style={{ textAlign: "center" }}>
          <div style={{ width: 46, height: 46, margin: "0 auto 12px", borderRadius: 12, display: "grid", placeItems: "center", background: "var(--rm-accent)", color: "#fff" }}>
            <Building2 size={24} />
          </div>
          <h1 style={{ fontSize: 22, fontWeight: 800, margin: 0 }}>{created ? "Your workspace is ready" : "Create a RuleMind workspace"}</h1>
          <p style={{ fontSize: 13, color: "var(--rm-muted)", marginTop: 6 }}>
            {created ? "Keep the key below — it is not shown again." : "You'll be its owner. Sample lending policies are loaded so you can make a decision straight away."}
          </p>
        </div>

        <Card>
          {created ? (
            <div style={{ display: "grid", gap: 12 }}>
              <Field label={`Development API key for ${created.company}`}>
                <div style={{ display: "flex", gap: 8 }}>
                  <Input mono readOnly value={created.api_key} aria-label="Development API key" data-testid="signup-api-key"
                         onFocus={(e) => e.currentTarget.select()} />
                  <Button variant="secondary" onClick={copyKey} aria-label="Copy the key"><Copy size={14} /></Button>
                </div>
              </Field>
              <div role="status" style={{ fontSize: 12.5, color: "var(--rm-muted)" }}>
                {copied ? "Copied. " : ""}Send it as <code>x-api-key</code> from your integration. A production key is issued
                from setup once a decision has been made with this one.
              </div>
              <Button onClick={() => router.push("/onboarding")} data-testid="signup-continue">
                <KeyRound size={15} /> Continue to setup
              </Button>
            </div>
          ) : (
            <form onSubmit={submit} style={{ display: "grid", gap: 12 }} noValidate>
              <Field label="Company">
                <Input value={company} onChange={(e) => setCompany(e.target.value)} autoComplete="organization" required />
              </Field>
              <Field label="Work email">
                <Input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email"
                       placeholder="you@company.com" required />
              </Field>
              <Field label="Your name (optional)">
                <Input value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" />
              </Field>
              <Field label={`Password — at least ${MIN_PASSWORD} characters`}>
                <Input type="password" value={password} onChange={(e) => setPassword(e.target.value)}
                       autoComplete="new-password" minLength={MIN_PASSWORD} required
                       aria-describedby="signup-password-hint" />
              </Field>
              {password && password.length < MIN_PASSWORD ? (
                <div id="signup-password-hint" style={{ fontSize: 12, color: "var(--rm-muted)" }}>
                  {MIN_PASSWORD - password.length} more character{MIN_PASSWORD - password.length === 1 ? "" : "s"}.
                </div>
              ) : null}
              <Field label="What will you decide?">
                <Select value={useCase} onChange={(e) => setUseCase(e.target.value)}>
                  <option value="lending">Lending / credit</option>
                  <option value="bnpl">Buy now, pay later</option>
                  <option value="insurance">Insurance</option>
                  <option value="fraud">Fraud &amp; risk</option>
                  <option value="other">Something else</option>
                </Select>
              </Field>
              <Button type="submit" disabled={busy || !valid}>{busy ? "Creating…" : "Create workspace"}</Button>
              {error ? <div role="alert" style={{ fontSize: 12.5, color: "var(--rm-danger)" }}>{error}</div> : null}
            </form>
          )}
        </Card>

        <p style={{ textAlign: "center", fontSize: 11.5, color: "var(--rm-muted)" }}>
          Already have a workspace? <a href="/login" style={{ color: "var(--rm-accent)" }}>Sign in</a>.
        </p>
      </div>
    </div>
  );
}
