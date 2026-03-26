# Contributing

## Development Workflow

1. Install Node.js, pnpm, and Python dependencies.
2. Copy `.env.example` to `.env`.
3. Use `DATABASE_ADAPTER=file` for zero-dependency development or start Postgres/Redis with Docker Compose.
4. Run `pnpm dev` for the web, API, and worker packages.
5. Run the Python executor separately with Uvicorn.

## Quality Gates

- `pnpm lint`
- `pnpm typecheck`
- `pnpm test`
- `pnpm build`

## Coding Notes

- Keep runtime config in environment variables; never hardcode secrets.
- Preserve the public `RuleDefinition` payload format.
- Any mutable asset that can affect decisions must go through maker-checker paths.
- Add or update tests alongside any change to evaluation behavior, validation, or API contracts.
