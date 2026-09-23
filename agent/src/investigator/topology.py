"""Service topology graph, built from traces and stored in Neo4j (Chapter 2).

Traces already record who calls whom: when a span's parent belongs to a
different service, that's a call edge. The builder samples recent traces,
extracts edges and workload mappings, and merges them into Neo4j with a
last-seen time, so the graph follows the system as it changes.

    build-topology --jaeger-url http://localhost:8080/jaeger/ui \\
                   --neo4j-uri bolt://localhost:7687 --neo4j-password <password> \\
                   --every 300

Requires the neo4j driver: pip install -e ".[neo4j]"
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

log = logging.getLogger(__name__)

DEFAULT_FRESHNESS_HOURS = 24


@dataclass
class Edge:
    caller: str
    callee: str
    calls: int = 0
    errors: int = 0
    operations: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class Workload:
    service: str
    namespace: str
    name: str


def extract_edges(traces: Iterable[list[dict]]) -> list[Edge]:
    """Call edges across service boundaries, aggregated over many traces.

    A span whose parent belongs to another service is one call from the
    parent's service to the span's service. Calls within a service are ignored.
    """
    edges: dict[tuple[str, str], Edge] = {}

    def add(caller, callee, span):
        edge = edges.setdefault((caller, callee), Edge(caller, callee))
        edge.calls += 1
        edge.errors += 1 if span.get("error") else 0
        edge.operations.add(span["operation"])

    for spans in traces:
        by_id = {s["span_id"]: s for s in spans}
        answered = {s.get("parent_id") for s in spans}
        for s in spans:
            parent = by_id.get(s.get("parent_id"))
            if parent is not None and parent["service"] != s["service"]:
                add(parent["service"], s["service"], s)
            elif s.get("kind") == "client" and s["span_id"] not in answered and s.get("peer"):
                # A call nobody answered (callee down or untraced) still shows the dependency.
                add(s["service"], s["peer"], s)
    return sorted(edges.values(), key=lambda e: (e.caller, e.callee))


def extract_workloads(traces: Iterable[list[dict]]) -> list[Workload]:
    """Service-to-Deployment mappings from OpenTelemetry resource attributes.

    Needs the collector's k8sattributes processor (or equivalent) to set
    k8s.namespace.name and k8s.deployment.name. Spans without them are skipped.
    """
    found: set[Workload] = set()
    for spans in traces:
        for s in spans:
            r = s.get("resource") or {}
            ns, name = r.get("k8s.namespace.name"), r.get("k8s.deployment.name")
            if ns and name:
                found.add(Workload(s["service"], ns, name))
    return sorted(found, key=lambda w: (w.service, w.namespace, w.name))


SCHEMA = [
    "CREATE CONSTRAINT service_name IF NOT EXISTS FOR (s:Service) REQUIRE s.name IS UNIQUE",
    "CREATE CONSTRAINT workload_key IF NOT EXISTS FOR (w:Workload) REQUIRE w.key IS UNIQUE",
]

MERGE_EDGES = """
UNWIND $edges AS e
MERGE (a:Service {name: e.caller})
MERGE (b:Service {name: e.callee})
MERGE (a)-[c:CALLS]->(b)
SET c.last_seen = datetime($seen_at),
    c.calls = coalesce(c.calls, 0) + e.calls,
    c.errors = coalesce(c.errors, 0) + e.errors,
    c.operations = [op IN coalesce(c.operations, []) WHERE NOT op IN e.operations] + e.operations
