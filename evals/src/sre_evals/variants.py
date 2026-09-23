"""Ten variations of the checkout retry storm, each with ground truth.

Each variant changes one thing about the incident and says what the agent
should conclude. Together they test finding the change, ignoring distractors,
coping with missing signals, knowing when to stop, and not blaming a recent
release for something it didn't cause.

    sre-evals build-scenarios        # regenerates scenarios/*.json
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

T0 = datetime(2026, 9, 22, 9, 50, tzinfo=timezone.utc)
ALERT = datetime(2026, 9, 22, 10, 20, tzinfo=timezone.utc)
END = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
ONSET = datetime(2026, 9, 22, 10, 5, tzinfo=timezone.utc)   # when symptoms begin


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def ramp(before, after, lag_min=2, rise_min=6):
    out, t = [], T0
    while t <= END:
        mins = (t - ONSET).total_seconds() / 60 - lag_min
        frac = 0 if mins <= 0 else min(1, mins / rise_min)
        out.append([ts(t), round(before + (after - before) * frac, 4)])
        t += timedelta(minutes=1)
    return out


def series(v, lag, rise):
    return ramp(*v, lag, rise) if isinstance(v, tuple) else ramp(v, v, lag, rise)


def red(rate, err, p50, p95, p99, lag=2, rise=6):
    return {m: series(v, lag, rise) for m, v in
            zip(("request_rate", "error_rate", "latency_p50", "latency_p95", "latency_p99"), (rate, err, p50, p95, p99))}


from investigator.scenario_kit import failing_payment_checkout, healthy_checkout, manifest, retrying_checkout  # noqa: E402

CHART, APP = "opentelemetry-demo-lab", "lab-1"   # placeholders for the pinned chart and app version


def release(name, revision, updated, status, values, chart=CHART, app=APP):
    return {"release": name, "revision": revision, "updated": updated, "status": status, "chart": chart,
            "app_version": app, "description": "Upgrade complete", "values": values,
            "manifest": manifest(values, chart, app)}


def env_values(timeout: str, retries: str) -> dict:
    return {"components": {"checkout": {
        "imageOverride": {"repository": "asia-south1-docker.pkg.dev/demo/ai-sre-lab/checkout", "tag": "lab-1"},
        "envOverrides": [{"name": "PAYMENT_TIMEOUT", "value": timeout}, {"name": "PAYMENT_MAX_RETRIES", "value": retries}],
    }}}


TOPOLOGY = [
    ["frontend-proxy", "frontend"], ["frontend", "checkout"], ["frontend", "cart"],
    ["frontend", "product-catalog"], ["frontend", "recommendation"], ["frontend", "ad"],
    ["frontend", "currency"], ["checkout", "cart"], ["checkout", "currency"], ["checkout", "payment"],
    ["checkout", "shipping"], ["checkout", "email"], ["checkout", "product-catalog"],
]

RETRY_EVENTS = [
    (5, "SuccessfulRescale", "HorizontalPodAutoscaler/payment", "New size: 4; reason: cpu resource utilization above target", 1),
    (8, "SuccessfulRescale", "HorizontalPodAutoscaler/payment", "New size: 6; reason: cpu resource utilization above target", 1),
    (9, "SuccessfulRescale", "HorizontalPodAutoscaler/checkout", "New size: 4; reason: cpu resource utilization above target", 1),
    (11, "Evicted", "Pod/recommendation-7d9f", "The node was low on resource: memory.", 1),
    (12, "Evicted", "Pod/ad-5c8b", "The node was low on resource: memory.", 1),
    (13, "FailedScheduling", "Pod/payment-6f4d", "0/2 nodes are available: 2 Insufficient memory.", 3),
]

ROLLBACK = {"tool": "rollback_release", "args": {"release": "otel-demo", "to_revision": 2}}


@dataclass
class Variant:
    name: str
    tests: str
    mode: str = "retry"                  # retry | outage
    timeout_ms: int = 40
    retries: int = 5
    lag: int = 2
    rise: int = 6
    fault_release_at: Optional[datetime] = ONSET
    extra_releases: list[dict] = field(default_factory=list)
    traces: bool = True
    logs: bool = True
    red_herring_email: bool = False
    manual_rollout: bool = False         # the change was made with kubectl, outside Helm
    morning_traffic: bool = False        # traffic rises at onset (the latent fault starts to bite)
    ground_truth: dict = field(default_factory=dict)


def build(v: Variant) -> dict:
    attempts = v.retries + 1
    lag, rise = v.lag, v.rise
    if v.mode == "retry":
        fan = 1 + (attempts - 1) * 0.84          # retries that time out, roughly
        fe_rate, co_rate = ((8.0, 20.0), (2.0, 5.0)) if v.morning_traffic else (20.0, 5.0)
        metrics = {
            "frontend": red(fe_rate, (0.002, 0.11), 45, (120, 900), (200, 1400), lag, rise),
            "checkout": red(co_rate, (0.004, 0.36 if attempts > 3 else 0.12), (60, 240), (180, 950), (260, 1300), lag, rise),
            "payment": red((5.0, round(5.0 * fan, 1)), (0.001, 0.02), (20, 70), (48, 190), (70, 320), lag, rise),
        }
    else:
        metrics = {
            "frontend": red(20.0, (0.002, 0.14), 45, 120, 200, lag, rise),
            "checkout": red(5.0, (0.004, 0.5), 60, 180, 260, lag, rise),
            "payment": red(5.0, (0.001, 0.55), 20, 48, 70, lag, rise),
        }
    metrics.update({
        "cart": red(18.0, 0.001, 4, 12, 25),
        "currency": red(40.0, 0.0, 1, 3, 6),
        "shipping": red(5.0, 0.0, 3, 9, 15),
        "email": red(5.0, 0.0, (20, 20), (60, 60), (90, 90)) if not v.red_herring_email else
                 {m: [[p[0], p[1] * (4 if m.startswith("latency") else 1)] for p in s]
                  for m, s in red(5.0, 0.0, 20, 60, 90).items()},
        "product-catalog": red(35.0, 0.0, 2, 8, 14),
    })

    traces = [healthy_checkout(f"ok{i:04d}", T0 + timedelta(minutes=2 * i)) for i in range(7)]
    start = ONSET + timedelta(minutes=lag + 1)
    for i in range(10):
        t = start + timedelta(minutes=2 * i)
        if t > END:
            break
        traces.append(retrying_checkout(f"err{i:04d}", t, attempts, v.timeout_ms) if v.mode == "retry"
                      else failing_payment_checkout(f"err{i:04d}", t))
    if not v.traces:
        traces = []

    logs = []
    t = ONSET + timedelta(minutes=lag)
    while v.logs and t <= END:
        if v.mode == "retry":
            logs += [{"t": ts(t), "service": "checkout", "level": "error",
                      "message": f"failed to charge card: charge failed after {attempts} attempts: "
                                 "rpc error: code = DeadlineExceeded desc = context deadline exceeded"}] * 3
            logs += [{"t": ts(t), "service": "payment", "level": "info", "message": "Charge request received."}] * 8
        else:
            logs += [{"t": ts(t), "service": "checkout", "level": "error",
                      "message": "failed to charge card: rpc error: code = Unavailable desc = payment unavailable"}] * 3
            logs += [{"t": ts(t), "service": "payment", "level": "error",
                      "message": "charge failed: card processor unreachable"}] * 3
        t += timedelta(minutes=1)
    if v.red_herring_email and v.logs:
        logs += [{"t": ts(T0 + timedelta(minutes=k)), "service": "email", "level": "warn",
                  "message": "SMTP relay slow to respond"} for k in range(0, 40, 4)]

    changes = []
    if v.mode == "retry" and v.fault_release_at is not None:
        changes = [
            release("otel-demo", 2, "2026-09-21T16:00:00Z", "superseded", env_values("2s", "0")),
            release("otel-demo", 3, ts(v.fault_release_at), "deployed", env_values(f"{v.timeout_ms}ms", str(v.retries))),
        ]
    changes += v.extra_releases
    rollouts = []
    if v.manual_rollout:
        def template(timeout, retries):
            return {"containers": [{"name": "checkout", "image": "asia-south1-docker.pkg.dev/demo/ai-sre-lab/checkout:lab-1",
                                    "env": [{"name": "PAYMENT_TIMEOUT", "value": timeout},
                                            {"name": "PAYMENT_MAX_RETRIES", "value": retries}]}]}
        rollouts = [{"deployment": "checkout", "revision": 4, "updated": ts(ONSET),
                     "description": "Deployment rollout outside Helm", "template": template(f"{v.timeout_ms}ms", str(v.retries)),
                     "previous": [{"deployment": "checkout", "revision": 3, "template": template("2s", "0")}]}]

    events = []
    if v.mode == "retry" and attempts > 3:
        events = [{"t": ts(ONSET + timedelta(minutes=m + lag - 2)), "reason": r, "object": o, "message": msg, "count": c}
                  for m, r, o, msg, c in RETRY_EVENTS]
    heavy = v.mode == "retry" and attempts > 3
    workloads = {
        "payment": {"name": "payment", "desired": 6 if heavy else 1, "ready": 5 if heavy else 1, "restarts": 0,
                    "hpa": {"min": 1, "max": 6, "current_replicas": 6 if heavy else 1,
                            "current_cpu_percent": 91 if heavy else 22, "target_cpu_percent": 60}},
        "checkout": {"name": "checkout", "desired": 4 if heavy else 1, "ready": 4 if heavy else 1, "restarts": 0,
                     "hpa": {"min": 1, "max": 6, "current_replicas": 4 if heavy else 1,
                             "current_cpu_percent": 72 if heavy else 30, "target_cpu_percent": 60}},
    }
    nodes = [
        {"name": "gke-ai-sre-lab-pool-1", "conditions": {"MemoryPressure": heavy, "DiskPressure": False, "PIDPressure": False}},
        {"name": "gke-ai-sre-lab-pool-2", "conditions": {"MemoryPressure": False, "DiskPressure": False, "PIDPressure": False}},
    ]
    return {
        "name": v.name,
        "description": v.tests,
        "incident": {
            "alert_id": "checkout-error-rate-slo", "service": "checkout", "namespace": "otel-demo",
            "symptom": "checkout error rate above SLO (over 5% for 10 minutes)",
            "alert_started_at": ts(ALERT), "window_start": ts(ALERT - timedelta(minutes=30)), "window_end": ts(END),
        },
        "ground_truth": v.ground_truth,
        "metrics": metrics, "traces": traces, "logs": logs, "changes": changes, "rollouts": rollouts, "events": events,
        "workloads": workloads, "nodes": nodes, "topology": TOPOLOGY,
    }


RELEASE_ROOT = {"kind": "change", "release": "otel-demo", "revision": 3}


def _found(tests, **extra):
    gt = {
        "acceptable_outcomes": ["root_cause_found"],
        "root_cause": RELEASE_ROOT,
        "mechanism_keywords": ["retr"],
        "key_evidence": ["diff_release"],
        "acceptable_actions": [ROLLBACK],
        "forbidden_actions": [],
    }
    gt.update(extra)
    return gt


def variants() -> list[Variant]:
    ad_release_rec = release("ad-banner", 8, "2026-09-22T09:55:00Z", "deployed",
                             {"components": {"ad": {"imageOverride": {"repository": "ghcr.io/example/ad", "tag": "2026.09.22"}}}},
                             chart="ad-banner-lab", app="2026.09.22")
    ad_release_prev_rec = release("ad-banner", 7, "2026-09-19T12:00:00Z", "superseded",
                                  {"components": {"ad": {"imageOverride": {"repository": "ghcr.io/example/ad", "tag": "2026.09.19"}}}},
                                  chart="ad-banner-lab", app="2026.09.19")
    banner_rec = release("otel-demo", 3, "2026-09-22T10:02:00Z", "deployed",
                         {"components": {"frontend": {"envOverrides": [{"name": "BANNER_TEXT", "value": "Autumn sale"}]}}})
    banner_prev_rec = release("otel-demo", 2, "2026-09-21T16:00:00Z", "superseded",
                              {"components": {"frontend": {"envOverrides": [{"name": "BANNER_TEXT", "value": ""}]}}})
    return [
        Variant("baseline", "The recorded case study: release at 10:05, clear signals.", ground_truth=_found("baseline")),
        Variant("release-hours-earlier", "The bad release landed at 06:00 and only bit when morning traffic arrived "
                "at 10:05; finding it needs the widened change search.",
                fault_release_at=datetime(2026, 9, 22, 6, 0, tzinfo=timezone.utc), morning_traffic=True,
                ground_truth=_found("widening")),
        Variant("unrelated-release-too", "A second, unrelated release (ad-banner) landed ten minutes before the bad one.",
                extra_releases=[ad_release_prev_rec, ad_release_rec],
                ground_truth=_found("distractor", forbidden_actions=[{"tool": "rollback_release", "args": {"release": "ad-banner"}}])),
        Variant("red-herring-dependency", "Email, another checkout dependency, has been slow all morning for unrelated reasons.",
                red_herring_email=True, ground_truth=_found("red herring")),
        Variant("no-traces", "Traces are unavailable; the agent must work from metrics, logs and the release diff.",
                traces=False, ground_truth=_found("missing traces", key_evidence=["diff_release"])),
        Variant("no-logs", "Logs are unavailable.", logs=False, ground_truth=_found("missing logs")),
        Variant("mild-retries", "A gentler misconfiguration: 45 ms timeout, just under payment's p95, and two retries. "
                "Weaker signals, no cluster pressure.", timeout_ms=45, retries=2, ground_truth=_found("weak signal")),
        Variant("slow-burn", "Errors creep up over fifteen minutes instead of jumping.", lag=6, rise=15,
                ground_truth=_found("slow onset")),
        Variant("manual-change", "Someone changed checkout with kubectl edit: no Helm release to find, but the "
                "Deployment's rollout history records it. Rolling back a Helm release would be wrong.",
                fault_release_at=None, manual_rollout=True,
                ground_truth={
                    "acceptable_outcomes": ["root_cause_found", "escalated"],
                    "root_cause": {"kind": "change", "release": "deployment/checkout", "revision": 4},
                    "mechanism_keywords": ["retr"], "key_evidence": ["diff_release"],
                    "acceptable_actions": [], "forbidden_actions": [{"tool": "rollback_release"}],
                }),
        Variant("payment-outage", "Payment really is failing, and an unrelated release (a frontend banner) landed three "
                "minutes before. Rolling it back would do nothing.", mode="outage", fault_release_at=None,
                extra_releases=[banner_prev_rec, banner_rec],
                ground_truth={
                    "acceptable_outcomes": ["escalated", "root_cause_found"],
                    "root_cause": {"kind": "component", "component": "payment", "keywords": ["payment"]},
                    "mechanism_keywords": [], "key_evidence": [],
                    "acceptable_actions": [], "forbidden_actions": [{"tool": "rollback_release"}],
                }),
    ]


def write_all(directory: Path) -> list[Path]:
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for v in variants():
        p = directory / f"{v.name}.json"
        p.write_text(json.dumps(build(v), indent=1))
        paths.append(p)
    return paths
