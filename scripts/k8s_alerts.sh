#!/usr/bin/env bash
# Apply the alert rules to a Kubernetes cluster, generated from the SAME file compose uses.
#
# WHY A SCRIPT AND NOT A HELM TEMPLATE. The obvious move is a PrometheusRule template inside
# helm/rulemind. That needs the rules to live inside the chart directory (Helm's .Files.Get
# cannot read above the chart root), which means a SECOND COPY of observability/alerts.yml.
# Two copies of an alert definition is precisely the drift this repository keeps getting bitten
# by -- and a stale duplicate is worse here than elsewhere, because the symptom is "the alert
# that would have caught it was the old one".
#
# So: one file, observability/alerts.yml, wrapped at apply time.
#
#   scripts/k8s_alerts.sh                 # render and apply
#   scripts/k8s_alerts.sh --dry-run       # print the manifest, apply nothing
#
# TWO SHAPES, because clusters differ:
#   * prometheus-operator present  -> a PrometheusRule (monitoring.coreos.com/v1), picked up
#                                     automatically by the operator's ruleSelector
#   * no operator                  -> a ConfigMap labelled for a sidecar reloader, which is how
#                                     kube-prometheus-stack's rule sidecar discovers rules
#
# The script detects which by asking the API server for the CRD, rather than guessing.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

NS="${RULEMIND_K8S_NAMESPACE:-rulemind}"
SRC="observability/alerts.yml"
DRY=0
[ "${1:-}" = "--dry-run" ] && DRY=1

# Diagnostics go to STDERR, always. In --dry-run the manifest must be the ONLY thing on stdout,
# or `scripts/k8s_alerts.sh --dry-run | kubectl apply -f -` feeds kubectl a YAML document with
# ANSI colour codes in front of it. Found by piping the output into a parser instead of reading
# it, which is the difference between "looks right" and "is right".
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1" >&2; }
bad()  { printf '  \033[31mfail\033[0m  %s\n' "$1" >&2; }
note() { printf '        %s\n' "$1" >&2; }

[ -f "$SRC" ] || { bad "$SRC not found"; exit 1; }
command -v kubectl >/dev/null 2>&1 || { bad "kubectl is required"; exit 1; }

# Validate BEFORE shipping. A rules file that Prometheus refuses leaves the cluster with the
# PREVIOUS rules silently still in force, which reads as "alerting is configured".
if command -v promtool >/dev/null 2>&1; then
  promtool check rules "$SRC" >/dev/null 2>&1 \
    && ok "promtool: rules valid" \
    || { bad "promtool rejected $SRC — fix it before applying"; promtool check rules "$SRC"; exit 1; }
elif command -v docker >/dev/null 2>&1; then
  docker run --rm -v "$PWD/$SRC:/tmp/a.yml:ro" --entrypoint promtool prom/prometheus:latest \
    check rules /tmp/a.yml >/dev/null 2>&1 \
    && ok "promtool (via docker): rules valid" \
    || { bad "promtool rejected $SRC — fix it before applying"; exit 1; }
else
  note "neither promtool nor docker available; applying unvalidated"
fi

HAVE_OPERATOR=0
kubectl get crd prometheusrules.monitoring.coreos.com >/dev/null 2>&1 && HAVE_OPERATOR=1

MANIFEST=$(mktemp)
trap 'rm -f "$MANIFEST"' EXIT

if [ "$HAVE_OPERATOR" -eq 1 ]; then
  ok "prometheus-operator detected — rendering a PrometheusRule"
  {
    printf 'apiVersion: monitoring.coreos.com/v1\nkind: PrometheusRule\nmetadata:\n'
    printf '  name: rulemind-alerts\n  namespace: %s\n  labels:\n' "$NS"
    printf '    app.kubernetes.io/name: rulemind\n    release: prometheus\n'
    printf 'spec:\n'
    # The file is already `groups:` at the top level, which is exactly PrometheusRule's spec.
    # Indent it by two and it drops straight in -- no transformation, so nothing to get wrong.
    sed 's/^/  /' "$SRC"
  } > "$MANIFEST"
else
  ok "no prometheus-operator — rendering a ConfigMap for a rule sidecar"
  {
    printf 'apiVersion: v1\nkind: ConfigMap\nmetadata:\n'
    printf '  name: rulemind-alerts\n  namespace: %s\n  labels:\n' "$NS"
    printf '    app.kubernetes.io/name: rulemind\n    prometheus_rule: "1"\n'
    printf 'data:\n  rulemind-alerts.yaml: |\n'
    sed 's/^/    /' "$SRC"
  } > "$MANIFEST"
  note "a plain Prometheus needs this mounted and referenced from rule_files —"
  note "the label above is what kube-prometheus-stack's sidecar looks for."
fi

if [ "$DRY" -eq 1 ]; then
  cat "$MANIFEST"
  exit 0
fi

kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null 2>&1
if kubectl apply -f "$MANIFEST" >/dev/null 2>&1; then
  ok "applied rulemind-alerts to namespace $NS"
  printf '\n'
  note "Verify the CHAIN, not the parts — every component here validates on its own while"
  note "being disconnected, which is how this went unnoticed in the compose stack:"
  note "  kubectl -n $NS get prometheusrule,configmap rulemind-alerts"
  note "  <prometheus>/api/v1/rules          are they loaded?"
  note "  <prometheus>/api/v1/alertmanagers  is one ACTIVE?"
  note "  <alertmanager>/api/v2/alerts       did anything ARRIVE?"
else
  bad "kubectl apply failed"
  kubectl apply -f "$MANIFEST"
  exit 1
fi
