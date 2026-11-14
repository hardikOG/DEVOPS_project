"""Demo microservice used as the workload the self-healing system protects.

The same image runs as three services (orders, payments, inventory) and as a
traffic generator (MODE=loadgen). Each service exposes Prometheus metrics and a
set of chaos endpoints so failures can be injected on demand:

    POST /chaos/crash     process exits immediately          -> kubelet restarts the container
    POST /chaos/hang      liveness probe starts failing      -> kubelet restarts the container
    POST /chaos/errors    a share of requests return HTTP 500 -> Prometheus alert -> healer
    POST /chaos/latency   every request is slowed down        -> Prometheus alert -> healer
    POST /chaos/leak      memory grows until restarted        -> Prometheus alert -> healer
    POST /chaos/reset     clear injected faults

Faults that only a restart can clear (errors, latency, leak) are deliberately
sticky: Kubernetes probes still pass, so only the metrics-driven healer notices.
"""

from __future__ import annotations

import asyncio
import os
import random
import threading
import time
from dataclasses import dataclass, field

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

SERVICE_NAME = os.getenv("SERVICE_NAME", "demo")
POD_NAME = os.getenv("POD_NAME", "local")
VERSION = os.getenv("APP_VERSION", "1.0.0")
LEAK_STEP_MB = int(os.getenv("LEAK_STEP_MB", "8"))
LEAK_MAX_MB = int(os.getenv("LEAK_MAX_MB", "400"))

