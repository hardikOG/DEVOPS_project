from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "healer"))
sys.path.insert(0, str(ROOT / "services" / "demo-service"))

from healer.config import Settings  # noqa: E402
from healer.kube import ClusterSnapshot, DeploymentInfo, PodInfo  # noqa: E402


class FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeCluster:
    """In-memory stand-in for the Kubernetes API, mutated directly by tests."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.deployments: dict[str, DeploymentInfo] = {}
        self.calls: list[tuple[str, ...]] = []
        self.fail_next: Exception | None = None
        self._counter = 0

    def add_deployment(self, name: str, replicas: int = 2, min_replicas: int = 2, revisions: int = 1) -> DeploymentInfo:
        dep = DeploymentInfo(
            name=name, desired=replicas, ready=replicas, available=replicas, updated=replicas,
            min_replicas=min_replicas, revision=revisions, revision_created_at=self.clock() - 3600,
            current_hash="h1", has_previous_revision=revisions > 1, image="demo:1", version="1.0.0",
        )
        self.deployments[name] = dep
        for _ in range(replicas):
            self.add_pod(name)
        return dep

    def add_pod(self, deployment: str, ready: bool = True, template_hash: str | None = None) -> PodInfo:
        self._counter += 1
        dep = self.deployments[deployment]
        pod = PodInfo(
            name=f"{deployment}-{self._counter}", deployment=deployment, phase="Running", ready=ready,
            restart_count=0, waiting_reason=None, last_exit_reason=None, ip=f"10.0.0.{self._counter}",
            template_hash=template_hash or dep.current_hash, created_at=self.clock(),
        )
        dep.pods.append(pod)
        return pod

    def pod(self, name: str) -> PodInfo:
        return next(p for d in self.deployments.values() for p in d.pods if p.name == name)

    def remove_pod(self, name: str) -> None:
        for dep in self.deployments.values():
            dep.pods = [p for p in dep.pods if p.name != name]

    # -- gateway protocol ------------------------------------------------------

    def snapshot(self) -> ClusterSnapshot:
        deployments = {n: replace(d, pods=[replace(p) for p in d.pods]) for n, d in self.deployments.items()}
        pods = {p.name: p for d in deployments.values() for p in d.pods}
        return ClusterSnapshot(deployments, pods, self.clock())

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            exc, self.fail_next = self.fail_next, None
            raise exc

    def delete_pod(self, name: str) -> None:
        self._maybe_fail()
        self.calls.append(("delete_pod", name))
        self.pod(name).terminating = True

    def scale(self, deployment: str, replicas: int) -> None:
        self._maybe_fail()
        self.calls.append(("scale", deployment, str(replicas)))
        self.deployments[deployment].desired = replicas

    def rollback(self, deployment: str) -> str:
        self._maybe_fail()
        self.calls.append(("rollback", deployment))
        return "revision 2 -> 1"

    def release(self, deployment: str, env: dict[str, str], version: str) -> None:
        self.calls.append(("release", deployment, version))


def make_settings(**overrides: object) -> Settings:
    base = Settings(
        namespace="self-healing", managed_label="selfheal/managed=true", reconcile_interval=0.01,
        cooldown_seconds=30, budget_max_actions=3, budget_window_seconds=600,
        crashloop_restart_threshold=2, rollback_window_seconds=900, recovery_timeout_seconds=120,
        chaos_enabled=True, chaos_token="", prometheus_url="http://prometheus.invalid:9090",
        grafana_url="g", alertmanager_url="a", jenkins_url="j", notify_webhook_url="", dry_run=False,
    )
    return replace(base, **overrides)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def cluster(clock: FakeClock) -> FakeCluster:
    return FakeCluster(clock)
