"""Backends that replay a recorded scenario file.

Used for offline runs, tests and Chapter 6 eval replays: same scenario, same
tool results, every time.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from investigator.tools.backends import Backends
from investigator.tools.base import parse_ts


def _in(ts: str, start: datetime, end: datetime) -> bool:
    return start <= parse_ts(ts) <= end


class FixtureMetrics:
    def __init__(self, data: dict) -> None:
        self.data = data.get("metrics", {})

    def series(self, service, metric, start, end):
        points = self.data.get(service, {}).get(metric, [])
        return [(t, v) for t, v in points if _in(t, start, end)]

    def query(self, expr, start, end, step_s):
        raise RuntimeError("raw PromQL isn't available in recorded scenarios; use get_service_red or compare_windows")


class FixtureTraces:
    def __init__(self, data: dict) -> None:
        self.traces = data.get("traces", [])

    def search(self, service, operation, start, end, errors_only, min_duration_ms, limit):
        out = []
        for t in self.traces:
            spans = t["spans"]
            if not any(sp["service"] == service and (operation is None or sp["operation"] == operation) for sp in spans):
                continue
            root = next(sp for sp in spans if sp["parent_id"] is None)
            if not _in(root["start"], start, end):
                continue
            error = any(sp["error"] for sp in spans)
            if errors_only and not error:
                continue
            if min_duration_ms is not None and root["duration_ms"] < min_duration_ms:
                continue
            out.append({
                "trace_id": t["trace_id"],
                "root_service": root["service"],
                "root_operation": root["operation"],
                "start": root["start"],
                "duration_ms": root["duration_ms"],
                "error": error,
            })
        return out[:limit]

    def get(self, trace_id):
        return next((t["spans"] for t in self.traces if t["trace_id"] == trace_id), [])


class FixtureLogs:
    def __init__(self, data: dict) -> None:
        self.lines = data.get("logs", [])

    def search(self, service, start, end, pattern, level, limit):
        rx = re.compile(pattern, re.I) if pattern else None
        out = [
            line for line in self.lines
            if line["service"] == service
            and _in(line["t"], start, end)
            and (level is None or line["level"] == level)
            and (rx is None or rx.search(line["message"]))
        ]
        return out[:limit]


class FixtureChanges:
    """Helm releases, plus Deployment rollouts made outside Helm (kubectl edit,
    kubectl set image): the scenario's `rollouts` list, as ReplicaSet history."""

    def __init__(self, data: dict) -> None:
        self.releases = data.get("changes", [])
        self.rollouts = data.get("rollouts", [])

    def list(self, namespace, start, end):
        out = [
            {k: v for k, v in r.items() if k not in ("values", "manifest")}
            for r in self.releases
            if r.get("namespace", namespace) == namespace and _in(r["updated"], start, end)
        ]
        out += [
            {"release": f"deployment/{r['deployment']}", "revision": r["revision"], "updated": r["updated"],
             "status": "rollout", "chart": None, "description": r.get("description", "Deployment rollout outside Helm"),
             "source": "rollout"}
            for r in self.rollouts if _in(r["updated"], start, end)
        ]
        return out

    def values(self, namespace, release, revision):
        if release.startswith("deployment/"):
            name = release.split("/", 1)[1]
            for r in self.rollouts + self.data_rollout_history():
                if r["deployment"] == name and r["revision"] == revision:
                    return r["template"]
            raise KeyError(f"no revision {revision} of {release}")
        for r in self.releases:
            if r["release"] == release and r["revision"] == revision:
                return r.get("values", {})
        raise KeyError(f"no revision {revision} of release {release}")

    def data_rollout_history(self):
        return [p for r in self.rollouts for p in r.get("previous", [])]


class FixtureK8s:
    def __init__(self, data: dict) -> None:
        self.data = data

    def events(self, namespace, start, end, reasons: Optional[list[str]]):
        return [
            e for e in self.data.get("events", [])
            if _in(e["t"], start, end) and (not reasons or e["reason"] in reasons)
        ]

    def workload(self, namespace, name):
        w = self.data.get("workloads", {}).get(name)
        if w is None:
            raise KeyError(f"no Deployment named {name} in {namespace}")
        return w

    def nodes(self):
        return self.data.get("nodes", [])


class FixtureTopology:
    def __init__(self, data: dict) -> None:
        self._edges = [tuple(e) for e in data.get("topology", [])]

    def edges(self):
        return self._edges


def load_scenario(path: str | Path) -> dict:
    return json.loads(Path(path).read_text())


def fixture_backends(scenario: dict) -> Backends:
    return Backends(
        metrics=FixtureMetrics(scenario),
        traces=FixtureTraces(scenario),
        logs=FixtureLogs(scenario),
        changes=FixtureChanges(scenario),
        k8s=FixtureK8s(scenario),
        topology=FixtureTopology(scenario),
    )
