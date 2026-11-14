"""Pure decision logic for the healer: no Kubernetes or network calls live here.

Keeping these rules side-effect free makes them cheap to unit-test exhaustively
and keeps the "what should we do" question separate from "how do we do it".
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import Enum


class Action(str, Enum):
    RESTART_POD = "restart_pod"
    RESTORE_REPLICAS = "restore_replicas"
    ROLLBACK = "rollback"
    OBSERVE = "observe"  # Kubernetes already healed it; we only verify recovery.


@dataclass(frozen=True)
class Playbook:
    """How the healer responds to one kind of failure."""

    title: str
    action: Action
    severity: str
    description: str


# Alert names come from k8s/monitoring/prometheus-rules.yaml.
ALERT_PLAYBOOKS: dict[str, Playbook] = {
    "HighErrorRate": Playbook(
        "Error rate spike", Action.RESTART_POD, "critical",
        "Pod is answering >30% of requests with 5xx while its probes still pass.",
    ),
    "HighMemoryUsage": Playbook(
        "Memory leak", Action.RESTART_POD, "warning",
        "Pod memory is climbing towards its limit; recycle it before the OOM killer does.",
    ),
    "HighLatency": Playbook(
        "Latency degradation", Action.RESTART_POD, "warning",
        "Pod p95 latency is above 500ms.",
    ),
    "ServiceDown": Playbook(
        "Service outage", Action.RESTORE_REPLICAS, "critical",
        "Deployment has zero available replicas.",
    ),
    "PodCrashLooping": Playbook(
        "Crash loop", Action.ROLLBACK, "critical",
        "Containers keep dying on start; likely a bad release.",
    ),
}

K8S_PLAYBOOKS: dict[str, Playbook] = {
    "ContainerRestarted": Playbook(
        "Container crashed", Action.OBSERVE, "warning",
        "Container exited or failed its liveness probe; kubelet restarted it.",
    ),
    "PodLost": Playbook(
        "Pod terminated", Action.OBSERVE, "warning",
        "Pod disappeared; the ReplicaSet is scheduling a replacement.",
    ),
    "ReplicasBelowMinimum": Playbook(
        "Capacity drift", Action.RESTORE_REPLICAS, "critical",
        "Deployment was scaled below its declared minimum replica count.",
    ),
    "PodCrashLooping": ALERT_PLAYBOOKS["PodCrashLooping"],
}


def playbook_for_alert(alertname: str) -> Playbook | None:
    """Return the playbook for a Prometheus alert, or None if we do not act on it."""
    return ALERT_PLAYBOOKS.get(alertname)


def decide_crashloop_action(
    *,
    restart_count: int,
    threshold: int,
    has_previous_revision: bool,
    current_revision_age_seconds: float,
    rollback_window_seconds: float,
    ready_pods_on_current_revision: int,
) -> Action | None:
    """Pick a remediation for a crash-looping pod.

    A fresh revision where *no* pod has managed to become ready is almost certainly
    a bad release, so we roll back. If some pods of the same revision are healthy,
    the release is fine and this pod is the problem: recycling it (which also resets
    kubelet's exponential back-off) is the safer move.
    """
    if restart_count < threshold:
        return None
    if (
        has_previous_revision
        and current_revision_age_seconds <= rollback_window_seconds
        and ready_pods_on_current_revision == 0
    ):
        return Action.ROLLBACK
    return Action.RESTART_POD


def replicas_to_restore(current: int, minimum: int) -> int | None:
    """Return the replica count to scale back to, or None when no change is needed."""
    if minimum > 0 and current < minimum:
        return minimum
    return None


class Cooldown:
    """Suppresses repeat actions against the same target inside a quiet period."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self._last: dict[str, float] = {}

    def ready(self, key: str, now: float) -> bool:
        last = self._last.get(key)
        return last is None or now - last >= self.seconds

    def mark(self, key: str, now: float) -> None:
        self._last[key] = now

    def remaining(self, key: str, now: float) -> float:
        last = self._last.get(key)
        return 0.0 if last is None else max(0.0, self.seconds - (now - last))


class ActionBudget:
    """Circuit breaker: caps automated actions per deployment in a sliding window.

    If the healer keeps "fixing" the same deployment, the fix is not working and a
    human needs to look; continuing would only hide the problem (or make it worse).
    """

    def __init__(self, max_actions: int, window_seconds: float) -> None:
        self.max_actions = max_actions
        self.window_seconds = window_seconds
        self._history: dict[str, deque[float]] = defaultdict(deque)

    def _trim(self, key: str, now: float) -> deque[float]:
        history = self._history[key]
        while history and now - history[0] > self.window_seconds:
            history.popleft()
        return history

    def allow(self, key: str, now: float) -> bool:
        return len(self._trim(key, now)) < self.max_actions

    def record(self, key: str, now: float) -> None:
        self._trim(key, now).append(now)

    def used(self, key: str, now: float) -> int:
        return len(self._trim(key, now))
