"""Backends for the running lab. Read-only: HTTP GETs to Prometheus and Jaeger,
and `kubectl get` / `helm history|list|get values` for cluster state.

UNTESTED against a real cluster. Before relying on these, check against the
pinned demo version (see README.md, "Before running live"):
- the span-metrics metric and label names in RED_QUERIES
- service names as they appear in Jaeger and Prometheus
- where logs land (LogsBackend is left unconfigured until that's decided)
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, timezone
from typing import Optional

import httpx

from investigator.tools.backends import Backends
from investigator.tools.base import fmt_ts, parse_ts

# PromQL for RED metrics from the OpenTelemetry Collector's span-metrics connector.
# VERIFY metric and label names against the pinned demo's Prometheus.
_SEL = 'service_name="{service}", span_kind="SPAN_KIND_SERVER"'
RED_QUERIES = {
    "request_rate": f"sum(rate(traces_span_metrics_calls_total{{{_SEL}}}[1m]))",
    "error_rate": (
        f'sum(rate(traces_span_metrics_calls_total{{{_SEL}, status_code="STATUS_CODE_ERROR"}}[1m]))'
        f" / sum(rate(traces_span_metrics_calls_total{{{_SEL}}}[1m]))"
    ),
    "latency_p50": f"histogram_quantile(0.50, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{{{_SEL}}}[1m])))",
    "latency_p95": f"histogram_quantile(0.95, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{{{_SEL}}}[1m])))",
    "latency_p99": f"histogram_quantile(0.99, sum by (le) (rate(traces_span_metrics_duration_milliseconds_bucket{{{_SEL}}}[1m])))",
}

HTTP_TIMEOUT_S = 15
CLI_TIMEOUT_S = 30


def _ts_from_epoch(seconds: float) -> str:
    return fmt_ts(datetime.fromtimestamp(seconds, tz=timezone.utc))


def _normalize_ts(value: str) -> str:
    """Kubernetes and Helm may emit nanosecond fractions; Python parses up to microseconds."""
    value = re.sub(r"(\.\d{6})\d+", r"\1", value)
    return fmt_ts(parse_ts(value))


class PrometheusMetrics:
    def __init__(self, base_url: str, queries: Optional[dict] = None) -> None:
        self.base = base_url.rstrip("/")
        self.queries = queries or RED_QUERIES

    def _range(self, expr: str, start: datetime, end: datetime, step_s: int) -> list[dict]:
        r = httpx.get(
            f"{self.base}/api/v1/query_range",
            params={"query": expr, "start": start.timestamp(), "end": end.timestamp(), "step": step_s},
            timeout=HTTP_TIMEOUT_S,
        )
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(body.get("error", "Prometheus query failed"))
        return [
            {
                "labels": s.get("metric", {}),
                "points": [(_ts_from_epoch(float(t)), float(v)) for t, v in s["values"] if v not in ("NaN", "+Inf", "-Inf")],
            }
            for s in body["data"]["result"]
        ]

    def series(self, service, metric, start, end):
        result = self._range(self.queries[metric].replace("{service}", service), start, end, 60)
        return result[0]["points"] if result else []

    def query(self, expr, start, end, step_s):
        return self._range(expr, start, end, step_s)


class JaegerTraces:
    """Jaeger query HTTP API. In the demo it sits behind the frontend proxy at
    /jaeger/ui, so base_url is e.g. http://localhost:8080/jaeger/ui."""

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def _get(self, path: str, params: dict) -> dict:
        r = httpx.get(f"{self.base}{path}", params=params, timeout=HTTP_TIMEOUT_S)
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _spans(trace: dict) -> list[dict]:
        procs = trace.get("processes", {})
        out = []
        for sp in trace.get("spans", []):
            parent = next((ref["spanID"] for ref in sp.get("references", []) if ref.get("refType") == "CHILD_OF"), None)
            tags = {t["key"]: t.get("value") for t in sp.get("tags", [])}
            error = tags.get("error") in (True, "true") or tags.get("otel.status_code") == "ERROR"
            proc = procs.get(sp.get("processID"), {})
            kind = str(tags.get("span.kind", "")).lower() or None
            peer = next((tags[k] for k in ("peer.service", "rpc.service", "server.address", "net.peer.name") if tags.get(k)), None)
            out.append({
                "span_id": sp["spanID"],
                "parent_id": parent,
                "service": proc.get("serviceName", "unknown"),
                # OpenTelemetry resource attributes arrive as Jaeger process tags
                "resource": {t["key"]: t.get("value") for t in proc.get("tags", [])},
                "operation": sp["operationName"],
                "start": _ts_from_epoch(sp["startTime"] / 1e6),
                "duration_ms": sp["duration"] / 1000,
                "error": error,
                "kind": kind,
                "peer": peer,
            })
        return out

    def services(self) -> list[str]:
        return sorted(self._get("/api/services", {}).get("data", []) or [])

    def search(self, service, operation, start, end, errors_only, min_duration_ms, limit):
        params = {
            "service": service,
            "start": int(start.timestamp() * 1e6),
            "end": int(end.timestamp() * 1e6),
            "limit": limit,
        }
        if operation:
            params["operation"] = operation
        if min_duration_ms:
            params["minDuration"] = f"{int(min_duration_ms)}ms"
        if errors_only:
            params["tags"] = json.dumps({"error": "true"})
        out = []
        for trace in self._get("/api/traces", params).get("data", []):
            spans = self._spans(trace)
            if not spans:
                continue
            root = next((s for s in spans if s["parent_id"] is None), min(spans, key=lambda s: s["start"]))
            out.append({
                "trace_id": trace["traceID"],
                "root_service": root["service"],
                "root_operation": root["operation"],
                "start": root["start"],
                "duration_ms": root["duration_ms"],
                "error": any(s["error"] for s in spans),
            })
        return out

    def get(self, trace_id):
        data = self._get(f"/api/traces/{trace_id}", {}).get("data", [])
        return self._spans(data[0]) if data else []


