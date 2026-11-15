import asyncio

import pytest
from conftest import make_settings

from healer.engine import Healer, incident_key
from healer.incidents import IncidentStore


def run(coro):
    return asyncio.run(coro)


def alert(name, status="firing", **labels):
    return {"alerts": [{"status": status, "labels": {"alertname": name, **labels}, "annotations": {"summary": "s"}}]}


@pytest.fixture
def healer(cluster, clock):
    cluster.add_deployment("orders")
    cluster.add_deployment("payments")
    h = Healer(cluster, make_settings(), IncidentStore(), clock=clock)
    run(h.reconcile_once())  # baseline snapshot
    return h


def only_incident(healer):
    incidents = healer.store.all()
    assert len(incidents) == 1, [i.title for i in incidents]
    return incidents[0]


def test_steady_state_creates_no_incidents(healer, clock):
    for _ in range(5):
        clock.advance(2)
        run(healer.reconcile_once())
    assert healer.store.all() == []
    assert healer.availability()["orders"] == 1.0


def test_killed_pod_is_detected_and_recovery_verified(healer, cluster, clock):
    victim = cluster.deployments["orders"].pods[0].name
    cluster.remove_pod(victim)
    clock.advance(2)
    run(healer.reconcile_once())
    incident = only_incident(healer)
    assert incident.kind == "PodLost" and incident.status == "remediating"
    assert cluster.calls == []  # Kubernetes heals this itself; healer only verifies.

    replacement = cluster.add_pod("orders", ready=False)
    clock.advance(2)
    run(healer.reconcile_once())
    assert incident.status == "remediating"
    cluster.pod(replacement.name).ready = True
    clock.advance(3)
    run(healer.reconcile_once())
    assert incident.status == "recovered"
    assert incident.recovery_seconds == 5


def test_error_alert_restarts_the_pod_and_waits_for_replacement(healer, cluster, clock):
    sick = cluster.deployments["orders"].pods[1].name
    acted = run(healer.handle_alerts(alert("HighErrorRate", service="orders", pod=sick)))
    assert acted == 1
    assert cluster.calls == [("delete_pod", sick)]
    incident = only_incident(healer)
    assert incident.source == "prometheus" and incident.action == "restart_pod"

    clock.advance(2)
    run(healer.reconcile_once())  # pod terminating, no replacement yet
    assert incident.status == "remediating"
    assert len(healer.store.all()) == 1  # our own deletion is not a "PodLost"

    cluster.remove_pod(sick)
    cluster.add_pod("orders")
    clock.advance(4)
    run(healer.reconcile_once())
    assert incident.status == "recovered"
    assert incident.recovery_seconds == 6


def test_repeated_alert_is_deduplicated(healer, cluster):
    sick = cluster.deployments["orders"].pods[0].name
    run(healer.handle_alerts(alert("HighMemoryUsage", service="orders", pod=sick)))
    run(healer.handle_alerts(alert("HighMemoryUsage", service="orders", pod=sick)))
    assert len(healer.store.all()) == 1
    assert cluster.calls == [("delete_pod", sick)]


def test_alert_for_already_replaced_pod_is_ignored(healer, cluster):
    assert run(healer.handle_alerts(alert("HighLatency", service="orders", pod="orders-gone"))) == 0
    assert healer.store.all() == []


def test_unknown_alert_and_unmanaged_service_are_ignored(healer):
    assert run(healer.handle_alerts(alert("PodRestartBurst", service="orders"))) == 0
    assert run(healer.handle_alerts(alert("ServiceDown", deployment="loadgen"))) == 0
    assert healer.store.all() == []


def test_resolved_alert_is_noted_on_incident(healer, cluster):
    sick = cluster.deployments["orders"].pods[0].name
    run(healer.handle_alerts(alert("HighErrorRate", service="orders", pod=sick)))
    run(healer.handle_alerts(alert("HighErrorRate", status="resolved", service="orders", pod=sick)))
    assert any("resolved" in e.message for e in only_incident(healer).timeline)


