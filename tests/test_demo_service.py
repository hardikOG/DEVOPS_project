import os

import pytest
from fastapi.testclient import TestClient

os.environ["SERVICE_NAME"] = "orders"  # read once at import time, like in the container
import app as demo  # noqa: E402


@pytest.fixture
def client():
    with TestClient(demo.create_app()) as c:
        yield c


def test_work_and_metrics(client):
    assert client.get("/api/work").json()["ok"] is True
    text = client.get("/metrics").text
    assert 'http_requests_total{route="/api/work",service="orders",status="200"}' in text


def test_error_injection_is_sticky_until_reset(client):
    client.post("/chaos/errors?rate=1")
    assert client.get("/api/work").status_code == 500
    assert client.get("/healthz").status_code == 200  # probes stay green - that's the point
    client.post("/chaos/reset")
    assert client.get("/api/work").status_code == 200


def test_hang_fails_liveness(client):
    client.post("/chaos/hang")
    assert client.get("/healthz").status_code == 503
    assert client.get("/readyz").status_code == 200


def test_latency_and_state(client):
    client.post("/chaos/latency?ms=50")
    assert client.get("/chaos").json()["latency_ms"] == 50


def test_error_rate_is_clamped(client):
    assert client.post("/chaos/errors?rate=5").json()["rate"] == 1.0
