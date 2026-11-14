#!/usr/bin/env bash
# Start a single-node Kubernetes cluster (k3s) inside Docker.
# Needs nothing but Docker - no kind/minikube/k3d install. NodePorts are published
# on localhost so the dashboards are reachable straight from the browser.
set -euo pipefail

NAME="${CLUSTER_NAME:-shk-k3s}"
K3S_IMAGE="${K3S_IMAGE:-rancher/k3s:v1.31.5-k3s1}"
API_PORT="${API_PORT:-6550}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
KUBE_DIR="$ROOT/.kube"
mkdir -p "$KUBE_DIR"

if docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
  echo "cluster '$NAME' already running"
else
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "starting k3s ($K3S_IMAGE) as container '$NAME'"
  docker run -d --name "$NAME" --hostname "$NAME" --privileged \
    --tmpfs /run --tmpfs /var/run \
    -p "$API_PORT:6443" \
    -p 30080:30080 -p 30030:30030 -p 30090:30090 -p 30093:30093 \
    "$K3S_IMAGE" server \
      --disable traefik \
      --tls-san 127.0.0.1 --tls-san host.docker.internal \
      "--kubelet-arg=eviction-hard=memory.available<100Mi" \
      --write-kubeconfig-mode 644 >/dev/null
fi

echo -n "waiting for API server"
for _ in $(seq 1 90); do
  if docker exec "$NAME" kubectl get nodes 2>/dev/null | grep -q " Ready"; then echo " ready"; break; fi
  echo -n "."; sleep 2
done

docker exec "$NAME" cat /etc/rancher/k3s/k3s.yaml \
  | sed "s#https://127.0.0.1:6443#https://127.0.0.1:$API_PORT#" > "$KUBE_DIR/config"
# Variant for containers (e.g. Jenkins) that reach the host via host.docker.internal.
sed "s#https://127.0.0.1:$API_PORT#https://host.docker.internal:$API_PORT#" \
  "$KUBE_DIR/config" > "$KUBE_DIR/config.docker"

echo "kubeconfig written to $KUBE_DIR/config"
echo "  export KUBECONFIG=$KUBE_DIR/config"
