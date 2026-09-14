#!/usr/bin/env bash
# Run RuleMind on a LOCAL Kubernetes cluster — kind + the Helm chart, no cloud, no registry.
#
# WHY THIS EXISTS. helm/rulemind was linted and rendered but had never been INSTALLED anywhere.
# `helm lint` proves the YAML parses; `verify_chart.py` proves the rendered manifests match a
# contract. Neither can tell you the pods start, the migrate hook completes, or the Service
# routes to something Ready. This script closes that gap on hardware you already have.
#
#   scripts/k8s_up.sh          # create cluster, build+load images, install, wait
#   scripts/smoke_k8s.sh       # then verify it actually works
#   make k8s-down              # delete the whole cluster
set -uo pipefail
cd "$(dirname "$0")/.." || exit 2

CLUSTER="${RULEMIND_KIND_CLUSTER:-rulemind}"
RELEASE="${RULEMIND_HELM_RELEASE:-rulemind}"
NS="${RULEMIND_K8S_NAMESPACE:-rulemind}"

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32mok\033[0m    %s\n' "$1"; }
die()  { printf '  \033[31mfail\033[0m  %s\n' "$1"; exit 1; }

for bin in kind kubectl helm docker; do
  command -v "$bin" >/dev/null 2>&1 || die "$bin is not installed (brew install kind kubectl helm)"
done

say "1/5  cluster"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  ok "kind cluster '$CLUSTER' already exists"
else
  kind create cluster --name "$CLUSTER" --wait 120s || die "kind create cluster"
  ok "created kind cluster '$CLUSTER'"
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null 2>&1 || die "could not select kind-$CLUSTER context"

say "2/5  images"
# Built from the same Dockerfiles compose uses, so this tests the artifact that actually ships.
docker build -q -f apps/python-executor/Dockerfile -t rulemind/api:local . >/dev/null || die "api image build"
ok "built rulemind/api:local"
docker build -q -f apps/web/Dockerfile -t rulemind/web:local . >/dev/null || die "web image build"
ok "built rulemind/web:local"
# kind nodes cannot pull from the host daemon; the image has to be shipped into the node. This
# is why values-kind.yaml pins pullPolicy to IfNotPresent -- Always would ignore what we load.
kind load docker-image rulemind/api:local rulemind/web:local --name "$CLUSTER" >/dev/null 2>&1 \
  || die "kind load docker-image"
ok "loaded both images into the cluster"

say "3/5  dependencies"
kubectl create namespace "$NS" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$NS" apply -f k8s/kind/deps.yaml >/dev/null || die "applying k8s/kind/deps.yaml"
kubectl -n "$NS" rollout status deploy/rulemind-postgres --timeout=180s >/dev/null || die "postgres never became ready"
kubectl -n "$NS" rollout status deploy/rulemind-redis --timeout=120s >/dev/null || die "redis never became ready"
ok "postgres + redis ready"

say "4/5  install"
# Generated per install, never committed, and deliberately not the dev defaults: values-kind.yaml
# sets rulemindEnv=production, and app/security_config.py refuses to boot on a default secret.
# That check is worth exercising rather than tiptoeing around.
CONFIG_KEY="$(openssl rand -hex 32)"
ADMIN_JWT="$(openssl rand -hex 32)"
ADMIN_PW="$(openssl rand -hex 24)"
DEV_API_KEY="rm_live_$(openssl rand -hex 16)"
helm upgrade --install "$RELEASE" helm/rulemind \
  -n "$NS" \
  -f helm/rulemind/values-kind.yaml \
  --set-string env.configKey="$CONFIG_KEY" \
  --set-string env.adminJwtSecret="$ADMIN_JWT" \
  --set-string env.adminPassword="$ADMIN_PW" \
  --set-string env.devApiKey="$DEV_API_KEY" \
  --wait --timeout 8m >/dev/null || {
    printf '\n  helm install failed — recent events:\n'
    kubectl -n "$NS" get pods
    kubectl -n "$NS" get events --sort-by=.lastTimestamp | tail -20
    exit 1
  }
ok "helm release '$RELEASE' installed in namespace '$NS'"

say "5/5  rollout"
for d in api web worker; do
  if kubectl -n "$NS" get deploy "$RELEASE-$d" >/dev/null 2>&1; then
    kubectl -n "$NS" rollout status "deploy/$RELEASE-$d" --timeout=300s >/dev/null \
      && ok "$d rolled out" || die "$d never became ready"
  fi
done

# Written, not echoed: the smoke script needs the key and nothing should paste it into a terminal
# transcript. .runtime/ is gitignored.
mkdir -p .runtime && umask 077 && printf 'RULEMIND_K8S_API_KEY=%s\n' "$DEV_API_KEY" > .runtime/k8s-key.env
printf '\n\033[32mUp.\033[0m namespace=%s release=%s\n' "$NS" "$RELEASE"
printf '  verify:      scripts/smoke_k8s.sh\n'
printf '  dashboard:   kubectl -n %s port-forward svc/%s-web 3000:3000\n' "$NS" "$RELEASE"
printf '  api:         kubectl -n %s port-forward svc/%s-api 8080:8080\n' "$NS" "$RELEASE"
printf '  tear down:   make k8s-down\n\n'
