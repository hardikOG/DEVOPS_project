#!/usr/bin/env bash
# Render the kustomize tree, pin the image tag, apply, and wait for every rollout.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${IMAGE_TAG:-1.0.0}"

kubectl apply -f "$ROOT/k8s/namespaces.yaml"

# Grafana admin password lives only in the cluster, never in git.
if ! kubectl -n monitoring get secret grafana-admin >/dev/null 2>&1; then
  password="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 20)"
  kubectl -n monitoring create secret generic grafana-admin --from-literal=password="$password"
  echo "created Grafana admin secret (read it with: kubectl -n monitoring get secret grafana-admin -o jsonpath='{.data.password}' | base64 -d)"
fi

kubectl kustomize "$ROOT/k8s" \
  | sed -E "s#(self-healing/(demo-service|healer)):1\.0\.0#\1:${TAG}#g" \
  | kubectl apply -f -

for dep in orders payments inventory loadgen healer; do
  kubectl -n self-healing rollout status "deploy/$dep" --timeout=180s
done
for dep in prometheus alertmanager kube-state-metrics grafana; do
  kubectl -n monitoring rollout status "deploy/$dep" --timeout=240s
done

cat <<EOF

  Aegis control plane  http://localhost:30080
  Grafana              http://localhost:30030
  Prometheus           http://localhost:30090
  Alertmanager         http://localhost:30093
EOF
