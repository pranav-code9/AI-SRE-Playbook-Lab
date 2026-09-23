"""The twelve read-only investigation tools.

Every summary is produced by code, not a model, so the same data always yields
the same summary. Results are capped; full payloads go to the raw store.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from statistics import mean
from typing import Literal, Optional

from pydantic import Field

from investigator.tools.backends import METRICS, Backends
from investigator.safety import config_value, untrusted
from investigator.tools.base import Tool, ToolArgs, ToolContext, ToolRegistry, ToolResult, fmt_ts

MAX_TRACES = 20
MAX_LOG_LINES = 50
MAX_EVENTS = 50
MAX_SERIES = 5
MAX_POINTS = 60


def _result(ctx: ToolContext, tool: str, args: ToolArgs, summary: str, data: dict, raw: object) -> ToolResult:
    query = args.model_dump(exclude_none=True)
    return ToolResult(ok=True, summary=summary, data=data, raw_ref=ctx.raw_store.put(tool, query, raw), query=query)


def _avg(points: list[tuple[str, float]]) -> Optional[float]:
    return mean(v for _, v in points) if points else None


def _fmt_metric(metric: str, value: Optional[float]) -> str:
    if value is None:
        return "no data"
    if metric == "error_rate":
        return f"{value * 100:.1f}%"
    if metric.startswith("latency"):
        return f"{value:.0f} ms"
    return f"{value:.1f} req/s"


class WindowArgs(ToolArgs):
    start: Optional[str] = Field(None, description="ISO 8601; defaults to investigation window start")
    end: Optional[str] = Field(None, description="ISO 8601; defaults to investigation window end")


# 1. get_service_red ---------------------------------------------------------

class ServiceRedArgs(WindowArgs):
    service: str


def get_service_red(b: Backends):
    def run(ctx: ToolContext, a: ServiceRedArgs) -> ToolResult:
        s, e = ctx.clamp(a.start, a.end)
        values = {m: _avg(b.metrics.series(a.service, m, s, e)) for m in METRICS}
        summary = (
            f"{a.service} {fmt_ts(s)}–{fmt_ts(e)}: {_fmt_metric('request_rate', values['request_rate'])}, "
            f"errors {_fmt_metric('error_rate', values['error_rate'])}, "
            f"p50 {_fmt_metric('latency_p50', values['latency_p50'])}, "
            f"p95 {_fmt_metric('latency_p95', values['latency_p95'])} (window averages)."
        )
        return _result(ctx, "get_service_red", a, summary, {"service": a.service, **values}, values)

    return Tool("get_service_red", "metric", "Rate, errors and latency averages for one service.", ServiceRedArgs, run)


# 2. compare_windows ---------------------------------------------------------

class CompareArgs(ToolArgs):
    service: str
    metric: Literal["request_rate", "error_rate", "latency_p50", "latency_p95", "latency_p99"]
    before_start: str
    before_end: str
    after_start: str
    after_end: str


def compare_windows(b: Backends):
    def run(ctx: ToolContext, a: CompareArgs) -> ToolResult:
        bs, be = ctx.clamp(a.before_start, a.before_end)
        as_, ae = ctx.clamp(a.after_start, a.after_end)
        before = _avg(b.metrics.series(a.service, a.metric, bs, be))
        after = _avg(b.metrics.series(a.service, a.metric, as_, ae))
        if before is None or after is None:
            change = None
            summary = f"{a.service} {a.metric}: not enough data to compare."
        else:
            change = ((after - before) / before * 100) if before else None
            pct = f"{change:+.0f}%" if change is not None else "from zero"
            summary = (
                f"{a.service} {a.metric}: {_fmt_metric(a.metric, before)} before, "
                f"{_fmt_metric(a.metric, after)} after ({pct})."
            )
        data = {"before": before, "after": after, "percent_change": change}
        return _result(ctx, "compare_windows", a, summary, data, data)

    return Tool("compare_windows", "metric", "Compare one metric for a service between two windows.", CompareArgs, run)


# 3. query_promql ------------------------------------------------------------

class PromqlArgs(WindowArgs):
    expr: str
    step_s: int = Field(60, ge=15, le=600)


def query_promql(b: Backends):
    def run(ctx: ToolContext, a: PromqlArgs) -> ToolResult:
        s, e = ctx.clamp(a.start, a.end)
        series = b.metrics.query(a.expr, s, e, a.step_s)
        capped = [{"labels": x["labels"], "points": x["points"][-MAX_POINTS:]} for x in series[:MAX_SERIES]]
        parts = []
        for x in capped:
            pts = [v for _, v in x["points"]]
            if pts:
                parts.append(f"{x['labels']}: min {min(pts):.3g}, max {max(pts):.3g}, last {pts[-1]:.3g}")
        summary = f"{len(series)} series" + (f" (showing {len(capped)})" if len(series) > len(capped) else "")
        summary += (". " + "; ".join(parts)) if parts else ". No data points."
        return _result(ctx, "query_promql", a, summary, {"series": capped}, series)

    return Tool("query_promql", "metric", "Raw PromQL range query. Escape hatch when typed tools don't fit.", PromqlArgs, run)


# 4. search_traces -----------------------------------------------------------

class SearchTracesArgs(WindowArgs):
    service: str
    operation: Optional[str] = None
    errors_only: bool = False
    min_duration_ms: Optional[float] = None
    limit: int = Field(10, ge=1, le=MAX_TRACES)


def search_traces(b: Backends):
    def run(ctx: ToolContext, a: SearchTracesArgs) -> ToolResult:
        s, e = ctx.clamp(a.start, a.end)
        traces = b.traces.search(a.service, a.operation, s, e, a.errors_only, a.min_duration_ms, a.limit)
        traces = sorted(traces, key=lambda t: t["start"])[: a.limit]
        if traces:
            errs = sum(1 for t in traces if t["error"])
            durs = [t["duration_ms"] for t in traces]
            summary = (
                f"{len(traces)} traces for {a.service}{'/' + a.operation if a.operation else ''}: "
                f"{errs} with errors, duration {min(durs):.0f}–{max(durs):.0f} ms. "
                f"Example ids: {', '.join(t['trace_id'] for t in traces[:3])}."
            )
        else:
            summary = f"No traces found for {a.service} in the window."
        return _result(ctx, "search_traces", a, summary, {"traces": traces}, traces)

    return Tool("search_traces", "trace", "Find traces for a service, optionally errors or slow ones only.", SearchTracesArgs, run)


# 5. summarize_trace ---------------------------------------------------------

class TraceArgs(ToolArgs):
    trace_id: str


def repeated_calls(spans: list[dict]) -> list[dict]:
    """Calls one service made to another more than once under the same parent:
    retries, or a loop. In real OpenTelemetry traces each outgoing call is a
    CLIENT span in the caller; its callee is the service of the SERVER span
    beneath it, or the client span's peer attribute if the callee never
    answered. Spans without a kind (older data) are grouped as they are."""
    children: dict[str, list[dict]] = defaultdict(list)
    for sp in spans:
        children[sp.get("parent_id")].append(sp)
    by_id = {sp["span_id"]: sp for sp in spans}
    kinds = {sp.get("kind") for sp in spans}
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for sp in spans:
        if "client" in kinds:
            if sp.get("kind") != "client":
                continue
            server = next((c for c in children[sp["span_id"]] if c["service"] != sp["service"]), None)
            callee = server["service"] if server else (sp.get("peer") or "unknown")
            caller = sp["service"]
        else:
            parent = by_id.get(sp.get("parent_id"))
            caller, callee = (parent["service"] if parent else "root"), sp["service"]
        groups[(sp.get("parent_id"), caller, callee, sp["operation"])].append(sp)
    return [
        {"caller": caller, "callee": callee, "operation": op, "count": len(members),
         "errors": sum(1 for m in members if m["error"])}
        for (_, caller, callee, op), members in groups.items() if len(members) > 1
    ]


def summarize_trace(b: Backends):
    def run(ctx: ToolContext, a: TraceArgs) -> ToolResult:
        spans = b.traces.get(a.trace_id)
        if not spans:
            return ToolResult(ok=False, summary=f"Trace {a.trace_id} not found.", query=a.model_dump(), error="not_found")
        repeated = repeated_calls(spans)
        root = next((sp for sp in spans if sp["parent_id"] is None), spans[0])
        # Client spans mirror the server span they call; the slowest *server-side* work is the useful fact.
        inner = [sp for sp in spans if sp is not root and sp.get("kind") != "client"]
        slowest = max(inner, key=lambda sp: sp["duration_ms"], default=root)
        errors = [sp for sp in spans if sp["error"]]
        parts = [
            f"Trace {a.trace_id}: {len(spans)} spans across {len({sp['service'] for sp in spans})} services, "
            f"root {root['service']}/{root['operation']} {root['duration_ms']:.0f} ms"
            f"{' (error)' if root['error'] else ''}."
        ]
        for r in sorted(repeated, key=lambda r: -r["count"]):
            parts.append(
                f"{r['caller']} called {r['callee']} ({untrusted(r['operation'], 80)}) {r['count']} times "
                f"under one parent ({r['errors']} failed)."
            )
        parts.append(f"Slowest span: {slowest['service']}/{untrusted(slowest['operation'], 80)} {slowest['duration_ms']:.0f} ms.")
        if errors:
            parts.append(f"{len(errors)} spans in error.")
        data = {"root": root, "repeated_calls": repeated, "error_spans": len(errors), "span_count": len(spans)}
        return _result(ctx, "summarize_trace", a, " ".join(parts), data, spans)

    return Tool("summarize_trace", "trace", "Summarise one trace, including repeated calls (retries).", TraceArgs, run)


# 6. search_logs -------------------------------------------------------------

class LogsArgs(WindowArgs):
    service: str
    pattern: Optional[str] = None
    level: Optional[Literal["debug", "info", "warn", "error"]] = None
    limit: int = Field(20, ge=1, le=MAX_LOG_LINES)


def search_logs(b: Backends):
    def run(ctx: ToolContext, a: LogsArgs) -> ToolResult:
        s, e = ctx.clamp(a.start, a.end)
        lines = b.logs.search(a.service, s, e, a.pattern, a.level, MAX_LOG_LINES * 10)
        counts = Counter(line["message"] for line in lines)
        top = counts.most_common(a.limit)
        if top:
            summary = f"{len(lines)} log lines from {a.service}; top messages: " + "; ".join(
                f"\u201c{untrusted(msg, 100)}\u201d \u00d7{n}" for msg, n in top[:3]
            ) + "."
        else:
            summary = f"No matching log lines from {a.service}."
        data = {"total": len(lines), "messages": [{"message": untrusted(m, 300), "count": n} for m, n in top]}
        return _result(ctx, "search_logs", a, summary, data, lines[: MAX_LOG_LINES * 10])

    return Tool("search_logs", "log", "Search a service's logs; returns deduplicated messages with counts.", LogsArgs, run)


# 7. list_changes ------------------------------------------------------------

class ChangesArgs(WindowArgs):
    namespace: Optional[str] = None


def list_changes(b: Backends):
    def run(ctx: ToolContext, a: ChangesArgs) -> ToolResult:
        s, e = ctx.clamp(a.start, a.end, change=True)
        ns = a.namespace or ctx.namespace
        changes = sorted(b.changes.list(ns, s, e), key=lambda c: c["updated"])
        if changes:
            summary = f"{len(changes)} change(s) in {ns} {fmt_ts(s)}–{fmt_ts(e)}: " + "; ".join(
                f"{c['release']} revision {c['revision']} at {c['updated']} ({untrusted(c.get('description', ''), 80)})"
                for c in changes
            ) + "."
        else:
            summary = f"No changes in {ns} {fmt_ts(s)}–{fmt_ts(e)}."
        return _result(ctx, "list_changes", a, summary, {"changes": changes, "start": fmt_ts(s), "end": fmt_ts(e)}, changes)

    return Tool("list_changes", "change", "Helm releases and rollouts in the namespace during the window.", ChangesArgs, run)


# 8. diff_release ------------------------------------------------------------

class DiffArgs(ToolArgs):
    release: str
    revision_a: int
    revision_b: int
    namespace: Optional[str] = None


def _flatten(value, prefix: str = "") -> dict[str, object]:
    out: dict[str, object] = {}
    if isinstance(value, dict):
        for k, v in value.items():
            out.update(_flatten(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(value, list):
        named = all(isinstance(i, dict) and "name" in i for i in value)
        for idx, item in enumerate(value):
            key = item["name"] if named else str(idx)
            inner = {k: v for k, v in item.items() if k != "name"} if named else item
            out.update(_flatten(inner, f"{prefix}[{key}]"))
    else:
        out[prefix] = value
    return out


def diff_release(b: Backends):
    def run(ctx: ToolContext, a: DiffArgs) -> ToolResult:
        ns = a.namespace or ctx.namespace
        before = _flatten(b.changes.values(ns, a.release, a.revision_a))
        after = _flatten(b.changes.values(ns, a.release, a.revision_b))
        diffs = []
        for key in sorted(set(before) | set(after)):
            if before.get(key) != after.get(key):
                # Compared raw, shown redacted: a changed secret appears as two
                # different fingerprints, never as its value (even in the raw store).
                diffs.append({"key": key, "before": config_value(key, before.get(key)),
                              "after": config_value(key, after.get(key))})
        if diffs:
            summary = f"{a.release} revision {a.revision_a} → {a.revision_b}: {len(diffs)} value(s) changed: " + "; ".join(
                f"{d['key']}: {d['before']!r} → {d['after']!r}" for d in diffs[:10]
            ) + "."
        else:
            summary = f"{a.release} revisions {a.revision_a} and {a.revision_b} have identical values."
        return _result(ctx, "diff_release", a, summary, {"diffs": diffs}, diffs)

    return Tool("diff_release", "change", "Values and environment variables that differ between two release revisions.", DiffArgs, run)


# 9. get_k8s_events ----------------------------------------------------------

class EventsArgs(WindowArgs):
    namespace: Optional[str] = None
    reasons: Optional[list[str]] = None


def get_k8s_events(b: Backends):
    def run(ctx: ToolContext, a: EventsArgs) -> ToolResult:
        s, e = ctx.clamp(a.start, a.end)
        ns = a.namespace or ctx.namespace
        events = sorted(b.k8s.events(ns, s, e, a.reasons), key=lambda ev: ev["t"])
        by_reason = Counter()
        first_seen: dict[str, str] = {}
        for ev in events:
            by_reason[ev["reason"]] += ev.get("count", 1)
            first_seen.setdefault(ev["reason"], ev["t"])
        if events:
            summary = f"{len(events)} events in {ns}: " + "; ".join(
                f"{reason} ×{n} (first {first_seen[reason]})" for reason, n in by_reason.most_common()
            ) + "."
        else:
            summary = f"No matching events in {ns}."
        data = {"counts": dict(by_reason), "first_seen": first_seen, "events": events[:MAX_EVENTS]}
        return _result(ctx, "get_k8s_events", a, summary, data, events)

    return Tool("get_k8s_events", "k8s", "Kubernetes events (evictions, rescales, scheduling failures) in the window.", EventsArgs, run)


# 10. get_workload_status ----------------------------------------------------

class WorkloadArgs(ToolArgs):
    name: str
    namespace: Optional[str] = None


def get_workload_status(b: Backends):
    def run(ctx: ToolContext, a: WorkloadArgs) -> ToolResult:
        ns = a.namespace or ctx.namespace
        w = b.k8s.workload(ns, a.name)
        hpa = w.get("hpa")
        summary = f"{a.name}: {w['ready']}/{w['desired']} ready, {w['restarts']} restarts"
        if hpa:
            summary += (
                f"; HPA {hpa['current_replicas']} replicas (min {hpa['min']}, max {hpa['max']}), "
                f"CPU {hpa.get('current_cpu_percent', '?')}% of {hpa.get('target_cpu_percent', '?')}% target"
            )
        return _result(ctx, "get_workload_status", a, summary + ".", w, w)

    return Tool("get_workload_status", "k8s", "Replicas, restarts and autoscaler state for one Deployment.", WorkloadArgs, run)


# 11. get_node_conditions ----------------------------------------------------

class NodesArgs(ToolArgs):
    pass


def get_node_conditions(b: Backends):
    def run(ctx: ToolContext, a: NodesArgs) -> ToolResult:
        nodes = b.k8s.nodes()
        pressured = [
            f"{n['name']} ({', '.join(c for c, on in n['conditions'].items() if on)})"
            for n in nodes
            if any(n["conditions"].values())
        ]
        summary = f"{len(nodes)} nodes; " + (
            "under pressure: " + "; ".join(pressured) if pressured else "none under pressure"
        ) + "."
        return _result(ctx, "get_node_conditions", a, summary, {"nodes": nodes}, nodes)

    return Tool("get_node_conditions", "k8s", "Memory, disk and PID pressure for every node.", NodesArgs, run)


# 12. get_dependencies -------------------------------------------------------

class DepsArgs(ToolArgs):
    service: str
    direction: Literal["downstream", "upstream"] = "downstream"
    depth: int = Field(1, ge=1, le=3)


def get_dependencies(b: Backends):
    def run(ctx: ToolContext, a: DepsArgs) -> ToolResult:
        edges = b.topology.edges()
        frontier, seen, found = {a.service}, {a.service}, []
        for _ in range(a.depth):
            nxt = set()
            for caller, callee in edges:
                src, dst = (caller, callee) if a.direction == "downstream" else (callee, caller)
                if src in frontier and dst not in seen:
                    found.append(dst)
                    seen.add(dst)
                    nxt.add(dst)
            frontier = nxt
        verb = "calls" if a.direction == "downstream" else "is called by"
        summary = f"{a.service} {verb}: {', '.join(found) if found else 'nothing recorded'} (depth {a.depth})."
        return _result(ctx, "get_dependencies", a, summary, {"services": found}, edges)

    return Tool("get_dependencies", "topology", "Services a service calls, or is called by, from the topology graph.", DepsArgs, run)


def build_registry(backends: Backends) -> ToolRegistry:
    factories = [
        get_service_red, compare_windows, query_promql, search_traces, summarize_trace, search_logs,
        list_changes, diff_release, get_k8s_events, get_workload_status, get_node_conditions, get_dependencies,
    ]
    return ToolRegistry([f(backends) for f in factories])
