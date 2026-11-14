#!/usr/bin/env bash
# Make locally built images available to the cluster's container runtime.
#
#   k3s-docker     k3s running as a Docker container (scripts/cluster-up.sh): import into its containerd
#   shared-docker  cluster uses the same Docker daemon (Docker Desktop Kubernetes, k3s --docker on EC2)
set -euo pipefail

RUNTIME="${CLUSTER_RUNTIME:-k3s-docker}"
TAG="${IMAGE_TAG:-1.0.0}"
NAME="${CLUSTER_NAME:-shk-k3s}"
APP_IMAGES=("self-healing/demo-service:$TAG" "self-healing/healer:$TAG")
INFRA_IMAGES=(prom/prometheus:v2.55.1 prom/alertmanager:v0.27.0 grafana/grafana:11.3.1
              registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0)

case "$RUNTIME" in
  k3s-docker)
    images=("${APP_IMAGES[@]}")
    if [ "${IMPORT_INFRA:-0}" = "1" ]; then
      for image in "${INFRA_IMAGES[@]}"; do
        docker image inspect "$image" >/dev/null 2>&1 && images+=("$image")
      done
    fi
    echo "importing ${#images[@]} image(s) into $NAME"
    docker save "${images[@]}" | docker exec -i "$NAME" ctr -n k8s.io images import - >/dev/null
    ;;
  shared-docker|docker-desktop|k3s-host)
    echo "cluster shares the Docker daemon; nothing to load"
    ;;
  *)
    echo "unknown CLUSTER_RUNTIME '$RUNTIME'" >&2
    exit 1
    ;;
esac
