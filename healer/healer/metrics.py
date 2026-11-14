"""Prometheus metrics the healer exposes about its own behaviour."""

from prometheus_client import Counter, Gauge, Histogram

INCIDENTS = Counter("healer_incidents_total", "Incidents opened", ["kind", "source"])
ACTIONS = Counter("healer_actions_total", "Remediation actions attempted", ["action", "result"])
RECOVERY_SECONDS = Histogram(
    "healer_recovery_seconds",
    "Time from detection to verified recovery",
    ["kind"],
    buckets=(2, 5, 10, 15, 20, 30, 45, 60, 90, 120, 180),
)
OPEN_INCIDENTS = Gauge("healer_open_incidents", "Incidents currently open")
RECONCILE_SECONDS = Histogram(
    "healer_reconcile_seconds", "Duration of one reconcile pass",
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5),
)
CHAOS = Counter("healer_chaos_injections_total", "Chaos experiments injected", ["service", "kind"])
