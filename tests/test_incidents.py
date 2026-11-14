from healer.incidents import IncidentStore


def new(store, key="k", detected_at=100.0, chaos_at=None):
    return store.open(
        key=key, kind="PodLost", title="t", severity="warning", source="kubernetes",
        deployment="orders", pod="orders-1", action="observe", detected_at=detected_at, chaos_at=chaos_at,
    )


def test_stats_empty():
    stats = IncidentStore().stats()
    assert stats["total"] == 0
    assert stats["mttr_seconds"] is None
    assert stats["auto_recovery_rate"] is None


def test_mttr_mttd_and_recovery_rate():
    store = IncidentStore()
    a = new(store, "a", detected_at=100, chaos_at=95)
    a.status, a.resolved_at = "recovered", 110
    b = new(store, "b", detected_at=200, chaos_at=190)
    b.status, b.resolved_at = "recovered", 230
    c = new(store, "c", detected_at=300)
    c.status, c.resolved_at = "failed", 400
    new(store, "d")  # still open
    s = new(store, "e")
    s.status = "superseded"

    stats = store.stats()
    assert stats["total"] == 4  # superseded excluded
    assert stats["open"] == 1
    assert stats["recovered"] == 2
    assert stats["failed"] == 1
    assert stats["mttr_seconds"] == 20
    assert stats["mttr_p95_seconds"] == 30
    assert stats["mttd_seconds"] == 7.5
    assert abs(stats["auto_recovery_rate"] - 2 / 3) < 1e-9


def test_find_open_and_latest():
    store = IncidentStore()
    first = new(store, "x")
    first.status = "recovered"
    second = new(store, "x")
    assert store.find_open("x") is second
    assert store.find_latest("x") is second
    second.status = "failed"
    assert store.find_open("x") is None
    assert store.find_latest("x") is second


def test_store_is_bounded():
    store = IncidentStore(max_incidents=3, max_events=2)
    for i in range(5):
        new(store, f"k{i}")
        store.log(i, "info", "test", f"event {i}")
    assert len(store.all()) == 3
    assert [e.message for e in store.events] == ["event 3", "event 4"]


def test_to_dict_contains_derived_fields():
    store = IncidentStore()
    incident = new(store, chaos_at=90)
    incident.note(100, "detected", "boom")
    incident.status, incident.resolved_at = "recovered", 112
    data = incident.to_dict()
    assert data["recovery_seconds"] == 12
    assert data["detection_seconds"] == 10
    assert data["timeline"][0]["message"] == "boom"