"""

MERGE_WORKLOADS = """
UNWIND $workloads AS w
MERGE (s:Service {name: w.service})
MERGE (d:Workload {key: w.namespace + '/' + w.name})
SET d.namespace = w.namespace, d.name = w.name
MERGE (s)-[r:RUNS_AS]->(d)
SET r.last_seen = datetime($seen_at)
"""

FRESH_EDGES = """
MATCH (a:Service)-[c:CALLS]->(b:Service)
WHERE c.last_seen >= datetime() - duration({hours: $hours})
RETURN a.name AS caller, b.name AS callee
ORDER BY caller, callee
"""


class TopologyWriter:
    def __init__(self, driver, database: Optional[str] = None) -> None:
        self.driver, self.database = driver, database

    def ensure_schema(self) -> None:
        with self.driver.session(database=self.database) as session:
            for stmt in SCHEMA:
                session.run(stmt)

    def write(self, edges: list[Edge], workloads: list[Workload], seen_at: datetime) -> None:
        params_edges = [
            {"caller": e.caller, "callee": e.callee, "calls": e.calls, "errors": e.errors,
             "operations": sorted(e.operations)}
            for e in edges
        ]
        params_workloads = [{"service": w.service, "namespace": w.namespace, "name": w.name} for w in workloads]
        stamp = seen_at.astimezone(timezone.utc).isoformat()

        def tx(t):
            if params_edges:
                t.run(MERGE_EDGES, edges=params_edges, seen_at=stamp)
            if params_workloads:
                t.run(MERGE_WORKLOADS, workloads=params_workloads, seen_at=stamp)

        with self.driver.session(database=self.database) as session:
            session.execute_write(tx)


class Neo4jTopology:
    """TopologyBackend for the agent: only edges seen within the freshness window."""

    def __init__(self, driver, database: Optional[str] = None, freshness_hours: int = DEFAULT_FRESHNESS_HOURS) -> None:
        self.driver, self.database, self.hours = driver, database, freshness_hours

    def edges(self):
        with self.driver.session(database=self.database) as session:
            return [(r["caller"], r["callee"]) for r in session.run(FRESH_EDGES, hours=self.hours)]


def sample_traces(traces_backend, lookback: timedelta, per_service: int, now: Optional[datetime] = None) -> list[list[dict]]:
    """Recent traces for every service Jaeger knows, deduplicated by trace id."""
    end = now or datetime.now(tz=timezone.utc)
    start = end - lookback
    ids: list[str] = []
    for service in traces_backend.services():
        for t in traces_backend.search(service, None, start, end, False, None, per_service):
            if t["trace_id"] not in ids:
                ids.append(t["trace_id"])
    return [traces_backend.get(tid) for tid in ids]


def build_once(traces_backend, writer: TopologyWriter, lookback: timedelta, per_service: int = 20) -> tuple[int, int]:
    now = datetime.now(tz=timezone.utc)
    traces = sample_traces(traces_backend, lookback, per_service, now)
    edges, workloads = extract_edges(traces), extract_workloads(traces)
    writer.write(edges, workloads, now)
    log.info("topology: %d traces, %d edges, %d workloads", len(traces), len(edges), len(workloads))
    return len(edges), len(workloads)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="build-topology", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--jaeger-url", default="http://localhost:8080/jaeger/ui")
    p.add_argument("--neo4j-uri", default="bolt://localhost:7687")
    p.add_argument("--neo4j-user", default="neo4j")
    p.add_argument("--neo4j-password", required=True)
    p.add_argument("--lookback-minutes", type=int, default=15)
    p.add_argument("--per-service", type=int, default=20, help="traces sampled per service each run")
    p.add_argument("--every", type=int, default=0, help="seconds between runs; 0 runs once")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    from neo4j import GraphDatabase

    from investigator.tools.live import JaegerTraces

    driver = GraphDatabase.driver(a.neo4j_uri, auth=(a.neo4j_user, a.neo4j_password))
    writer = TopologyWriter(driver)
    writer.ensure_schema()
    traces = JaegerTraces(a.jaeger_url)
    while True:
        try:
            build_once(traces, writer, timedelta(minutes=a.lookback_minutes), a.per_service)
        except Exception as exc:  # keep a scheduled builder alive through transient failures
            log.warning("topology build failed: %s", exc)
            if not a.every:
                return 1
        if not a.every:
            return 0
        time.sleep(a.every)


if __name__ == "__main__":
    sys.exit(main())