class JaegerTopology:
    """Call graph from Jaeger's dependency endpoint. Chapter 2 replaces this
    with the Neo4j topology graph; the interface stays the same."""

    def __init__(self, base_url: str, lookback_hours: int = 1) -> None:
        self.base = base_url.rstrip("/")
        self.lookback_ms = lookback_hours * 3600 * 1000

    def edges(self):
        end_ms = int(datetime.now(tz=timezone.utc).timestamp() * 1000)
        r = httpx.get(
            f"{self.base}/api/dependencies",
            params={"endTs": end_ms, "lookback": self.lookback_ms},
            timeout=HTTP_TIMEOUT_S,
        )
        r.raise_for_status()
        return [(d["parent"], d["child"]) for d in r.json().get("data", []) if d["parent"] != d["child"]]


class UnconfiguredLogs:
    """Placeholder until the log backend is confirmed (DESIGN.md, Decisions).
    Every call comes back to the agent as a failed tool result, not a crash."""

    def search(self, service, start, end, pattern, level, limit):
        raise RuntimeError("no log backend is configured for live runs yet")


def _run_json(cmd: list[str]) -> dict:
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=CLI_TIMEOUT_S)
    if out.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd[:3])} failed: {out.stderr.strip()[:300]}")
    return json.loads(out.stdout or "null")


class HelmChanges:
    """Needs get/list on Secrets in the namespace (where Helm stores releases)."""

    def list(self, namespace, start, end):
        out = self._helm(namespace, start, end)
        # Changes made outside Helm still leave a Deployment rollout (a new
        # ReplicaSet). Rollouts within two minutes of a Helm revision are that
        # release's own rollout and are skipped.
        helm_times = [parse_ts(c["updated"]) for c in out]
        for r in _rollouts(namespace):
            t = parse_ts(r["updated"])
            if start <= t <= end and not any(abs((t - h).total_seconds()) <= 120 for h in helm_times):
                out.append(r)
        return out

    def _helm(self, namespace, start, end):
        releases = _run_json(["helm", "list", "-n", namespace, "-o", "json"]) or []
        out = []
        for rel in releases:
            name = rel["name"]
            for h in _run_json(["helm", "history", name, "-n", namespace, "-o", "json"]) or []:
                updated = _normalize_ts(h["updated"])
                if start <= parse_ts(updated) <= end:
                    out.append({
                        "release": name,
                        "revision": h["revision"],
                        "updated": updated,
                        "status": h.get("status"),
                        "chart": h.get("chart"),
                        "description": h.get("description", ""),
                    })
        return out

    def values(self, namespace, release, revision):
        if release.startswith("deployment/"):
            return _rollout_template(namespace, release.split("/", 1)[1], revision)
        return _run_json(["helm", "get", "values", release, "-n", namespace, "--revision", str(revision), "-o", "json"]) or {}


