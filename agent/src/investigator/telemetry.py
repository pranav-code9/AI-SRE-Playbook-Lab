"""Telemetry for the agent itself (Chapter 7).

`Telemetry` is the interface the graph calls; it does nothing, so the agent
runs without OpenTelemetry installed. `OTelTelemetry` emits one trace per
investigation (a span per graph node, with a child span for every tool call
and model call) and metrics for outcomes, tool calls, tokens, cost and guard
trips.

Span and attribute names follow the OpenTelemetry semantic conventions for
generative AI where they exist (`invoke_agent`, `execute_tool`, `chat`,
`gen_ai.*`). Those conventions are still marked as in development, so check
them against the current specification when you upgrade. Everything specific
to this agent is under `sre.*`.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

AGENT_NAME = "sre-investigator"


class _Handle:
    def finish(self, final_state: dict) -> None:
        pass


class Telemetry:
    """No-op telemetry. Every method is safe to call and does nothing."""

    @contextmanager
    def investigation(self, incident) -> Iterator[_Handle]:
        yield _Handle()

    @contextmanager
    def node(self, name: str) -> Iterator[None]:
        yield

    @contextmanager
    def tool_call(self, tool: str, args: dict) -> Iterator[Callable[[Any], None]]:
        yield lambda result: None

    @contextmanager
    def llm_call(self, schema: str, model: str) -> Iterator[Callable[[float, dict], None]]:
        yield lambda cost, usage: None

    def event(self, name: str, **attributes) -> None:
        pass

    def guard(self, name: str, detail: str) -> None:
        pass


class OTelTelemetry(Telemetry):
    """OpenTelemetry-backed telemetry. Pass providers explicitly (tests use
    in-memory ones) or let it use the globally configured providers."""

    def __init__(self, tracer_provider=None, meter_provider=None) -> None:
        from opentelemetry import metrics, trace

        self._trace = trace
        self.tracer = trace.get_tracer("investigator", tracer_provider=tracer_provider)
        meter = metrics.get_meter("investigator", meter_provider=meter_provider)
        self.m_investigations = meter.create_counter("sre_agent.investigations", description="Investigations by outcome")
        self.m_duration = meter.create_histogram("sre_agent.investigation.duration", unit="s")
        self.m_iterations = meter.create_histogram("sre_agent.investigation.iterations")
        self.m_tool_calls = meter.create_counter("sre_agent.tool_calls", description="Tool calls by tool and result")
        self.m_tokens = meter.create_histogram("gen_ai.client.token.usage", unit="{token}")
        self.m_cost = meter.create_counter("sre_agent.cost_usd", description="Model cost in US dollars")
        self.m_guards = meter.create_counter("sre_agent.guard_trips", description="Loop and budget guards that fired")
        self._root = None
        self._node = None
        self._alert = None

    def _ctx(self, span):
        return self._trace.set_span_in_context(span) if span is not None else None

    @contextmanager
    def investigation(self, incident):
        # Explicit parents rather than ambient context: graph nodes may run on
        # worker threads, and the trace must still hang together.
        self._alert = incident.alert_id
        with self.tracer.start_as_current_span(f"invoke_agent {AGENT_NAME}", attributes={
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": AGENT_NAME,
            "sre.alert.id": incident.alert_id,
            "sre.alert.service": incident.service,
            "k8s.namespace.name": incident.namespace,
        }) as span:
            self._root = span
            started = time.perf_counter()
            telemetry = self

            class Handle(_Handle):
                def finish(self, final: dict) -> None:
                    b, c = final["budget"], final.get("conclusion")
                    outcome = c.outcome if c else "none"
                    attrs = {
                        "sre.outcome": outcome,
                        "sre.budget.iterations": b.iterations,
                        "sre.budget.tool_calls": b.tool_calls,
                        "sre.budget.tokens": b.tokens,
                        "sre.budget.cost_usd": b.cost_usd,
                        "sre.evidence.count": len(final["evidence"]),
                        "sre.hypotheses.count": len(final["hypotheses"]),
                        "sre.change_search": final["change_search"],
                    }
                    if c and c.root_cause_id:
                        attrs["sre.root_cause.id"] = c.root_cause_id
                        attrs["sre.root_cause.confidence"] = c.confidence
                    if c and c.escalation_reason:
                        attrs["sre.escalation.reason"] = c.escalation_reason
                    span.set_attributes(attrs)
                    labels = {"sre.outcome": outcome, "sre.alert.id": telemetry._alert}
                    telemetry.m_investigations.add(1, labels)
                    telemetry.m_duration.record(time.perf_counter() - started, labels)
                    telemetry.m_iterations.record(b.iterations, labels)

            try:
                yield Handle()
            except Exception as exc:
                # A crashed investigation is the agent's own outage: count it.
                span.record_exception(exc)
                span.set_status(self._trace.Status(self._trace.StatusCode.ERROR, type(exc).__name__))
                self.m_investigations.add(1, {"sre.outcome": "error", "sre.alert.id": self._alert})
                raise
            finally:
                self._root = None

    @contextmanager
    def node(self, name):
        with self.tracer.start_as_current_span(f"node {name}", context=self._ctx(self._root),
                                               attributes={"sre.node": name}) as span:
            self._node = span
            try:
                yield
            finally:
                self._node = None

    @contextmanager
    def tool_call(self, tool, args):
        parent = self._node or self._root
        with self.tracer.start_as_current_span(f"execute_tool {tool}", context=self._ctx(parent), attributes={
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": tool,
            "sre.tool.args": _short(args),
        }) as span:
            def record(result):
                span.set_attributes({"sre.tool.ok": result.ok, "sre.tool.summary": result.summary[:500]})
                if not result.ok:
                    span.set_status(self._trace.Status(self._trace.StatusCode.ERROR, result.error or "tool failed"))
                self.m_tool_calls.add(1, {"gen_ai.tool.name": tool, "sre.tool.ok": result.ok})
            yield record

    @contextmanager
    def llm_call(self, schema, model):
        parent = self._node or self._root
        with self.tracer.start_as_current_span(f"chat {model}", context=self._ctx(parent), attributes={
            "gen_ai.operation.name": "chat",
            "gen_ai.request.model": model,
            "sre.output_schema": schema,
        }) as span:
            def record(cost, usage):
                span.set_attributes({
                    "gen_ai.usage.input_tokens": usage.get("input_tokens", 0),
                    "gen_ai.usage.output_tokens": usage.get("output_tokens", 0),
                    "sre.cost_usd": cost,
                })
                base = {"gen_ai.operation.name": "chat", "gen_ai.request.model": model}
                self.m_tokens.record(usage.get("input_tokens", 0), {**base, "gen_ai.token.type": "input"})
                self.m_tokens.record(usage.get("output_tokens", 0), {**base, "gen_ai.token.type": "output"})
                self.m_cost.add(cost, {"gen_ai.request.model": model})
            yield record

    def event(self, name, **attributes):
        span = self._node or self._root
        if span is not None:
            span.add_event(name, attributes=attributes)

    def guard(self, name, detail):
        self.event("guard_tripped", **{"sre.guard": name, "sre.guard.detail": detail})
        self.m_guards.add(1, {"sre.guard": name})


def _short(args: dict) -> str:
    text = ", ".join(f"{k}={v!r}" for k, v in args.items())
    return text[:300]


def otlp_telemetry(endpoint: Optional[str] = None, service_name: str = AGENT_NAME) -> OTelTelemetry:
    """Configure OTLP export (gRPC) and return telemetry using it. With no
    endpoint, the standard OTEL_EXPORTER_OTLP_* environment variables apply."""
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    kw = {"endpoint": endpoint, "insecure": True} if endpoint else {}
    resource = Resource.create({"service.name": service_name})
    tp = TracerProvider(resource=resource)
    tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(**kw)))
    mp = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(OTLPMetricExporter(**kw))])
    trace.set_tracer_provider(tp)
    metrics.set_meter_provider(mp)
    return OTelTelemetry(tp, mp)


def shutdown() -> None:
    """Flush exporters before the process exits."""
    from opentelemetry import metrics, trace

    for provider in (trace.get_tracer_provider(), metrics.get_meter_provider()):
        if hasattr(provider, "shutdown"):
            provider.shutdown()
