from datetime import datetime, timezone

import pytest

from investigator.llm import LangChainLLM, LLMError
from investigator.schemas import ReportOutput
from investigator.tools import live


class _Raw:
    usage_metadata = {"input_tokens": 1000, "output_tokens": 500}


class _Runnable:
    def __init__(self, outputs):
        self.outputs = list(outputs)

    def invoke(self, messages):
        return self.outputs.pop(0)


class _Model:
    def __init__(self, outputs):
        self.runnable = _Runnable(outputs)

    def with_structured_output(self, schema, include_raw):
        assert include_raw
        return self.runnable


def _llm(outputs):
    llm = LangChainLLM.__new__(LangChainLLM)
    llm.model, llm.input_price, llm.output_price, llm.max_retries = _Model(outputs), 3.0, 15.0, 1
    return llm


def test_langchain_llm_retries_and_counts_cost():
    good = ReportOutput(summary="s", open_questions=[], proposed_actions=[])
    llm = _llm([{"raw": _Raw(), "parsed": None, "parsing_error": "bad"}, {"raw": _Raw(), "parsed": good}])
    parsed, cost = llm.structured(ReportOutput, "sys", "user")
    assert parsed is good
    assert cost == pytest.approx(2 * (1000 * 3 + 500 * 15) / 1e6)


def test_langchain_llm_gives_up():
    llm = _llm([{"raw": _Raw(), "parsed": None, "parsing_error": "bad"}] * 2)
    with pytest.raises(LLMError):
        llm.structured(ReportOutput, "sys", "user")


def test_jaeger_spans_are_converted():
    trace = {
        "traceID": "t1",
        "processes": {"p1": {"serviceName": "checkout", "tags": [{"key": "k8s.deployment.name", "value": "checkout"}]},
                      "p2": {"serviceName": "payment"}},
        "spans": [
            {"spanID": "a", "operationName": "PlaceOrder", "processID": "p1", "references": [],
             "startTime": 1_758_535_500_000_000, "duration": 300_000, "tags": [{"key": "error", "value": True}]},
            {"spanID": "b", "operationName": "Charge", "processID": "p2",
             "references": [{"refType": "CHILD_OF", "spanID": "a"}],
             "startTime": 1_758_535_500_010_000, "duration": 40_000,
             "tags": [{"key": "otel.status_code", "value": "ERROR"}, {"key": "span.kind", "value": "server"}]},
        ],
    }
    spans = live.JaegerTraces._spans(trace)
    assert spans[0]["parent_id"] is None and spans[0]["error"] and spans[0]["duration_ms"] == 300
    assert spans[0]["resource"] == {"k8s.deployment.name": "checkout"}
    assert spans[1] == {"span_id": "b", "parent_id": "a", "service": "payment", "resource": {},
                        "operation": "Charge", "start": spans[1]["start"], "duration_ms": 40, "error": True,
                        "kind": "server", "peer": None}


def test_timestamps_with_nanoseconds_are_normalised():
    assert live._normalize_ts("2026-09-22T10:05:00.123456789+05:30") == "2026-09-22T04:35:00Z"


def test_helm_changes_filters_by_window(monkeypatch):
    responses = {
        ("helm", "list"): [{"name": "otel-demo"}],
        ("helm", "history"): [
            {"revision": 2, "updated": "2026-09-21T16:00:00.1+00:00", "status": "superseded", "chart": "c", "description": "d"},
            {"revision": 3, "updated": "2026-09-22T10:05:00.123456789+00:00", "status": "deployed", "chart": "c", "description": "d"},
        ],
    }
    def rs(owner, rev, created):
        return {"metadata": {"ownerReferences": [{"kind": "Deployment", "name": owner}],
                             "annotations": {"deployment.kubernetes.io/revision": str(rev)},
                             "creationTimestamp": created},
                "spec": {"template": {"spec": {"containers": []}}}}

    responses[("kubectl", "get")] = {"items": [
        rs("checkout", 4, "2026-09-22T10:05:30Z"),   # Helm's own rollout of revision 3: skipped
        rs("payment", 9, "2026-09-22T10:12:00Z"),    # kubectl edit: a change Helm never saw
    ]}
    monkeypatch.setattr(live, "_run_json", lambda cmd: responses[tuple(cmd[:2])])
    start = datetime(2026, 9, 22, 9, 50, tzinfo=timezone.utc)
    end = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
    out = live.HelmChanges().list("otel-demo", start, end)
    assert [(c["release"], c["revision"]) for c in out] == [("otel-demo", 3), ("deployment/payment", 9)]
    assert out[0]["updated"] == "2026-09-22T10:05:00Z"


def test_red_queries_template_cleanly():
    q = live.RED_QUERIES["error_rate"].replace("{service}", "checkout")
    assert "{service}" not in q and 'service_name="checkout"' in q


def test_unconfigured_logs_fail_as_data(ctx):
    from investigator.tools.backends import Backends
    from investigator.tools.catalog import build_registry
    from investigator.tools.fixture import FixtureMetrics

    reg = build_registry(Backends(FixtureMetrics({}), None, live.UnconfiguredLogs(), None, None, None))
    r = reg.call(ctx, "search_logs", {"service": "checkout"})
    assert not r.ok and "no log backend" in r.summary
