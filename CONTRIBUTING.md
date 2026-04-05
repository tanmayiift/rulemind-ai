# Contributing to RuleMind AI

Thank you for considering contributing to RuleMind AI! This guide helps you get started.

## Development Setup

### Prerequisites

- Node.js >= 18
- pnpm >= 9 (`npm install -g pnpm`)
- Python >= 3.10
- (Optional) Docker and Docker Compose

### Getting Started

```bash
# Clone the repo
git clone https://github.com/your-org/rulemind-ai.git
cd rulemind-ai

# Install dependencies
pnpm install
pip install -r apps/python-executor/requirements.txt

# Set up environment
cp .env.example .env
# Edit .env — set AUTH_MODE=none for local dev

# Start development servers
pnpm dev
```

This starts the Next.js dashboard on port 3000 and the FastAPI backend on port 8080.

### Running the Backend Separately

```bash
cd apps/python-executor
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8080 --reload
```

## Quality Gates

All PRs must pass these checks:

```bash
pnpm lint        # ESLint
pnpm typecheck   # TypeScript strict mode
pnpm test        # Vitest unit tests
pnpm test:e2e    # Playwright browser tests
```

For Python backend changes:

```bash
cd apps/python-executor
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Coding Guidelines

- **No hardcoded secrets.** Runtime config goes in environment variables.
- **Preserve the `RuleDefinition` payload format.** It is a public API contract.
- **Maker-checker for decision-affecting changes.** Any mutable asset that affects decisions must go through promotion paths (dev > uat > prod).
- **Add tests for behavioral changes.** If you change evaluation logic, API contracts, or validation — add or update tests.
- **Keep PRs focused.** One feature or fix per PR. Large refactors should be discussed in an issue first.

## Project Architecture

| Layer | Technology | Path |
| --- | --- | --- |
| Dashboard | Next.js 14 | `apps/web/` |
| API | FastAPI (Python) | `apps/python-executor/` |
| Rule Engine | TypeScript | `packages/engine/` |
| SDKs | JS, Python, Kotlin, Dart | `packages/sdk-*/` |
| Schemas | JSON Schema | `packages/schemas/` |
| Tests | Vitest + Playwright | `test/`, `e2e/` |

## Submitting Changes

1. Fork the repository
2. Create a feature branch from `main`
3. Make your changes
4. Ensure all quality gates pass
5. Submit a pull request with a clear description

## Reporting Issues

- **Bugs:** Use the [Bug Report](https://github.com/your-org/rulemind-ai/issues/new?template=bug_report.md) template
- **Features:** Use the [Feature Request](https://github.com/your-org/rulemind-ai/issues/new?template=feature_request.md) template
- **Security:** See [SECURITY.md](SECURITY.md) — do not open public issues for vulnerabilities

## Code of Conduct

This project follows the [Contributor Covenant Code of Conduct](CODE_OF_CONDUCT.md).
