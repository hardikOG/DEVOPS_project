"""Fault injection used by the dashboard and the automated chaos test.

Pod-level faults are sent to the demo service's /chaos endpoints on a specific
pod IP; cluster-level faults (kill, outage, bad release) go through the
Kubernetes API, exactly as a careless human or a broken pipeline would cause them.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass

import httpx

from . import metrics
from .engine import Healer

POD_FAULTS = {"crash", "hang", "errors", "latency", "leak"}


@dataclass(frozen=True)
class Experiment:
    kind: str
    label: str
    description: str
    expected: str


EXPERIMENTS: list[Experiment] = [
    Experiment("crash", "Crash process", "Kill the app process inside one pod.", "kubelet restarts container; healer verifies"),
    Experiment("kill_pod", "Delete pod", "Delete a pod through the Kubernetes API.", "ReplicaSet replaces it; healer verifies"),
    Experiment("hang", "Hang (liveness)", "Make /healthz fail on one pod.", "liveness probe restarts it"),
    Experiment("errors", "Error storm", "70% of requests on one pod return 500. Probes stay green.", "Prometheus alert, healer restarts pod"),
    Experiment("latency", "Latency spike", "Add 900ms to every request on one pod.", "Prometheus alert, healer restarts pod"),
    Experiment("leak", "Memory leak", "One pod leaks memory towards its limit.", "Prometheus alert, healer recycles pod before OOM"),
    Experiment("outage", "Scale to zero", "Scale the deployment to 0 replicas.", "healer restores declared minimum"),
    Experiment("bad_deploy", "Bad release", "Roll out a release that crashes on start.", "crash loop detected, healer rolls back"),
]
EXPERIMENT_KINDS = {e.kind for e in EXPERIMENTS}


class ChaosError(Exception):
    pass


async def inject(healer: Healer, service: str, kind: str, pod: str | None = None) -> dict[str, object]:
    if kind not in EXPERIMENT_KINDS:
        raise ChaosError(f"unknown experiment '{kind}'")
    snap = healer.snapshot
    if snap is None or service not in snap.deployments:
        raise ChaosError(f"unknown service '{service}'")
    dep = snap.deployments[service]
    live = [p for p in dep.pods if p.ready and not p.terminating]

    if kind in POD_FAULTS or kind == "kill_pod":
        candidates = [p for p in live if pod is None or p.name == pod]
        if not candidates:
            raise ChaosError(f"no ready pod available in {service}")
        target = random.choice(candidates)
        if kind == "kill_pod":
            await asyncio.to_thread(healer.gateway.delete_pod, target.name)
        else:
            if not target.ip:
                raise ChaosError(f"{target.name} has no IP yet")
            async with httpx.AsyncClient(timeout=4.0) as client:
                response = await client.post(f"http://{target.ip}:8000/chaos/{kind}")
                response.raise_for_status()
        detail = f"{kind} -> pod {target.name}"
    elif kind == "outage":
        await asyncio.to_thread(healer.gateway.scale, service, 0)
        detail = f"scaled {service} to 0 replicas"
    else:  # bad_deploy
        await asyncio.to_thread(
            healer.gateway.release, service, {"CRASH_ON_START": "true"}, "2.0.0-broken"
        )
        detail = f"rolled out broken release 2.0.0-broken to {service}"

    healer.record_chaos(service, kind)
    metrics.CHAOS.labels(service, kind).inc()
    healer.store.log(healer.clock(), "chaos", "chaos", detail)
    return {"service": service, "kind": kind, "detail": detail}
