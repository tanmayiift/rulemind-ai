/**
 * English -- the source catalog. Every key the app renders through `t()` lives here; other
 * locales are partial and fall back to these strings, key by key.
 *
 * Flat, dotted keys (`signin.title`) so a missing key is a single grep away, and `Messages` is the
 * type every other catalog is checked against.
 */
export const en = {
  "signin.title": "Sign in to RuleMind",
  "signin.scope": "Access is scoped to your role.",
  "signin.signedInAs": "You're currently signed in as {email}.",
  "signin.mode.password": "Password",
  "signin.mode.code": "Email code",
  "signin.email": "Work email",
  "signin.password": "Password",
  "signin.code": "6-digit code",
  "signin.submit": "Sign in",
  "signin.submitting": "Signing in…",
  "signin.sendCode": "Email me a code",
  "signin.sending": "Sending…",
  "signin.verify": "Verify & sign in",
  "signin.verifying": "Verifying…",
  "signin.resend": "Resend",
  "signin.or": "OR",
  "signin.sso": "Sign in with SSO",
  "signin.noAccount": "No account yet? Ask a workspace admin to add you under Access & Roles, or",
  "signin.createWorkspace": "create a new workspace",
  "signup.title": "Create a RuleMind workspace",
  "signup.ready": "Your workspace is ready",
  "signup.lead": "You'll be its owner. Sample lending policies are loaded so you can make a decision straight away.",
  "signup.keepKey": "Keep the key below — it is not shown again.",
  "signup.company": "Company",
  "signup.email": "Work email",
  "signup.name": "Your name (optional)",
  "signup.password": "Password — at least {min} characters",
  "signup.passwordShort.one": "{count} more character.",
  "signup.passwordShort.other": "{count} more characters.",
  "signup.useCase": "What will you decide?",
  "signup.submit": "Create workspace",
  "signup.submitting": "Creating…",
  "signup.devKey": "Development API key for {company}",
  "signup.continue": "Continue to setup",
  "signup.haveWorkspace": "Already have a workspace?",
  "signup.signIn": "Sign in",
} as const;

export type MessageKey = keyof typeof en;
export type Messages = Partial<Record<MessageKey, string>>;
