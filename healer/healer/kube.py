"""Thin gateway over the Kubernetes API.

Everything the healer needs from the cluster goes through `KubeGateway`, which
returns plain dataclasses. The engine depends only on the `ClusterGateway`
protocol, so tests swap in an in-memory fake.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Protocol

from .policy import choose_rollback_target

log = logging.getLogger("healer.kube")

REVISION_ANNOTATION = "deployment.kubernetes.io/revision"
MIN_REPLICAS_ANNOTATION = "selfheal/min-replicas"


@dataclass
class PodInfo:
    name: str
    deployment: str
    phase: str
    ready: bool
    restart_count: int
    waiting_reason: str | None
    last_exit_reason: str | None
    ip: str | None
    template_hash: str | None
    created_at: float
    terminating: bool = False


@dataclass
class DeploymentInfo:
    name: str
    desired: int
    ready: int
    available: int
    updated: int
    min_replicas: int
    revision: int
    revision_created_at: float
    current_hash: str | None
    has_previous_revision: bool
    image: str
    version: str
    pods: list[PodInfo] = field(default_factory=list)


@dataclass
class ClusterSnapshot:
    deployments: dict[str, DeploymentInfo]
    pods: dict[str, PodInfo]
    taken_at: float


class ClusterGateway(Protocol):
    def snapshot(self) -> ClusterSnapshot: ...
    def delete_pod(self, name: str) -> None: ...
    def scale(self, deployment: str, replicas: int) -> None: ...
    def rollback(self, deployment: str) -> str: ...
    def release(self, deployment: str, env: dict[str, str], version: str) -> None: ...


def _ts(value: dt.datetime | None) -> float:
    return value.timestamp() if value else 0.0


class KubeGateway:
    """Real implementation backed by the official Kubernetes Python client."""

    def __init__(self, namespace: str, label_selector: str) -> None:
        from kubernetes import client, config

        try:
            config.load_incluster_config()
            log.info("using in-cluster Kubernetes config")
        except config.ConfigException:
            config.load_kube_config()
            log.info("using local kubeconfig")
        self.namespace = namespace
        self.selector = label_selector
        self.core = client.CoreV1Api()
        self.apps = client.AppsV1Api()
        self.api_client = client.ApiClient()

    # -- reads -----------------------------------------------------------------

    def snapshot(self) -> ClusterSnapshot:
        deployments = self.apps.list_namespaced_deployment(self.namespace, label_selector=self.selector)
        replicasets = self.apps.list_namespaced_replica_set(self.namespace, label_selector=self.selector)
        pods = self.core.list_namespaced_pod(self.namespace, label_selector=self.selector)

        rs_by_owner: dict[str, list[tuple[int, object]]] = {}
        for rs in replicasets.items:
            for owner in rs.metadata.owner_references or []:
                if owner.kind == "Deployment":
                    revision = int((rs.metadata.annotations or {}).get(REVISION_ANNOTATION, "0"))
                    rs_by_owner.setdefault(owner.name, []).append((revision, rs))

        result: dict[str, DeploymentInfo] = {}
        for dep in deployments.items:
            name = dep.metadata.name
            history = sorted(rs_by_owner.get(name, []), key=lambda item: item[0])
            current_rs = history[-1][1] if history else None
            annotations = dep.metadata.annotations or {}
            container = dep.spec.template.spec.containers[0]
            desired = dep.spec.replicas or 0
            result[name] = DeploymentInfo(
                name=name,
                desired=desired,
                ready=dep.status.ready_replicas or 0,
                available=dep.status.available_replicas or 0,
                updated=dep.status.updated_replicas or 0,
                min_replicas=int(annotations.get(MIN_REPLICAS_ANNOTATION, "1")),
                revision=int(annotations.get(REVISION_ANNOTATION, "0")),
                revision_created_at=_ts(current_rs.metadata.creation_timestamp) if current_rs else 0.0,
                current_hash=(current_rs.metadata.labels or {}).get("pod-template-hash") if current_rs else None,
                has_previous_revision=len(history) > 1,
                image=container.image,
                version=(dep.spec.template.metadata.labels or {}).get("version", "?"),
            )

        pod_map: dict[str, PodInfo] = {}
        for pod in pods.items:
            labels = pod.metadata.labels or {}
            statuses = pod.status.container_statuses or []
            waiting = next((s.state.waiting.reason for s in statuses if s.state and s.state.waiting), None)
            last_exit = next(
                (
                    s.last_state.terminated.reason
                    for s in statuses
                    if s.last_state and s.last_state.terminated
                ),
                None,
            )
            info = PodInfo(
                name=pod.metadata.name,
                deployment=labels.get("app", "?"),
                phase=pod.status.phase or "Unknown",
                ready=bool(statuses) and all(s.ready for s in statuses),
                restart_count=sum(s.restart_count for s in statuses),
                waiting_reason=waiting,
                last_exit_reason=last_exit,
                ip=pod.status.pod_ip,
                template_hash=labels.get("pod-template-hash"),
                created_at=_ts(pod.metadata.creation_timestamp),
                terminating=pod.metadata.deletion_timestamp is not None,
            )
            pod_map[info.name] = info
            if info.deployment in result:
                result[info.deployment].pods.append(info)

        for dep_info in result.values():
            dep_info.pods.sort(key=lambda p: p.created_at)
        return ClusterSnapshot(result, pod_map, dt.datetime.now(dt.UTC).timestamp())

    # -- writes ----------------------------------------------------------------

    def delete_pod(self, name: str) -> None:
        self.core.delete_namespaced_pod(name, self.namespace, grace_period_seconds=5)

    def scale(self, deployment: str, replicas: int) -> None:
        self.apps.patch_namespaced_deployment_scale(
            deployment, self.namespace, {"spec": {"replicas": replicas}}
        )

    def rollback(self, deployment: str) -> str:
        """Like `kubectl rollout undo`, but targets the last revision that still has healthy pods."""
        replicasets = self.apps.list_namespaced_replica_set(self.namespace, label_selector=self.selector)
        history = sorted(
            (
                (int((rs.metadata.annotations or {}).get(REVISION_ANNOTATION, "0")), rs)
                for rs in replicasets.items
                if any(o.kind == "Deployment" and o.name == deployment for o in rs.metadata.owner_references or [])
            ),
            key=lambda item: item[0],
        )
        target = choose_rollback_target([(rev, rs.status.ready_replicas or 0) for rev, rs in history])
        if target is None:
            raise RuntimeError(f"{deployment} has no previous revision to roll back to")
        current_rev = history[-1][0]
        previous_rev, previous_rs = next((rev, rs) for rev, rs in history if rev == target)
        template = self.api_client.sanitize_for_serialization(previous_rs.spec.template)
        template.get("metadata", {}).get("labels", {}).pop("pod-template-hash", None)
        self.apps.patch_namespaced_deployment(
            deployment,
            self.namespace,
            [{"op": "replace", "path": "/spec/template", "value": template}],
        )
        return f"revision {current_rev} -> {previous_rev}"

    def release(self, deployment: str, env: dict[str, str], version: str) -> None:
        """Roll out a new revision with extra env vars and a new version label."""
        dep = self.apps.read_namespaced_deployment(deployment, self.namespace)
        container = dep.spec.template.spec.containers[0].name
        self.apps.patch_namespaced_deployment(
            deployment,
            self.namespace,
            {
                "spec": {
                    "template": {
                        "metadata": {"labels": {"version": version}},
                        "spec": {
                            "containers": [
                                {
                                    "name": container,
                                    "env": [{"name": k, "value": v} for k, v in env.items()],
                                }
                            ]
                        },
                    }
                }
            },
        )
