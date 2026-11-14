"""Incident lifecycle and event log, held in memory.

An incident goes: detected -> remediating -> recovered | failed | escalated
(or `superseded` when a broader incident absorbs it). MTTR is measured from
detection to the moment the cluster is verified healthy again, not to the
moment an action was sent - a restart that never comes back is not a recovery.
"""

from __future__ import annotations

import itertools
import statistics
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

OPEN_STATES = {"detected", "remediating"}
TERMINAL_STATES = {"recovered", "failed", "escalated", "superseded"}


@dataclass
class TimelineEntry:
    ts: float
    stage: str
    message: str


@dataclass
class Incident:
    id: int
    key: str
    kind: str
    title: str
    severity: str
    source: str
    deployment: str
    pod: str | None
    action: str
    detected_at: float
    status: str = "detected"
    recovery_check: str = "deployment_available"
    resolved_at: float | None = None
    chaos_kind: str | None = None
    chaos_at: float | None = None
    timeline: list[TimelineEntry] = field(default_factory=list)

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATES

    @property
    def recovery_seconds(self) -> float | None:
        if self.resolved_at is None or self.status != "recovered":
            return None
        return self.resolved_at - self.detected_at

    @property
    def detection_seconds(self) -> float | None:
        if self.chaos_at is None:
            return None
        return max(0.0, self.detected_at - self.chaos_at)

    def note(self, ts: float, stage: str, message: str) -> None:
        self.timeline.append(TimelineEntry(ts, stage, message))

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["recovery_seconds"] = self.recovery_seconds
        data["detection_seconds"] = self.detection_seconds
        return data


@dataclass
class LogEvent:
    ts: float
    level: str
    source: str
    message: str


class IncidentStore:
    def __init__(self, max_incidents: int = 200, max_events: int = 400) -> None:
        self._ids = itertools.count(1)
        self._incidents: deque[Incident] = deque(maxlen=max_incidents)
        self.events: deque[LogEvent] = deque(maxlen=max_events)
        self.actions_taken = 0

    def log(self, ts: float, level: str, source: str, message: str) -> None:
        self.events.append(LogEvent(ts, level, source, message))

    def open(self, **fields: Any) -> Incident:
        incident = Incident(id=next(self._ids), **fields)
        self._incidents.append(incident)
        return incident

    def find_open(self, key: str) -> Incident | None:
        return next((i for i in reversed(self._incidents) if i.key == key and i.is_open), None)

    def find_latest(self, key: str) -> Incident | None:
        return next((i for i in reversed(self._incidents) if i.key == key), None)

    def open_incidents(self) -> list[Incident]:
        return [i for i in self._incidents if i.is_open]

    def all(self) -> list[Incident]:
        return list(self._incidents)

    def stats(self) -> dict[str, Any]:
        counted = [i for i in self._incidents if i.status != "superseded"]
        recovered = [i for i in counted if i.status == "recovered"]
        closed = [i for i in counted if i.status in TERMINAL_STATES]
        durations = [i.recovery_seconds for i in recovered if i.recovery_seconds is not None]
        detections = [i.detection_seconds for i in counted if i.detection_seconds is not None]
        return {
            "total": len(counted),
            "open": sum(1 for i in counted if i.is_open),
            "recovered": len(recovered),
            "failed": sum(1 for i in counted if i.status in ("failed", "escalated")),
            "auto_recovery_rate": (len(recovered) / len(closed)) if closed else None,
            "mttr_seconds": statistics.fmean(durations) if durations else None,
            "mttr_p95_seconds": _percentile(durations, 0.95),
            "mttd_seconds": statistics.fmean(detections) if detections else None,
            "actions_taken": self.actions_taken,
        }


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]
