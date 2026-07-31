## RuleMind — common developer + ops commands.
.PHONY: help up down observability observability-down logs test

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

up: ## Start the core stack (API, web, Postgres, Redis)
	docker compose up

down: ## Stop the core stack
	docker compose down

observability: ## Start everything WITH tracing on — Grafana :3001, Prometheus :9090, Tempo, OTel collector (one command)
	OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 docker compose --profile observability up

observability-down: ## Stop the stack including the observability profile
	docker compose --profile observability down

logs: ## Tail the API logs
	docker compose logs -f api

test: ## Run the Python + web test suites
	cd apps/python-executor && python -m unittest discover -s tests -p 'test_*.py'
	pnpm test