def _replicasets(namespace):
    items = _run_json(["kubectl", "get", "replicasets", "-n", namespace, "-o", "json"]).get("items", [])
    for rs in items:
        owner = next((o["name"] for o in rs["metadata"].get("ownerReferences", []) if o.get("kind") == "Deployment"), None)
        rev = rs["metadata"].get("annotations", {}).get("deployment.kubernetes.io/revision")
        if owner and rev:
            yield owner, int(rev), rs


def _rollouts(namespace):
    return [
        {"release": f"deployment/{owner}", "revision": rev, "updated": _normalize_ts(rs["metadata"]["creationTimestamp"]),
         "status": "rollout", "chart": None, "description": "Deployment rollout outside Helm", "source": "rollout"}
        for owner, rev, rs in _replicasets(namespace)
    ]


def _rollout_template(namespace, deployment, revision):
    """Container images and environment of one rollout revision (secrets are
    referenced, not inlined, in pod templates; values are redacted later anyway)."""
    for owner, rev, rs in _replicasets(namespace):
        if owner == deployment and rev == revision:
            containers = rs["spec"]["template"]["spec"].get("containers", [])
            return {"containers": [{"name": c["name"], "image": c.get("image"),
                                    "env": [{"name": e["name"], "value": e.get("value")} for e in c.get("env", [])]}
                                   for c in containers]}
    raise KeyError(f"no revision {revision} of deployment/{deployment}")


class KubectlK8s:
    def events(self, namespace, start, end, reasons):
        items = _run_json(["kubectl", "get", "events", "-n", namespace, "-o", "json"]).get("items", [])
        out = []
        for ev in items:
            raw_t = ev.get("lastTimestamp") or ev.get("eventTime") or ev.get("firstTimestamp")
            if not raw_t:
                continue
            t = _normalize_ts(raw_t)
            if not (start <= parse_ts(t) <= end) or (reasons and ev.get("reason") not in reasons):
                continue
            obj = ev.get("involvedObject", {})
            out.append({
                "t": t,
                "reason": ev.get("reason"),
                "object": f"{obj.get('kind')}/{obj.get('name')}",
                "message": ev.get("message", ""),
                "count": ev.get("count") or 1,
            })
        return out

    def workload(self, namespace, name):
        d = _run_json(["kubectl", "get", "deployment", name, "-n", namespace, "-o", "json"])
        labels = d["spec"]["selector"].get("matchLabels", {})
        selector = ",".join(f"{k}={v}" for k, v in labels.items())
        pods = _run_json(["kubectl", "get", "pods", "-n", namespace, "-l", selector, "-o", "json"]).get("items", [])
        restarts = sum(cs.get("restartCount", 0) for p in pods for cs in p.get("status", {}).get("containerStatuses", []))
        hpa = None
        try:
            h = _run_json(["kubectl", "get", "hpa", name, "-n", namespace, "-o", "json"])
            current = next((m["resource"]["current"].get("averageUtilization") for m in h.get("status", {}).get("currentMetrics", []) or [] if m.get("type") == "Resource"), None)
            target = next((m["resource"]["target"].get("averageUtilization") for m in h["spec"].get("metrics", []) if m.get("type") == "Resource"), None)
            hpa = {
                "min": h["spec"].get("minReplicas", 1),
                "max": h["spec"]["maxReplicas"],
                "current_replicas": h.get("status", {}).get("currentReplicas"),
                "current_cpu_percent": current,
                "target_cpu_percent": target,
            }
        except RuntimeError:
            pass  # no autoscaler for this workload
        return {
            "name": name,
            "desired": d["spec"].get("replicas", 1),
            "ready": d.get("status", {}).get("readyReplicas", 0),
            "restarts": restarts,
            "hpa": hpa,
        }

    def nodes(self):
        items = _run_json(["kubectl", "get", "nodes", "-o", "json"]).get("items", [])
        watched = ("MemoryPressure", "DiskPressure", "PIDPressure")
        return [
            {
                "name": n["metadata"]["name"],
                "conditions": {
                    c["type"]: c["status"] == "True"
                    for c in n.get("status", {}).get("conditions", [])
                    if c["type"] in watched
                },
            }
            for n in items
        ]


def live_backends(prometheus_url: str, jaeger_url: str) -> Backends:
    return Backends(
        metrics=PrometheusMetrics(prometheus_url),
        traces=JaegerTraces(jaeger_url),
        logs=UnconfiguredLogs(),
        changes=HelmChanges(),
        k8s=KubectlK8s(),
        topology=JaegerTopology(jaeger_url),
    )
