"""The self-healing control loop.

Two detection sources feed one remediation path:

* **Kubernetes watch** (`reconcile_once`, every couple of seconds): crash-looping
  containers, restarts, lost pods, replica counts drifting below their minimum.
* **Prometheus alerts** (`handle_alerts`, via Alertmanager webhook): failures that
  Kubernetes probes cannot see - error spikes, memory leaks, latency.

Every incident is then verified against live cluster state on later ticks, so an
incident is only "recovered" once the workload is really healthy again.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from . import metrics
from .config import Settings
from .incidents import Incident, IncidentStore
from .kube import ClusterGateway, ClusterSnapshot, DeploymentInfo, PodInfo
from .policy import (
    ALERT_PLAYBOOKS,
    K8S_PLAYBOOKS,
    Action,
    ActionBudget,
    Cooldown,
    Playbook,
    decide_crashloop_action,
    replicas_to_restore,
)

log = logging.getLogger("healer.engine")

Notifier = Callable[[str], Awaitable[None]]
CHAOS_CORRELATION_SECONDS = 180.0
LATE_SIGNAL_SECONDS = 120.0


@dataclass
class Detection:
    kind: str
    source: str
    deployment: str
    pod: str | None
    playbook: Playbook
    detail: str


def incident_key(kind: str, deployment: str, pod: str | None) -> str:
    """Signals describing the same underlying failure must map to the same key."""
    if kind == "PodCrashLooping":
        return f"crashloop:{deployment}"
    if kind in ("ReplicasBelowMinimum", "ServiceDown"):
        return f"capacity:{deployment}"
    return f"{kind}:{pod or deployment}"


def ready_count(dep: DeploymentInfo) -> int:
    return sum(1 for p in dep.pods if p.ready and not p.terminating)


def is_healthy(dep: DeploymentInfo | None) -> bool:
    if dep is None or dep.desired == 0:
        return False
    live = [p for p in dep.pods if not p.terminating]
    return ready_count(dep) >= dep.desired and all(p.ready for p in live)


class Healer:
    def __init__(
        self,
        gateway: ClusterGateway,
        settings: Settings,
        store: IncidentStore,
        notifier: Notifier | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.gateway = gateway
        self.settings = settings
        self.store = store
        self.notifier = notifier
        self.clock = clock
        self.cooldown = Cooldown(settings.cooldown_seconds)
        self.budget = ActionBudget(settings.budget_max_actions, settings.budget_window_seconds)
        self.snapshot: ClusterSnapshot | None = None
        self.started_at = clock()
        self.lock = asyncio.Lock()
        self._known_pods: dict[str, PodInfo] = {}
        self._prev_desired: dict[str, int] = {}
        self._expected_deletions: dict[str, float] = {}
        self._recent_chaos: dict[str, tuple[str, float]] = {}
        self._availability: dict[str, list[float]] = {}
        self._last_tick: float | None = None

    # ------------------------------------------------------------------ helpers

    async def _call(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.to_thread(fn, *args)

    def _log(self, level: str, source: str, message: str) -> None:
        self.store.log(self.clock(), level, source, message)
        getattr(log, "warning" if level == "warn" else level if level in ("info", "error") else "info")(
            "[%s] %s", source, message
        )

    async def _notify(self, text: str) -> None:
        if self.notifier is None:
            return
        try:
            await self.notifier(text)
        except Exception as exc:  # A broken chat webhook must never stall healing.
            log.warning("notification failed: %s", exc)

    def record_chaos(self, deployment: str, kind: str) -> None:
        self._recent_chaos[deployment] = (kind, self.clock())

    def expect_deletion(self, pod: str) -> None:
        self._expected_deletions[pod] = self.clock()

    # --------------------------------------------------------------- detection

    def _observe(self, snap: ClusterSnapshot) -> list[Detection]:
        now = self.clock()
        detections: list[Detection] = []
        threshold = self.settings.crashloop_restart_threshold

        if self._last_tick is not None:
            elapsed = min(now - self._last_tick, 10.0)
            for dep in snap.deployments.values():
                bucket = self._availability.setdefault(dep.name, [0.0, 0.0])
                bucket[1] += elapsed
                if ready_count(dep) > 0:
                    bucket[0] += elapsed
        self._last_tick = now

        for dep in snap.deployments.values():
            target = replicas_to_restore(dep.desired, dep.min_replicas)
            if target is not None:
                detections.append(Detection(
                    "ReplicasBelowMinimum", "kubernetes", dep.name, None,
                    K8S_PLAYBOOKS["ReplicasBelowMinimum"],
                    f"replicas={dep.desired} but selfheal/min-replicas={dep.min_replicas}",
                ))
            scaled_down = dep.desired < self._prev_desired.get(dep.name, dep.desired)

            for pod in dep.pods:
                previous = self._known_pods.get(pod.name)
                if pod.waiting_reason == "CrashLoopBackOff" and pod.restart_count >= threshold:
                    detections.append(Detection(
                        "PodCrashLooping", "kubernetes", dep.name, pod.name,
                        K8S_PLAYBOOKS["PodCrashLooping"],
                        f"{pod.name} in CrashLoopBackOff after {pod.restart_count} restarts",
                    ))
                elif previous is not None and pod.restart_count > previous.restart_count:
                    reason = pod.last_exit_reason or "unknown"
                    detections.append(Detection(
                        "ContainerRestarted", "kubernetes", dep.name, pod.name,
                        K8S_PLAYBOOKS["ContainerRestarted"],
                        f"{pod.name} restarted (last exit: {reason}, restarts={pod.restart_count})",
                    ))

            for name, previous in self._known_pods.items():
                if previous.deployment != dep.name:
                    continue
                current = snap.pods.get(name)
                if current is not None and not current.terminating:
                    continue
                if name in self._expected_deletions or scaled_down:
                    continue
                if previous.template_hash != dep.current_hash:
                    continue  # Old revision being rolled out - that is a deploy, not a failure.
                detections.append(Detection(
                    "PodLost", "kubernetes", dep.name, name, K8S_PLAYBOOKS["PodLost"],
                    f"{name} was terminated unexpectedly",
                ))

        self._known_pods = {name: p for name, p in snap.pods.items() if not p.terminating}
        self._prev_desired = {name: d.desired for name, d in snap.deployments.items()}
        self._expected_deletions = {
            pod: ts for pod, ts in self._expected_deletions.items() if now - ts < 300
        }
        return detections

    async def reconcile_once(self) -> None:
        started = time.perf_counter()
        snap: ClusterSnapshot = await self._call(self.gateway.snapshot)
        async with self.lock:
            self.snapshot = snap
            for detection in self._observe(snap):
                await self._handle_detection(detection)
            await self._verify_open(snap)
        metrics.RECONCILE_SECONDS.observe(time.perf_counter() - started)
        metrics.OPEN_INCIDENTS.set(len(self.store.open_incidents()))

    async def handle_alerts(self, payload: dict[str, Any]) -> int:
        """Process an Alertmanager webhook payload. Returns how many alerts were acted on."""
        acted = 0
        async with self.lock:
            snap = self.snapshot
            for alert in payload.get("alerts", []):
                labels: dict[str, str] = alert.get("labels", {})
                name = labels.get("alertname", "?")
                playbook = ALERT_PLAYBOOKS.get(name)
                if playbook is None:
                    self._log("info", "prometheus", f"alert {name} has no playbook; ignoring")
                    continue
                pod = labels.get("pod")
                deployment = labels.get("deployment") or labels.get("service")
                if pod and snap and pod in snap.pods:
                    deployment = snap.pods[pod].deployment
                if not deployment:
                    continue
                key = incident_key(name, deployment, pod)

                if alert.get("status") == "resolved":
                    latest = self.store.find_latest(key)
                    if latest:
                        latest.note(self.clock(), "signal", f"Prometheus alert {name} resolved")
                    self._log("info", "prometheus", f"alert {name} resolved for {pod or deployment}")
                    continue

                if snap is not None:
                    if deployment not in snap.deployments:
                        continue
                    if pod and (pod not in snap.pods or snap.pods[pod].terminating):
                        self._log("info", "prometheus", f"alert {name} for {pod}: pod already replaced")
                        continue
                summary = alert.get("annotations", {}).get("summary", "")
                await self._handle_detection(Detection(
                    name, "prometheus", deployment, pod, playbook, summary or f"alert {name} firing",
                ))
                acted += 1
        return acted

    # ------------------------------------------------------------- remediation

    async def _handle_detection(self, det: Detection) -> None:
        now = self.clock()
        key = incident_key(det.kind, det.deployment, det.pod)

        existing = self.store.find_open(key)
        if existing is not None:
            if det.source != existing.source and not any(
                e.message.startswith(f"Confirmed by {det.source}") for e in existing.timeline
            ):
                existing.note(now, "signal", f"Confirmed by {det.source}: {det.detail}")
            return
        latest = self.store.find_latest(key)
        if latest is not None and latest.resolved_at and now - latest.resolved_at < LATE_SIGNAL_SECONDS:
            if det.source == "prometheus":
                latest.note(now, "signal", f"Late confirmation from prometheus: {det.detail}")
                return
            if latest.status == "recovered" and now - latest.resolved_at < self.settings.cooldown_seconds:
                return

        chaos = self._recent_chaos.get(det.deployment)
        chaos_kind, chaos_at = (chaos if chaos and now - chaos[1] < CHAOS_CORRELATION_SECONDS else (None, None))
        incident = self.store.open(
            key=key, kind=det.kind, title=det.playbook.title, severity=det.playbook.severity,
            source=det.source, deployment=det.deployment, pod=det.pod,
            action=det.playbook.action.value, detected_at=now,
            chaos_kind=chaos_kind, chaos_at=chaos_at,
        )
        incident.note(now, "detected", f"[{det.source}] {det.detail}")
        metrics.INCIDENTS.labels(det.kind, det.source).inc()
        self._log("warn", det.source, f"#{incident.id} {det.playbook.title} on {det.pod or det.deployment}")

        if det.kind == "PodCrashLooping":
            self._supersede_restarts(det.deployment, incident)

        await self._remediate(incident, det)

    def _supersede_restarts(self, deployment: str, parent: Incident) -> None:
        for other in self.store.open_incidents():
            if other is not parent and other.deployment == deployment and other.kind == "ContainerRestarted":
                other.status = "superseded"
                other.resolved_at = self.clock()
                other.note(self.clock(), "superseded", f"Merged into crash-loop incident #{parent.id}")

    async def _remediate(self, incident: Incident, det: Detection) -> None:
        now = self.clock()
        snap = self.snapshot
        dep = snap.deployments.get(det.deployment) if snap else None
        action = det.playbook.action

        if det.kind == "PodCrashLooping":
            pod = snap.pods.get(det.pod) if snap and det.pod else None
            decided = decide_crashloop_action(
                restart_count=pod.restart_count if pod else self.settings.crashloop_restart_threshold,
                threshold=self.settings.crashloop_restart_threshold,
                has_previous_revision=bool(dep and dep.has_previous_revision),
                current_revision_age_seconds=now - dep.revision_created_at if dep else 1e9,
                rollback_window_seconds=self.settings.rollback_window_seconds,
                ready_pods_on_current_revision=sum(
                    1 for p in (dep.pods if dep else [])
                    if p.ready and not p.terminating and p.template_hash == dep.current_hash
                ) if dep else 0,
            )
            action = decided or Action.RESTART_POD
            incident.action = action.value

        if action is Action.OBSERVE:
            incident.status = "remediating"
            incident.recovery_check = "pod_ready" if det.kind == "ContainerRestarted" else "deployment_available"
            who = "kubelet restarted the container" if det.kind == "ContainerRestarted" else "ReplicaSet is replacing the pod"
            incident.note(now, "action", f"Kubernetes self-heal in progress ({who}); healer verifying recovery")
            return

        target_key = f"{action.value}:{det.pod or det.deployment}"
        if not self.cooldown.ready(target_key, now):
            incident.status = "remediating"
            incident.note(now, "action", f"Cooldown active ({self.cooldown.remaining(target_key, now):.0f}s); waiting for previous action")
            return
        if not self.budget.allow(det.deployment, now):
            incident.status = "escalated"
            incident.resolved_at = now
            incident.note(
                now, "escalated",
                f"Circuit breaker open: {self.budget.used(det.deployment, now)} actions on {det.deployment} "
                f"in {self.settings.budget_window_seconds:.0f}s. Paging a human instead of retrying.",
            )
            metrics.ACTIONS.labels(action.value, "circuit_open").inc()
            self._log("error", "healer", f"#{incident.id} escalated: circuit breaker open for {det.deployment}")
            await self._notify(f":rotating_light: Escalation #{incident.id}: {incident.title} on {det.deployment} - automatic healing paused")
            return

        incident.status = "remediating"
        try:
            message = await self._execute(action, det, dep)
        except Exception as exc:
            incident.status = "failed"
            incident.resolved_at = self.clock()
            incident.note(self.clock(), "failed", f"Action {action.value} failed: {exc}")
            metrics.ACTIONS.labels(action.value, "error").inc()
            self._log("error", "healer", f"#{incident.id} action {action.value} failed: {exc}")
            await self._notify(f":x: Healing failed #{incident.id} on {det.deployment}: {exc}")
            return

        self.cooldown.mark(target_key, now)
        self.budget.record(det.deployment, now)
        self.store.actions_taken += 1
        metrics.ACTIONS.labels(action.value, "dry_run" if self.settings.dry_run else "ok").inc()
        incident.note(self.clock(), "action", message)
        self._log("info", "healer", f"#{incident.id} {message}")
        await self._notify(f":wrench: #{incident.id} {incident.title} on {det.deployment}: {message}")

    async def _execute(self, action: Action, det: Detection, dep: DeploymentInfo | None) -> str:
        prefix = "[dry-run] would have " if self.settings.dry_run else ""
        if action is Action.RESTART_POD:
            if not det.pod:
                raise RuntimeError("no pod to restart")
            if not self.settings.dry_run:
                self.expect_deletion(det.pod)
                await self._call(self.gateway.delete_pod, det.pod)
            return f"{prefix}Deleted pod {det.pod}; ReplicaSet will schedule a fresh replica"
        if action is Action.RESTORE_REPLICAS:
            target = max(dep.min_replicas if dep else 1, 1)
            if not self.settings.dry_run:
                await self._call(self.gateway.scale, det.deployment, target)
            return f"{prefix}Scaled {det.deployment} back to {target} replicas (declared minimum)"
        if action is Action.ROLLBACK:
            if self.settings.dry_run:
                return f"{prefix}rolled back {det.deployment}"
            detail = await self._call(self.gateway.rollback, det.deployment)
            return f"Rolled back {det.deployment} ({detail}) - bad release reverted"
        raise RuntimeError(f"unsupported action {action}")

    # ---------------------------------------------------------------- recovery

    async def _verify_open(self, snap: ClusterSnapshot) -> None:
        now = self.clock()
        for incident in self.store.open_incidents():
            if incident.status != "remediating":
                continue
            dep = snap.deployments.get(incident.deployment)
            pod = snap.pods.get(incident.pod) if incident.pod else None
            pod_gone = incident.pod is not None and (pod is None or pod.terminating)

            if incident.action == Action.ROLLBACK.value:
                ok = is_healthy(dep) and dep is not None and dep.updated >= dep.desired and not any(
                    p.waiting_reason == "CrashLoopBackOff" for p in dep.pods if not p.terminating
                )
            elif incident.action == Action.RESTART_POD.value:
                ok = pod_gone and is_healthy(dep)
            elif incident.recovery_check == "pod_ready" and pod is not None and not pod.terminating:
                ok = pod.ready and pod.waiting_reason is None
            else:
                ok = is_healthy(dep)

            if ok and dep is not None:
                incident.status = "recovered"
                incident.resolved_at = now
                seconds = now - incident.detected_at
                incident.note(now, "recovered", f"Verified healthy: {ready_count(dep)}/{dep.desired} replicas ready ({seconds:.1f}s)")
                metrics.RECOVERY_SECONDS.labels(incident.kind).observe(seconds)
                self._log("info", "healer", f"#{incident.id} recovered in {seconds:.1f}s")
                await self._notify(f":white_check_mark: #{incident.id} {incident.title} on {incident.deployment} recovered in {seconds:.1f}s")
            elif now - incident.detected_at > self.settings.recovery_timeout_seconds:
                incident.status = "failed"
                incident.resolved_at = now
                incident.note(now, "failed", "Did not recover within the recovery timeout")
                self._log("error", "healer", f"#{incident.id} did not recover in time")
                await self._notify(f":x: #{incident.id} {incident.title} on {incident.deployment} did not recover")

    # ------------------------------------------------------------------- views

    def availability(self) -> dict[str, float | None]:
        result: dict[str, float | None] = {}
        for name, (up, total) in self._availability.items():
            result[name] = (up / total) if total > 0 else None
        return result

    def budget_view(self) -> dict[str, int]:
        now = self.clock()
        names = self.snapshot.deployments if self.snapshot else {}
        return {name: self.budget.used(name, now) for name in names}
