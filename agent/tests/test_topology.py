from datetime import datetime, timedelta, timezone

from investigator.topology import (
    FRESH_EDGES,
    MERGE_EDGES,
    MERGE_WORKLOADS,
    Neo4jTopology,
    TopologyWriter,
    build_once,
    extract_edges,
    extract_workloads,
)


def test_edges_from_the_case_study_traces(scenario):
    edges = {(e.caller, e.callee): e for e in extract_edges(t["spans"] for t in scenario["traces"])}
    assert set(edges) == {
        ("frontend", "checkout"), ("checkout", "cart"), ("checkout", "currency"),
        ("checkout", "payment"), ("checkout", "shipping"), ("checkout", "email"),
    }
    pay = edges[("checkout", "payment")]
    # 7 healthy traces with one Charge each, 10 failed traces with six each
    assert (pay.calls, pay.errors, pay.operations) == (67, 60, {"oteldemo.PaymentService/Charge"})


def test_calls_within_a_service_are_not_edges():
    spans = [
        {"span_id": "a", "parent_id": None, "service": "checkout", "operation": "PlaceOrder"},
        {"span_id": "b", "parent_id": "a", "service": "checkout", "operation": "prepare"},
    ]
    assert extract_edges([spans]) == []


def test_workloads_come_from_resource_attributes():
    spans = [
        {"span_id": "a", "parent_id": None, "service": "checkout", "operation": "x",
         "resource": {"k8s.namespace.name": "otel-demo", "k8s.deployment.name": "checkout"}},
        {"span_id": "b", "parent_id": "a", "service": "payment", "operation": "y", "resource": {}},
    ]
    [w] = extract_workloads([spans])
    assert (w.service, w.namespace, w.name) == ("checkout", "otel-demo", "checkout")


class FakeTx:
    def __init__(self, log):
        self.log = log

    def run(self, query, **params):
        self.log.append((query, params))
        return self.log.results


class FakeSession:
    def __init__(self, log):
        self.log = log

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def run(self, query, **params):
        return FakeTx(self.log).run(query, **params)

    def execute_write(self, fn):
        fn(FakeTx(self.log))


class Log(list):
    results = []


class FakeDriver:
    def __init__(self):
        self.log = Log()

    def session(self, database=None):
        return FakeSession(self.log)


def test_writer_merges_edges_and_workloads(scenario):
    driver = FakeDriver()
    edges = extract_edges(t["spans"] for t in scenario["traces"])
    TopologyWriter(driver).write(edges, [], datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc))
    [(query, params)] = driver.log
    assert query == MERGE_EDGES
    assert params["seen_at"] == "2026-09-22T10:30:00+00:00"
    pay = next(e for e in params["edges"] if e["callee"] == "payment")
    assert pay == {"caller": "checkout", "callee": "payment", "calls": 67, "errors": 60, "operations": ["oteldemo.PaymentService/Charge"]}


def test_writer_skips_empty_batches():
    driver = FakeDriver()
    TopologyWriter(driver).write([], [], datetime.now(tz=timezone.utc))
    assert driver.log == []


def test_backend_returns_fresh_edges_only():
    driver = FakeDriver()
    driver.log.results = [{"caller": "checkout", "callee": "payment"}]
    assert Neo4jTopology(driver, freshness_hours=6).edges() == [("checkout", "payment")]
    assert driver.log[0] == (FRESH_EDGES, {"hours": 6})


class FakeJaeger:
    def __init__(self, scenario):
        self.traces = {t["trace_id"]: t["spans"] for t in scenario["traces"]}

    def services(self):
        return ["checkout", "payment"]

    def search(self, service, operation, start, end, errors_only, min_duration_ms, limit):
        # the same traces come back for both services; the builder must dedupe
        return [{"trace_id": tid} for tid in list(self.traces)[:limit]]

    def get(self, trace_id):
        return self.traces[trace_id]


def test_build_once_samples_dedupes_and_writes(scenario):
    driver = FakeDriver()
    edges, workloads = build_once(FakeJaeger(scenario), TopologyWriter(driver), timedelta(minutes=15), per_service=50)
    assert (edges, workloads) == (6, 0)
    [(query, params)] = driver.log
    assert query == MERGE_EDGES and len(params["edges"]) == 6
    assert "MERGE (s)-[r:RUNS_AS]->(d)" in MERGE_WORKLOADS


def test_client_timeout_counts_as_an_edge_failure():
    """A deadline the caller gave up on, above a server span that succeeded.

    Counting only the callee's status would report this edge as healthy.
    """
    spans = [
        {"span_id": "a", "parent_id": None, "service": "checkout", "kind": "server",
         "operation": "PlaceOrder", "error": False},
        {"span_id": "b", "parent_id": "a", "service": "checkout", "kind": "client",
         "operation": "Charge", "peer": "payment", "error": True},
        {"span_id": "c", "parent_id": "b", "service": "payment", "kind": "server",
         "operation": "Charge", "error": False},
    ]
    [edge] = extract_edges([spans])
    assert (edge.caller, edge.callee) == ("checkout", "payment")
    assert (edge.calls, edge.errors) == (1, 1)


def test_retry_storm_edge_is_attributed_to_the_caller(scenario):
    """The recorded scenario: every payment failure is caller-observed."""
    traces = [t["spans"] for t in scenario["traces"]]
    [edge] = [e for e in extract_edges(traces) if e.callee == "payment"]
    assert (edge.calls, edge.errors) == (67, 60)
    server_errors = sum(s.get("error", False) for t in traces for s in t
                        if s["service"] == "payment" and s.get("kind") == "server")
    assert server_errors == 0, "payment's own spans succeeded; only the caller timed out"
