import json

import pytest
from conftest import SCENARIOS
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from investigator.graph import Deps, investigate
from investigator.llm import ScriptedLLM
from investigator.state import Budget, IncidentContext
from investigator.telemetry import OTelTelemetry
from investigator.tools.catalog import build_registry
from investigator.tools.fixture import fixture_backends


def looping():
    return json.loads((SCENARIOS / "checkout_retry_storm.looping.script.json").read_text())


def run(scenario, data, telemetry=None, budget=None, **deps_kw):
    deps = Deps(build_registry(fixture_backends(scenario)), ScriptedLLM.from_file_data(data), **deps_kw)
    if telemetry:
        deps.telemetry = telemetry
    return investigate(deps, IncidentContext(**scenario["incident"]), budget)


def test_unguarded_loop_burns_the_whole_budget(scenario):
    final = run(scenario, looping(), guards=False)
    b = final["budget"]
    assert final["conclusion"].escalation_reason == "budget exhausted"
    assert (b.iterations, b.tool_calls, b.tokens) == (8, 19, 160_000)
    assert b.cost_usd == pytest.approx(0.5)


def test_no_progress_guard_stops_the_loop_early(scenario):
    final = run(scenario, looping())
    b = final["budget"]
    assert final["conclusion"].escalation_reason == "new evidence changed nothing in consecutive iterations"
    assert (b.iterations, b.tool_calls, b.tokens) == (3, 14, 64_000)
    assert all(t.startswith("no_progress") for t in b.guard_trips)


def test_repeated_question_guard_skips_reworded_queries(scenario):
    final = run(scenario, looping(), max_same_question=1)
    trips = final["budget"].guard_trips
    assert any(t.startswith("repeated_question: search_logs asked 1 times") for t in trips)
    assert sum(e.tool == "search_logs" for e in final["evidence"]) == 1


def test_token_budget_stops_an_expensive_run(scenario):
    final = run(scenario, looping(), guards=False, budget=Budget(max_tokens=50_000))
    assert final["conclusion"].escalation_reason == "budget exhausted"
    assert final["budget"].tokens >= 50_000 and final["budget"].iterations < 8


def test_correct_investigation_trips_no_guards(scenario, script):
    final = run(scenario, script)
    assert final["conclusion"].outcome == "root_cause_found" and final["budget"].guard_trips == []


@pytest.fixture
def otel():
    spans, reader = InMemorySpanExporter(), InMemoryMetricReader()
    tp = TracerProvider()
    tp.add_span_processor(SimpleSpanProcessor(spans))
    return OTelTelemetry(tp, MeterProvider(metric_readers=[reader])), spans, reader


def test_one_trace_per_investigation(scenario, script, otel):
    telemetry, exporter, reader = otel
    run(scenario, script, telemetry)
    spans = exporter.get_finished_spans()
    root = [s for s in spans if s.parent is None]
    assert [s.name for s in root] == ["invoke_agent sre-investigator"]
    assert {s.context.trace_id for s in spans} == {root[0].context.trace_id}
    by_id = {s.context.span_id: s for s in spans}
    nodes = [s for s in spans if s.name.startswith("node ")]
    assert all(s.parent.span_id == root[0].context.span_id for s in nodes)
    assert [s.name for s in nodes][:3] == ["node intake", "node triage", "node hypothesize"]
    tools = [s for s in spans if s.name.startswith("execute_tool ")]
    assert len(tools) == 16
    assert {by_id[s.parent.span_id].name for s in tools} == {"node triage", "node gather"}
    chats = [s for s in spans if s.name.startswith("chat ")]
    assert {by_id[s.parent.span_id].name for s in chats} == {"node hypothesize", "node plan", "node assess", "node report"}
    attrs = root[0].attributes
    assert attrs["sre.outcome"] == "root_cause_found" and attrs["sre.root_cause.id"] == "H3"
    assert attrs["sre.budget.tool_calls"] == 16
    diff = next(s for s in tools if s.attributes["gen_ai.tool.name"] == "diff_release")
    assert diff.attributes["sre.tool.ok"] and "40ms" in diff.attributes["sre.tool.summary"]


def test_guard_trips_become_span_events_and_metrics(scenario, otel):
    telemetry, exporter, reader = otel
    run(scenario, looping(), telemetry)
    events = [e for s in exporter.get_finished_spans() for e in s.events if e.name == "guard_tripped"]
    assert len(events) == 2 and events[0].attributes["sre.guard"] == "no_progress"
    metrics = {m.name: m for rm in reader.get_metrics_data().resource_metrics
               for sm in rm.scope_metrics for m in sm.metrics}
    assert {"sre_agent.investigations", "sre_agent.tool_calls", "gen_ai.client.token.usage",
            "sre_agent.cost_usd", "sre_agent.guard_trips", "sre_agent.investigation.duration"} <= set(metrics)
    [point] = metrics["sre_agent.investigations"].data.data_points
    assert point.attributes["sre.outcome"] == "escalated" and point.value == 1
    [guards] = metrics["sre_agent.guard_trips"].data.data_points
    assert guards.value == 2


def test_failed_tool_calls_are_marked_as_errors(scenario, otel):
    from investigator.tools.base import ToolContext, parse_ts

    telemetry, exporter, _ = otel
    registry = build_registry(fixture_backends(scenario))
    i = scenario["incident"]
    ctx = ToolContext(i["namespace"], parse_ts(i["window_start"]), parse_ts(i["window_end"]))
    with telemetry.tool_call("query_promql", {"expr": "up"}) as record:
        record(registry.call(ctx, "query_promql", {"expr": "up"}))   # raw PromQL isn't in recorded scenarios
    [span] = exporter.get_finished_spans()
    assert not span.status.is_ok and span.attributes["sre.tool.ok"] is False


def test_a_crashed_investigation_is_counted(scenario, otel):
    telemetry, exporter, reader = otel

    class Broken:
        def structured(self, *a):
            raise RuntimeError("provider down")

    deps = Deps(build_registry(fixture_backends(scenario)), Broken(), telemetry=telemetry)
    with pytest.raises(RuntimeError):
        investigate(deps, IncidentContext(**scenario["incident"]))
    metrics = {m.name: m for rm in reader.get_metrics_data().resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    [point] = metrics["sre_agent.investigations"].data.data_points
    assert point.attributes["sre.outcome"] == "error"
    root = [s for s in exporter.get_finished_spans() if s.parent is None][0]
    assert not root.status.is_ok
