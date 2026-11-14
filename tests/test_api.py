import pytest
from conftest import make_settings
from fastapi.testclient import TestClient

from healer.main import create_app


@pytest.fixture
def client(cluster):
    cluster.add_deployment("orders")
    cluster.add_deployment("payments")
    app = create_app(gateway=cluster, settings=make_settings())
    with TestClient(app) as c:
        app.state.healer.clock = cluster.clock
        import asyncio
        asyncio.run(app.state.healer.reconcile_once())
        yield c


def test_index_serves_dashboard(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "Aegis" in response.text


def test_state_shape(client):
    body = client.get("/api/state").json()
    assert {s["name"] for s in body["services"]} == {"orders", "payments"}
    orders = next(s for s in body["services"] if s["name"] == "orders")
    assert orders["status"] == "healthy" and orders["ready"] == 2 and len(orders["pods"]) == 2
    assert body["chaos"]["enabled"] is True
    assert len(body["chaos"]["experiments"]) == 8
    assert body["healer"]["connected"] is True


def test_alert_webhook_triggers_remediation(client, cluster):
    pod = cluster.deployments["orders"].pods[0].name
    payload = {"alerts": [{"status": "firing", "labels": {"alertname": "HighErrorRate", "service": "orders", "pod": pod}}]}
    assert client.post("/api/alerts", json=payload).json() == {"received": 1, "acted": 1}
    assert ("delete_pod", pod) in cluster.calls
    state = client.get("/api/state").json()
    assert state["incidents"][0]["kind"] == "HighErrorRate"
    orders = next(s for s in state["services"] if s["name"] == "orders")
    assert orders["status"] == "healing"


def test_chaos_cluster_level_experiments(client, cluster):
    assert client.post("/api/chaos", json={"service": "orders", "kind": "outage"}).status_code == 200
    assert ("scale", "orders", "0") in cluster.calls
    assert client.post("/api/chaos", json={"service": "payments", "kind": "bad_deploy"}).status_code == 200
    assert ("release", "payments", "2.0.0-broken") in cluster.calls
    assert client.post("/api/chaos", json={"service": "payments", "kind": "kill_pod"}).status_code == 200
    assert any(c[0] == "delete_pod" for c in cluster.calls)


def test_chaos_rejects_bad_input(client):
    assert client.post("/api/chaos", json={"service": "nope", "kind": "crash"}).status_code == 400
    assert client.post("/api/chaos", json={"service": "orders", "kind": "meteor"}).status_code == 400


def test_chaos_can_be_disabled_and_token_protected(cluster):
    cluster.add_deployment("orders")
    with TestClient(create_app(gateway=cluster, settings=make_settings(chaos_enabled=False))) as c:
        assert c.post("/api/chaos", json={"service": "orders", "kind": "outage"}).status_code == 403
    with TestClient(create_app(gateway=cluster, settings=make_settings(chaos_token="s3cret"))) as c:
        assert c.post("/api/chaos", json={"service": "orders", "kind": "outage"}).status_code == 401


def test_series_degrades_gracefully_without_prometheus(client):
    assert client.get("/api/series").json()["available"] is False


def test_metrics_and_health(client):
    assert client.get("/healthz").json() == {"status": "ok"}
    assert "healer_reconcile_seconds" in client.get("/metrics").text
