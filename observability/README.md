# Observability — OpenTelemetry + Prometheus + Grafana + Tempo

RuleMind emits **metrics** (Prometheus, always on at `/metrics`) and **distributed
traces** (OpenTelemetry via OTLP, opt-in). The stack below is connect-and-go and
swappable for any OTLP/PromQL backend — nothing is vendor-locked.

## Start it

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317 \
  docker compose --profile observability up
```

- **Grafana** → http://localhost:3001 (anonymous viewer enabled; admin/admin to edit). The **RuleMind — Decisioning Overview** dashboard is pre-provisioned.
- **Prometheus** → http://localhost:9090 (scrapes the API `/metrics`).
- **Tempo** ← receives traces from the collector; explore them in Grafana → Explore → Tempo.

Omit the profile and the core stack (web/api/worker/db/redis) runs exactly as before — no observability containers, and tracing stays off.

## How it fits together

```
  API (FastAPI)
   ├── /metrics ──────────────► Prometheus ──► Grafana (dashboards)
   └── OTLP traces ──► otel-collector ──► Tempo ──► Grafana (Explore)
```

- Metrics: `rulemind_decisions_total{outcome,source}` and process metrics (via `prometheus-client`).
- Traces: FastAPI + SQLAlchemy auto-instrumentation ([app/observability.py](../apps/python-executor/app/observability.py)); every `/api/v1/decide` becomes a span tree. Enabled only when `OTEL_EXPORTER_OTLP_ENDPOINT` is set — otherwise a no-op.

## Scale / swap

- Point `OTEL_EXPORTER_OTLP_ENDPOINT` at a managed collector (Grafana Cloud, Honeycomb, Datadog OTLP, Jaeger) — no code change.
- On Kubernetes, run the collector as a DaemonSet/sidecar and set the env in the Helm values; Prometheus scrapes the pod `/metrics` via a ServiceMonitor.
- Files: `otel-collector-config.yaml`, `prometheus.yml`, `tempo.yaml`, `grafana/provisioning/*`, `grafana/dashboards/rulemind.json`.