REQUESTS = Counter(
    "http_requests_total", "HTTP requests served", ["service", "route", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["service", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)
FAULT = Gauge("demo_fault_active", "Injected fault currently active", ["service", "fault"])
INFO = Gauge("demo_service_info", "Static service info", ["service", "version"])


@dataclass
class FaultState:
    """Mutable fault-injection state for this process (one per app instance)."""

    error_rate: float = 0.0
    latency_ms: int = 0
    hung: bool = False
    leaking: bool = False
    leak: list[bytearray] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)

    def publish(self) -> None:
        FAULT.labels(SERVICE_NAME, "errors").set(1 if self.error_rate > 0 else 0)
        FAULT.labels(SERVICE_NAME, "latency").set(1 if self.latency_ms > 0 else 0)
        FAULT.labels(SERVICE_NAME, "hang").set(1 if self.hung else 0)
        FAULT.labels(SERVICE_NAME, "leak").set(1 if self.leaking else 0)


def create_app() -> FastAPI:
    """Build the FastAPI application with its own fault state."""
    if os.getenv("CRASH_ON_START", "").lower() in ("1", "true", "yes"):
        # Simulates a bad release: the container dies during startup every time.
        print(f"[{SERVICE_NAME}] CRASH_ON_START set - refusing to start (bad release)", flush=True)
        os._exit(3)

    app = FastAPI(title=f"{SERVICE_NAME}-service", version=VERSION)
    state = FaultState()
    app.state.faults = state
    state.publish()
    INFO.labels(SERVICE_NAME, VERSION).set(1)

    def leak_worker() -> None:
        while state.leaking and len(state.leak) * LEAK_STEP_MB < LEAK_MAX_MB:
            chunk = bytearray(LEAK_STEP_MB * 1024 * 1024)
            # Touch every page so the memory is really resident, not just reserved.
            for offset in range(0, len(chunk), 4096):
                chunk[offset] = 1
            state.leak.append(chunk)
            time.sleep(1)

    @app.middleware("http")
    async def instrument(request: Request, call_next):  # type: ignore[no-untyped-def]
        route = request.url.path
        if not route.startswith("/api/"):
            return await call_next(request)
        start = time.perf_counter()
        response = await call_next(request)
        LATENCY.labels(SERVICE_NAME, route).observe(time.perf_counter() - start)
        REQUESTS.labels(SERVICE_NAME, route, str(response.status_code)).inc()
        return response

    @app.get("/")
    async def root() -> dict[str, object]:
        return {"service": SERVICE_NAME, "pod": POD_NAME, "version": VERSION}

    @app.get("/api/work")
    async def work() -> Response:
        await asyncio.sleep(random.uniform(0.005, 0.04) + state.latency_ms / 1000)
        if state.error_rate and random.random() < state.error_rate:
            return JSONResponse({"error": "internal failure", "pod": POD_NAME}, status_code=500)
        return JSONResponse({"ok": True, "service": SERVICE_NAME, "pod": POD_NAME})

    @app.get("/healthz")
    async def healthz() -> Response:
        if state.hung:
            return JSONResponse({"status": "hung"}, status_code=503)
        return JSONResponse({"status": "ok"})

    @app.get("/readyz")
    async def readyz() -> Response:
        return JSONResponse({"status": "ready"})

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get("/chaos")
    async def chaos_state() -> dict[str, object]:
        return {
            "error_rate": state.error_rate,
            "latency_ms": state.latency_ms,
            "hung": state.hung,
            "leaking": state.leaking,
            "leaked_mb": len(state.leak) * LEAK_STEP_MB,
        }

    @app.post("/chaos/crash")
    async def chaos_crash() -> dict[str, str]:
        loop = asyncio.get_running_loop()
        loop.call_later(0.2, os._exit, 1)
        return {"injected": "crash", "pod": POD_NAME}

    @app.post("/chaos/hang")
    async def chaos_hang() -> dict[str, str]:
        state.hung = True
        state.publish()
        return {"injected": "hang", "pod": POD_NAME}

    @app.post("/chaos/errors")
    async def chaos_errors(rate: float = 0.7) -> dict[str, object]:
        state.error_rate = max(0.0, min(rate, 1.0))
        state.publish()
        return {"injected": "errors", "rate": state.error_rate, "pod": POD_NAME}

    @app.post("/chaos/latency")
    async def chaos_latency(ms: int = 900) -> dict[str, object]:
        state.latency_ms = max(0, ms)
        state.publish()
        return {"injected": "latency", "ms": state.latency_ms, "pod": POD_NAME}

    @app.post("/chaos/leak")
    async def chaos_leak() -> dict[str, str]:
        if not state.leaking:
            state.leaking = True
            threading.Thread(target=leak_worker, daemon=True).start()
        state.publish()
        return {"injected": "leak", "pod": POD_NAME}

    @app.post("/chaos/reset")
    async def chaos_reset() -> dict[str, str]:
        state.error_rate, state.latency_ms, state.hung, state.leaking = 0.0, 0, False, False
        state.leak.clear()
        state.publish()
        return {"reset": "ok", "pod": POD_NAME}

    return app


async def run_loadgen() -> None:
    """Send a steady stream of requests to every target so metrics are always flowing."""
    import httpx

    targets = [t.strip() for t in os.getenv("LOADGEN_TARGETS", "").split(",") if t.strip()]
    rps = float(os.getenv("LOADGEN_RPS", "10"))
    print(f"[loadgen] {rps} rps per target -> {targets}", flush=True)
    async with httpx.AsyncClient(timeout=3.0) as client:

        async def hit(url: str) -> None:
            try:
                await client.get(f"{url}/api/work")
            except httpx.HTTPError:
                pass  # The target being down is exactly what we are here to observe.

        in_flight: set[asyncio.Task[None]] = set()
        while True:
            # Fire-and-forget so one slow target never throttles traffic to the others.
            for url in targets:
                task = asyncio.create_task(hit(url))
                in_flight.add(task)
                task.add_done_callback(in_flight.discard)
            await asyncio.sleep(1 / rps)


if os.getenv("MODE", "service") == "service":
    app = create_app()

if __name__ == "__main__":
    if os.getenv("MODE") == "loadgen":
        asyncio.run(run_loadgen())
    else:
        import uvicorn

        uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="warning")
