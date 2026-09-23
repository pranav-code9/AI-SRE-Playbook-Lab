"""Backend interfaces. Tools depend on these, never on a specific system,
so the same tools run against the live lab or a recorded scenario."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Protocol

METRICS = ("request_rate", "error_rate", "latency_p50", "latency_p95", "latency_p99")


class MetricsBackend(Protocol):
    def series(self, service: str, metric: str, start: datetime, end: datetime) -> list[tuple[str, float]]:
        """One of METRICS for a service: [(iso timestamp, value)]. Latency in ms,
        error_rate as a fraction, request_rate per second."""

    def query(self, expr: str, start: datetime, end: datetime, step_s: int) -> list[dict]:
        """Raw query: [{"labels": {...}, "points": [(iso, value)]}]."""


class TracesBackend(Protocol):
    def search(
        self,
        service: str,
        operation: Optional[str],
        start: datetime,
        end: datetime,
        errors_only: bool,
        min_duration_ms: Optional[float],
        limit: int,
    ) -> list[dict]:
        """[{"trace_id", "root_service", "root_operation", "start", "duration_ms", "error"}]"""

    def get(self, trace_id: str) -> list[dict]:
        """Spans: [{"span_id", "parent_id", "service", "operation", "start", "duration_ms", "error"}]"""


class LogsBackend(Protocol):
    def search(
        self, service: str, start: datetime, end: datetime, pattern: Optional[str], level: Optional[str], limit: int
    ) -> list[dict]:
        """[{"t", "service", "level", "message"}]"""


class ChangesBackend(Protocol):
    def list(self, namespace: str, start: datetime, end: datetime) -> list[dict]:
        """[{"release", "revision", "updated", "status", "chart", "description"}]"""

    def values(self, namespace: str, release: str, revision: int) -> dict:
        """User-supplied values for a release revision."""


class K8sBackend(Protocol):
    def events(self, namespace: str, start: datetime, end: datetime, reasons: Optional[list[str]]) -> list[dict]:
        """[{"t", "reason", "object", "message", "count"}]"""

    def workload(self, namespace: str, name: str) -> dict:
        """{"name", "desired", "ready", "restarts", "hpa": {...} | None}"""

    def nodes(self) -> list[dict]:
        """[{"name", "conditions": {"MemoryPressure": bool, "DiskPressure": bool, "PIDPressure": bool}}]"""


class TopologyBackend(Protocol):
    def edges(self) -> list[tuple[str, str]]:
        """Directed call edges: (caller, callee)."""


@dataclass
class Backends:
    metrics: MetricsBackend
    traces: TracesBackend
    logs: LogsBackend
    changes: ChangesBackend
    k8s: K8sBackend
    topology: TopologyBackend
