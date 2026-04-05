# Security Policy

## Supported Versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | Yes       |

## Reporting a Vulnerability

If you discover a security vulnerability in RuleMind AI, please report it responsibly.

**Do NOT open a public GitHub issue for security vulnerabilities.**

Instead, please email: **security@rulemind.ai**

Include: description, reproduction steps, potential impact, and suggested fix (if any).

We will acknowledge your report within 48 hours and aim to provide a fix within 7 days for critical vulnerabilities.

## Security Architecture

### Authentication Modes

| Mode     | Description                                    | Use Case               |
| -------- | ---------------------------------------------- | ---------------------- |
| `none`   | No authentication required                     | Local development only |
| `apikey` | API key in `X-API-Key` header (SHA-256 hashed) | Default for production |
| `jwt`    | HS256 JWT tokens with 12-hour expiry           | Admin console sessions |

### Secrets Management

All secrets **must** be provided via environment variables in production:

| Variable                     | Purpose                      | Required In Production  |
| ---------------------------- | ---------------------------- | ----------------------- |
| `RULEMIND_ADMIN_JWT_SECRET`  | Signs admin session JWTs     | Yes                     |
| `RULEMIND_CONFIG_KEY`        | Encrypts config at rest      | Yes                     |
| `POSTGRES_PASSWORD`          | Database password            | Yes (if using Postgres) |
| `RULEMIND_ADMIN_EMAIL`       | Default admin login email    | Recommended             |
| `RULEMIND_ADMIN_PASSWORD`    | Default admin login password | Recommended             |

The codebase includes development-only fallback defaults that are **only** used when environment variables are not set. **Never use defaults in production.**

### API Key Security

- Keys generated with `rm_live_` prefix + 32 cryptographically random characters
- Stored as SHA-256 hashes, never in plaintext
- Rate limiting: 1,000 req/min (standard), 5,000 req/min (enterprise)

### Python Sandbox

Variable code executes in a restricted Python sandbox:
- Import whitelist: `math`, `re`, `json`, `datetime`, `statistics`, `collections`
- Configurable timeout (default: 2s) and memory limit (default: 128 MB)
- No filesystem or network access

### SDK Bundle Security

- RSA public key encryption per device
- HMAC-SHA256 signature verification
- Versioned with 304 caching
- Periodic expiry and re-fetch

### Network Security

- CORS origins explicitly configured (no wildcards)
- HTTPS expected in production (reverse proxy)
- Admin cookies use `Secure` + `HttpOnly` in non-development mode
- Per-tenant rate limiting at middleware level

## Self-Hosting Checklist

1. Set all secret environment variables (never rely on defaults)
2. Use HTTPS behind nginx, Caddy, or a cloud load balancer
3. Use Postgres in production (SQLite is development only)
4. Set `AUTH_MODE=apikey` (never `none` in production)
5. Rotate API keys periodically via admin console
6. Configure `AUDIT_RETENTION_DAYS` for compliance
7. Set restrictive `CORS_ORIGINS` to your frontend domain only
