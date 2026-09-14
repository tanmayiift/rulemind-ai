## RuleMind — common developer + ops commands.
.PHONY: help up down observability observability-down logs test k8s-up k8s-down k8s-smoke smoke

help: ## Show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'

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

smoke: ## Verify the compose stack end to end (14 checks)
	bash scripts/smoke_compose.sh

k8s-up: ## Run the Helm chart on a local kind cluster (no cloud, no registry)
	bash scripts/k8s_up.sh

k8s-smoke: ## Verify the kind deployment end to end
	bash scripts/smoke_k8s.sh

k8s-down: ## Delete the local kind cluster
	kind delete cluster --name $${RULEMIND_KIND_CLUSTER:-rulemind}

test: ## Run the Python + web test suites
	cd apps/python-executor && python -m unittest discover -s tests -p 'test_*.py'
	pnpm test
