"""HTTP surface of the healer: Alertmanager webhook, control-center API and UI."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import BaseModel

from .chaos import EXPERIMENTS, ChaosError, inject
from .config import Settings
from .engine import Healer, is_healthy, ready_count
from .incidents import IncidentStore
from .kube import ClusterGateway
from .notifier import webhook_notifier
from .policy import ALERT_PLAYBOOKS, K8S_PLAYBOOKS

log = logging.getLogger("healer")
STATIC_DIR = Path(__file__).parent / "static"


class ChaosRequest(BaseModel):
    service: str
    kind: str
    pod: str | None = None


def serialize_state(healer: Healer) -> dict[str, Any]:
    snap = healer.snapshot
    now = healer.clock()
    open_by_target: dict[str, str] = {}
    for incident in healer.store.open_incidents():
        open_by_target[incident.pod or incident.deployment] = incident.title
        open_by_target.setdefault(incident.deployment, incident.title)
    availability = healer.availability()

    services = []
    for dep in (snap.deployments.values() if snap else []):
        ready = ready_count(dep)
        if dep.desired == 0 or ready == 0:
            status = "down"
        elif dep.name in open_by_target:
            status = "healing"
        elif is_healthy(dep):
            status = "healthy"
        else:
            status = "degraded"
        services.append({
            "name": dep.name, "desired": dep.desired, "ready": ready,
            "min_replicas": dep.min_replicas, "version": dep.version, "image": dep.image,
            "revision": dep.revision, "status": status, "availability": availability.get(dep.name),
            "pods": [
                {
                    "name": p.name, "ready": p.ready, "phase": p.phase,
                    "restarts": p.restart_count, "waiting_reason": p.waiting_reason,
                    "terminating": p.terminating, "age_seconds": max(0.0, now - p.created_at),
                    "healing": p.name in open_by_target, "ip": p.ip,
                    "current": p.template_hash == dep.current_hash,
                }
                for p in dep.pods
            ],
        })
    values = [v for v in availability.values() if v is not None]
    stats = healer.store.stats()
    stats["availability"] = sum(values) / len(values) if values else None
    return {
        "now": now,
        "healer": {
            "started_at": healer.started_at, "dry_run": healer.settings.dry_run,
            "namespace": healer.settings.namespace, "connected": snap is not None,
            "last_sync": snap.taken_at if snap else None, "budget": healer.budget_view(),
            "budget_max": healer.settings.budget_max_actions,
        },
        "services": services,
        "incidents": [i.to_dict() for i in reversed(healer.store.all())][:60],
        "events": [asdict(e) for e in reversed(healer.store.events)][:120],
        "stats": stats,
        "chaos": {"enabled": healer.settings.chaos_enabled, "experiments": [asdict(e) for e in EXPERIMENTS]},
        "links": {
            "grafana": healer.settings.grafana_url, "alertmanager": healer.settings.alertmanager_url,
            "jenkins": healer.settings.jenkins_url,
            "prometheus": os.getenv("PROMETHEUS_PUBLIC_URL", "http://localhost:30090"),
        },
    }


def create_app(gateway: ClusterGateway | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        nonlocal gateway
        if gateway is None:
            from .kube import KubeGateway

            gateway = KubeGateway(settings.namespace, settings.managed_label)
        healer = Healer(gateway, settings, IncidentStore(), webhook_notifier(settings.notify_webhook_url))
        healer.store.log(time.time(), "info", "healer", f"healer online - watching namespace {settings.namespace}")
        app.state.healer = healer
        app.state.http = httpx.AsyncClient(timeout=5.0)

        async def loop() -> None:
            failures = 0
            while True:
                try:
                    await healer.reconcile_once()
                    if failures:
                        healer.store.log(time.time(), "info", "healer", "Kubernetes API reachable again")
                    failures = 0
                except Exception as exc:
                    failures += 1
                    log.exception("reconcile failed")
                    if failures in (1, 10) or failures % 60 == 0:
                        healer.store.log(time.time(), "error", "healer", f"reconcile failed: {exc}")
                await asyncio.sleep(min(settings.reconcile_interval * (2 ** min(failures, 4)), 30))

        task = asyncio.create_task(loop())
        try:
            yield
        finally:
            task.cancel()
            await app.state.http.aclose()

    app = FastAPI(title="Self-Healing Controller", version="1.0.0", lifespan=lifespan)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/metrics")
    async def metrics_endpoint() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/api/state")
    async def state(request: Request) -> dict[str, Any]:
        return serialize_state(request.app.state.healer)

    @app.get("/api/playbooks")
    async def playbooks() -> dict[str, Any]:
        return {
            "prometheus": {k: asdict(v) for k, v in ALERT_PLAYBOOKS.items()},
            "kubernetes": {k: asdict(v) for k, v in K8S_PLAYBOOKS.items()},
        }

    @app.post("/api/alerts")
    async def alerts(request: Request) -> dict[str, int]:
        payload = await request.json()
        acted = await request.app.state.healer.handle_alerts(payload)
        return {"received": len(payload.get("alerts", [])), "acted": acted}

    @app.post("/api/chaos")
    async def chaos(body: ChaosRequest, request: Request, x_chaos_token: str = Header(default="")) -> dict[str, object]:
        if not settings.chaos_enabled:
            raise HTTPException(403, "chaos experiments are disabled")
        if settings.chaos_token and x_chaos_token != settings.chaos_token:
            raise HTTPException(401, "invalid chaos token")
        try:
            return await inject(request.app.state.healer, body.service, body.kind, body.pod)
        except ChaosError as exc:
            raise HTTPException(400, str(exc)) from exc
        except httpx.HTTPError as exc:
            raise HTTPException(502, f"could not reach pod: {exc}") from exc

    @app.get("/api/series")
    async def series(request: Request) -> JSONResponse:
        """Proxy a few range queries to Prometheus for the dashboard sparklines."""
        queries = {
            "rps": 'sum by (service) (rate(http_requests_total{job="demo-services"}[30s]))',
            "errors": 'sum by (service) (rate(http_requests_total{job="demo-services",status=~"5.."}[30s]))'
                      ' / sum by (service) (rate(http_requests_total{job="demo-services"}[30s]))',
            "p95": 'histogram_quantile(0.95, sum by (service, le) (rate(http_request_duration_seconds_bucket{job="demo-services"}[30s])))',
        }
        end = time.time()
        out: dict[str, dict[str, list[list[float]]]] = {}
        client: httpx.AsyncClient = request.app.state.http
        try:
            for name, query in queries.items():
                response = await client.get(
                    f"{settings.prometheus_url}/api/v1/query_range",
                    params={"query": query, "start": end - 600, "end": end, "step": 10},
                )
                response.raise_for_status()
                out[name] = {
                    r["metric"].get("service", "?"): [[float(t), float(v)] for t, v in r["values"] if v != "NaN"]
                    for r in response.json()["data"]["result"]
                }
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            return JSONResponse({"available": False, "error": str(exc)})
        try:
            alerts_resp = await client.get(f"{settings.prometheus_url}/api/v1/alerts")
            firing = [
                {"name": a["labels"].get("alertname"), "state": a["state"],
                 "target": a["labels"].get("pod") or a["labels"].get("deployment") or a["labels"].get("service")}
                for a in alerts_resp.json()["data"]["alerts"]
            ]
        except (httpx.HTTPError, KeyError, ValueError):
            firing = []
        return JSONResponse({"available": True, "series": out, "alerts": firing})

    return app


def run() -> None:
    import uvicorn

    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    run()