def test_scale_to_zero_is_restored_to_declared_minimum(healer, cluster, clock):
    dep = cluster.deployments["payments"]
    dep.desired, dep.pods = 0, []
    clock.advance(2)
    run(healer.reconcile_once())
    incident = only_incident(healer)
    assert incident.kind == "ReplicasBelowMinimum"
    assert cluster.calls == [("scale", "payments", "2")]

    # Prometheus confirms the outage a bit later: same incident, not a new one.
    run(healer.handle_alerts(alert("ServiceDown", deployment="payments")))
    assert len(healer.store.all()) == 1
    assert any("Confirmed by prometheus" in e.message for e in incident.timeline)

    cluster.add_pod("payments")
    cluster.add_pod("payments")
    clock.advance(6)
    run(healer.reconcile_once())
    assert incident.status == "recovered"
    # Late ServiceDown after recovery is attached, not re-opened.
    run(healer.handle_alerts(alert("ServiceDown", deployment="payments")))
    assert len(healer.store.all()) == 1


def test_bad_release_crashloop_triggers_rollback(healer, cluster, clock):
    dep = cluster.deployments["orders"]
    dep.revision, dep.has_previous_revision, dep.current_hash = 2, True, "h2"
    dep.revision_created_at = clock() - 30
    bad = cluster.add_pod("orders", ready=False, template_hash="h2")
    clock.advance(2)
    run(healer.reconcile_once())

    cluster.pod(bad.name).restart_count = 1
    clock.advance(2)
    run(healer.reconcile_once())
    restart_incident = only_incident(healer)
    assert restart_incident.kind == "ContainerRestarted"

    cluster.pod(bad.name).restart_count = 2
    cluster.pod(bad.name).waiting_reason = "CrashLoopBackOff"
    clock.advance(2)
    run(healer.reconcile_once())
    assert ("rollback", "orders") in cluster.calls
    crash = healer.store.find_latest(incident_key("PodCrashLooping", "orders", None))
    assert crash.action == "rollback"
    assert restart_incident.status == "superseded"

    # Rollback done: bad pod gone, the old pods are the current revision again.
    cluster.remove_pod(bad.name)
    dep.current_hash = "h1"
    clock.advance(4)
    run(healer.reconcile_once())
    assert crash.status == "recovered"
    assert healer.store.stats()["total"] == 1  # superseded incident not counted


def test_crashloop_with_healthy_siblings_restarts_pod_instead(healer, cluster, clock):
    pod = cluster.deployments["orders"].pods[0]
    cluster.deployments["orders"].has_previous_revision = True
    cluster.deployments["orders"].revision_created_at = clock() - 10
    pod.restart_count, pod.waiting_reason, pod.ready = 3, "CrashLoopBackOff", False
    clock.advance(2)
    run(healer.reconcile_once())
    assert cluster.calls == [("delete_pod", pod.name)]


def test_container_restart_is_observed_until_ready(healer, cluster, clock):
    pod = cluster.deployments["orders"].pods[0]
    pod.restart_count, pod.ready, pod.last_exit_reason = 1, False, "Error"
    clock.advance(2)
    run(healer.reconcile_once())
    incident = only_incident(healer)
    assert incident.kind == "ContainerRestarted" and "Error" in incident.timeline[0].message
    pod.ready = True
    clock.advance(3)
    run(healer.reconcile_once())
    assert incident.status == "recovered"


def test_rolling_deploy_is_not_reported_as_pod_loss(healer, cluster, clock):
    dep = cluster.deployments["orders"]
    dep.current_hash = "h2"
    old = [p.name for p in dep.pods]
    cluster.add_pod("orders", template_hash="h2")
    cluster.add_pod("orders", template_hash="h2")
    for name in old:
        cluster.remove_pod(name)
    clock.advance(2)
    run(healer.reconcile_once())
    assert healer.store.all() == []


def test_circuit_breaker_escalates_after_budget(cluster, clock):
    cluster.add_deployment("orders", replicas=4)
    h = Healer(cluster, make_settings(budget_max_actions=2, cooldown_seconds=0), IncidentStore(), clock=clock)
    run(h.reconcile_once())
    pods = [p.name for p in cluster.deployments["orders"].pods]
    for name in pods[:3]:
        run(h.handle_alerts(alert("HighErrorRate", service="orders", pod=name)))
        clock.advance(1)
    statuses = [i.status for i in h.store.all()]
    assert statuses == ["remediating", "remediating", "escalated"]
    assert len(cluster.calls) == 2


def test_cooldown_prevents_hammering_same_target(cluster, clock):
    cluster.add_deployment("orders")
    h = Healer(cluster, make_settings(cooldown_seconds=60), IncidentStore(), clock=clock)
    run(h.reconcile_once())
    pod = cluster.deployments["orders"].pods[0].name
    run(h.handle_alerts(alert("HighErrorRate", service="orders", pod=pod)))
    first = h.store.all()[0]
    first.status = "failed"  # pretend it closed so a new incident can open
    cluster.pod(pod).terminating = False
    run(h.handle_alerts(alert("HighErrorRate", service="orders", pod=pod)))
    second = h.store.all()[1]
    assert len(cluster.calls) == 1
    assert "Cooldown" in second.timeline[-1].message


