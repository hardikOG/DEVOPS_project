"""Runtime configuration, read once from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.getenv(name, str(default)))


def _float(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _bool(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    namespace: str
    managed_label: str
    reconcile_interval: float
    cooldown_seconds: float
    budget_max_actions: int
    budget_window_seconds: float
    crashloop_restart_threshold: int
    rollback_window_seconds: float
    recovery_timeout_seconds: float
    chaos_enabled: bool
    chaos_token: str
    prometheus_url: str
    grafana_url: str
    alertmanager_url: str
    jenkins_url: str
    notify_webhook_url: str
    dry_run: bool

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            namespace=os.getenv("HEALER_NAMESPACE", "self-healing"),
            managed_label=os.getenv("HEALER_MANAGED_LABEL", "selfheal/managed=true"),
            reconcile_interval=_float("HEALER_RECONCILE_INTERVAL", 2.0),
            cooldown_seconds=_float("HEALER_COOLDOWN_SECONDS", 30.0),
            budget_max_actions=_int("HEALER_BUDGET_MAX_ACTIONS", 6),
            budget_window_seconds=_float("HEALER_BUDGET_WINDOW_SECONDS", 600.0),
            crashloop_restart_threshold=_int("HEALER_CRASHLOOP_RESTARTS", 2),
            rollback_window_seconds=_float("HEALER_ROLLBACK_WINDOW_SECONDS", 900.0),
            recovery_timeout_seconds=_float("HEALER_RECOVERY_TIMEOUT_SECONDS", 180.0),
            chaos_enabled=_bool("CHAOS_ENABLED", True),
            chaos_token=os.getenv("CHAOS_TOKEN", ""),
            prometheus_url=os.getenv("PROMETHEUS_URL", "http://prometheus.monitoring.svc:9090"),
            grafana_url=os.getenv("GRAFANA_PUBLIC_URL", "http://localhost:30030"),
            alertmanager_url=os.getenv("ALERTMANAGER_PUBLIC_URL", "http://localhost:30093"),
            jenkins_url=os.getenv("JENKINS_PUBLIC_URL", "http://localhost:8080"),
            notify_webhook_url=os.getenv("NOTIFY_WEBHOOK_URL", ""),
            dry_run=_bool("HEALER_DRY_RUN", False),
        )
