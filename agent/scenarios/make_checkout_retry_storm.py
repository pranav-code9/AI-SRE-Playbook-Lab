"""Generates checkout_retry_storm.json, the recorded version of the book's
running incident. The numbers are synthetic but shaped like the lab: a Helm
release at 10:05 shortens checkout's payment timeout and enables five
immediate retries.

Replace this with data captured from a real lab run once the lab works.

    python scenarios/make_checkout_retry_storm.py
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

T0 = datetime(2026, 9, 22, 9, 50, tzinfo=timezone.utc)
FAULT = datetime(2026, 9, 22, 10, 5, tzinfo=timezone.utc)
ALERT = datetime(2026, 9, 22, 10, 20, tzinfo=timezone.utc)
END = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)


def ts(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ramp(before, after, lag_min=2, rise_min=6):
    """Per-minute values: flat, then a ramp starting lag_min after the fault."""
    out, t = [], T0
    while t <= END:
        mins = (t - FAULT).total_seconds() / 60 - lag_min
        frac = 0 if mins <= 0 else min(1, mins / rise_min)
        out.append([ts(t), round(before + (after - before) * frac, 4)])
        t += timedelta(minutes=1)
    return out


def red(rate, err, p50, p95, p99):
    return {
        "request_rate": ramp(*rate) if isinstance(rate, tuple) else ramp(rate, rate),
        "error_rate": ramp(*err) if isinstance(err, tuple) else ramp(err, err),
        "latency_p50": ramp(*p50) if isinstance(p50, tuple) else ramp(p50, p50),
        "latency_p95": ramp(*p95) if isinstance(p95, tuple) else ramp(p95, p95),
        "latency_p99": ramp(*p99) if isinstance(p99, tuple) else ramp(p99, p99),
    }


metrics = {
    "frontend": red(20.0, (0.002, 0.11), 45, (120, 900), (200, 1400)),
    "checkout": red(5.0, (0.004, 0.36), (60, 240), (180, 950), (260, 1300)),
    # Baseline p95 of 48 ms sits above the faulty 40 ms timeout, so a share of
    # ordinary calls time out as soon as the release lands (Appendix A).
    "payment": red((5.0, 27.0), (0.001, 0.02), (20, 70), (48, 190), (70, 320)),
    "cart": red(18.0, 0.001, 4, 12, 25),
    "currency": red(40.0, 0.0, 1, 3, 6),
    "shipping": red(5.0, 0.0, 3, 9, 15),
    "email": red((5.0, 3.2), 0.0, 20, 60, 90),
    "product-catalog": red(35.0, 0.0, 2, 8, 14),
}


import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from investigator.scenario_kit import healthy_checkout, manifest, retrying_checkout  # noqa: E402

traces = [healthy_checkout(f"ok{i:04d}", T0 + timedelta(minutes=2 * i)) for i in range(7)]
traces += [retrying_checkout(f"err{i:04d}", FAULT + timedelta(minutes=3 + 2 * i), attempts=6, timeout_ms=40) for i in range(10)]

logs = []
t = FAULT + timedelta(minutes=2)
while t <= END:
    for _ in range(3):
        logs.append({"t": ts(t), "service": "checkout", "level": "error",
                     "message": "failed to charge card: charge failed after 6 attempts: "
                                "rpc error: code = DeadlineExceeded desc = context deadline exceeded"})
    for _ in range(8):
        logs.append({"t": ts(t), "service": "payment", "level": "info", "message": "Charge request received."})
    t += timedelta(minutes=1)

CHART, APP = "opentelemetry-demo-lab", "lab-1"   # placeholders for the pinned chart and app version
values_rev2 = {"components": {"checkout": {
    "imageOverride": {"repository": "asia-south1-docker.pkg.dev/demo/ai-sre-lab/checkout", "tag": "lab-1"},
    "envOverrides": [{"name": "PAYMENT_TIMEOUT", "value": "2s"}, {"name": "PAYMENT_MAX_RETRIES", "value": "0"}],
}}}
values_rev3 = json.loads(json.dumps(values_rev2))
values_rev3["components"]["checkout"]["envOverrides"] = [
    {"name": "PAYMENT_TIMEOUT", "value": "40ms"}, {"name": "PAYMENT_MAX_RETRIES", "value": "5"}]

scenario = {
    "name": "checkout-retry-storm",
    "description": "Helm release shortens checkout's payment timeout and enables immediate retries.",
    "incident": {
        "alert_id": "checkout-error-rate-slo",
        "service": "checkout",
        "namespace": "otel-demo",
        "symptom": "checkout error rate above SLO (over 5% for 10 minutes)",
        "alert_started_at": ts(ALERT),
        "window_start": ts(ALERT - timedelta(minutes=30)),
        "window_end": ts(END),
    },
    "ground_truth": {
        "root_cause": "Helm release otel-demo revision 3 shortened checkout's PAYMENT_TIMEOUT to 40ms and set PAYMENT_MAX_RETRIES to 5",
        "change": {"release": "otel-demo", "revision": 3},
        "causal_chain": ["release changes timeout and retries", "retry storm to payment",
                         "payment latency rises", "checkout errors"],
        "proposed_action": "roll back otel-demo to revision 2",
    },
    "metrics": metrics,
    "traces": traces,
    "logs": logs,
    "changes": [
        {"release": "otel-demo", "revision": 2, "updated": "2026-09-21T16:00:00Z", "status": "superseded",
         "chart": CHART, "app_version": APP, "description": "Upgrade complete", "values": values_rev2,
         "manifest": manifest(values_rev2, CHART, APP)},
        {"release": "otel-demo", "revision": 3, "updated": ts(FAULT), "status": "deployed",
         "chart": CHART, "app_version": APP, "description": "Upgrade complete", "values": values_rev3,
         "manifest": manifest(values_rev3, CHART, APP)},
    ],
    "events": [
        {"t": ts(FAULT + timedelta(minutes=5)), "reason": "SuccessfulRescale", "object": "HorizontalPodAutoscaler/payment",
         "message": "New size: 4; reason: cpu resource utilization above target", "count": 1},
        {"t": ts(FAULT + timedelta(minutes=8)), "reason": "SuccessfulRescale", "object": "HorizontalPodAutoscaler/payment",
         "message": "New size: 6; reason: cpu resource utilization above target", "count": 1},
        {"t": ts(FAULT + timedelta(minutes=9)), "reason": "SuccessfulRescale", "object": "HorizontalPodAutoscaler/checkout",
         "message": "New size: 4; reason: cpu resource utilization above target", "count": 1},
        {"t": ts(FAULT + timedelta(minutes=11)), "reason": "Evicted", "object": "Pod/recommendation-7d9f",
         "message": "The node was low on resource: memory.", "count": 1},
        {"t": ts(FAULT + timedelta(minutes=12)), "reason": "Evicted", "object": "Pod/ad-5c8b",
         "message": "The node was low on resource: memory.", "count": 1},
        {"t": ts(FAULT + timedelta(minutes=13)), "reason": "FailedScheduling", "object": "Pod/payment-6f4d",
         "message": "0/2 nodes are available: 2 Insufficient memory.", "count": 3},
    ],
    "workloads": {
        "payment": {"name": "payment", "desired": 6, "ready": 5, "restarts": 0,
                    "hpa": {"min": 1, "max": 6, "current_replicas": 6, "current_cpu_percent": 91, "target_cpu_percent": 60}},
        "checkout": {"name": "checkout", "desired": 4, "ready": 4, "restarts": 0,
                     "hpa": {"min": 1, "max": 6, "current_replicas": 4, "current_cpu_percent": 72, "target_cpu_percent": 60}},
    },
    "nodes": [
        {"name": "gke-ai-sre-lab-pool-1", "conditions": {"MemoryPressure": True, "DiskPressure": False, "PIDPressure": False}},
        {"name": "gke-ai-sre-lab-pool-2", "conditions": {"MemoryPressure": False, "DiskPressure": False, "PIDPressure": False}},
    ],
    "topology": [
        ["frontend-proxy", "frontend"], ["frontend", "checkout"], ["frontend", "cart"],
        ["frontend", "product-catalog"], ["frontend", "recommendation"], ["frontend", "ad"],
        ["frontend", "currency"], ["checkout", "cart"], ["checkout", "currency"], ["checkout", "payment"],
        ["checkout", "shipping"], ["checkout", "email"], ["checkout", "product-catalog"],
    ],
}

out = Path(__file__).with_name("checkout_retry_storm.json")
out.write_text(json.dumps(scenario, indent=1))
print(f"wrote {out}")