def test_failed_action_marks_incident_failed(healer, cluster):
    cluster.fail_next = RuntimeError("api down")
    pod = cluster.deployments["orders"].pods[0].name
    run(healer.handle_alerts(alert("HighErrorRate", service="orders", pod=pod)))
    incident = only_incident(healer)
    assert incident.status == "failed"
    assert "api down" in incident.timeline[-1].message


def test_recovery_timeout_fails_incident(healer, cluster, clock):
    pod = cluster.deployments["orders"].pods[0]
    pod.restart_count, pod.ready = 1, False
    clock.advance(2)
    run(healer.reconcile_once())
    clock.advance(500)
    run(healer.reconcile_once())
    assert only_incident(healer).status == "failed"


def test_dry_run_never_touches_the_cluster(cluster, clock):
    cluster.add_deployment("orders")
    h = Healer(cluster, make_settings(dry_run=True), IncidentStore(), clock=clock)
    run(h.reconcile_once())
    pod = cluster.deployments["orders"].pods[0].name
    run(h.handle_alerts(alert("HighErrorRate", service="orders", pod=pod)))
    assert cluster.calls == []
    assert "dry-run" in h.store.all()[0].timeline[-1].message


def test_chaos_correlation_measures_time_to_detect(healer, cluster, clock):
    healer.record_chaos("orders", "kill_pod")
    clock.advance(3)
    cluster.remove_pod(cluster.deployments["orders"].pods[0].name)
    run(healer.reconcile_once())
    incident = only_incident(healer)
    assert incident.chaos_kind == "kill_pod"
    assert incident.detection_seconds == 3


def test_notifier_failures_do_not_break_healing(cluster, clock):
    sent = []

    async def flaky(text):
        sent.append(text)
        raise RuntimeError("chat is down")

    cluster.add_deployment("orders")
    h = Healer(cluster, make_settings(), IncidentStore(), notifier=flaky, clock=clock)
    run(h.reconcile_once())
    pod = cluster.deployments["orders"].pods[0].name
    run(h.handle_alerts(alert("HighErrorRate", service="orders", pod=pod)))
    assert sent and cluster.calls == [("delete_pod", pod)]


def _bad_release(cluster, clock):
    dep = cluster.deployments["payments"]
    dep.revision, dep.has_previous_revision, dep.current_hash = 2, True, "h2"
    dep.revision_created_at = clock() - 20
    return dep, cluster.add_pod("payments", ready=False, template_hash="h2")


def test_prometheus_crashloop_alert_rolls_back_before_local_threshold(healer, cluster, clock):
    _, bad = _bad_release(cluster, clock)
    cluster.pod(bad.name).restart_count = 1  # Prometheus can fire before our own threshold of 2
    run(healer.reconcile_once())
    run(healer.handle_alerts(alert("PodCrashLooping", pod=bad.name)))
    assert ("rollback", "payments") in cluster.calls
    assert not any(c[0] == "delete_pod" for c in cluster.calls)


def test_restart_that_does_not_fix_crashloop_escalates_in_same_incident(healer, cluster, clock):
    dep, bad = _bad_release(cluster, clock)
    dep.has_previous_revision = False  # first decision can only be a pod restart
    run(healer.reconcile_once())
    cluster.pod(bad.name).restart_count, cluster.pod(bad.name).waiting_reason = 2, "CrashLoopBackOff"
    clock.advance(2)
    run(healer.reconcile_once())
    assert cluster.calls == [("delete_pod", bad.name)]
    incident = healer.store.find_open(incident_key("PodCrashLooping", "payments", None))

    cluster.remove_pod(bad.name)
    dep.has_previous_revision = True
    replacement = cluster.add_pod("payments", ready=False, template_hash="h2")
    clock.advance(2)
    run(healer.reconcile_once())
    cluster.pod(replacement.name).restart_count = 2
    cluster.pod(replacement.name).waiting_reason = "CrashLoopBackOff"
    clock.advance(2)
    run(healer.reconcile_once())
    assert ("rollback", "payments") in cluster.calls
    assert incident.action == "rollback"
    assert [i.kind for i in healer.store.all() if i.status != "superseded"] == ["PodCrashLooping"]
