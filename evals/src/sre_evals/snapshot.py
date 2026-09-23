"""Record a live incident as a replayable scenario.

Run the lab's fault, let the incident develop, then snapshot every signal the
agent's tools could ask for over the investigation window. The result is a
scenario file the fixture backends replay exactly; fill in its ground truth
by hand, from what you know you broke.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from investigator.tools.backends import METRICS, Backends
from investigator.tools.base import fmt_ts, parse_ts

GROUND_TRUTH_TEMPLATE = {
    "_fill_in": "Describe what you broke. See evals/README.md for each field.",
    "acceptable_outcomes": ["root_cause_found"],
    "root_cause": {"kind": "change", "release": "", "revision": 0},
    "mechanism_keywords": [],
    "key_evidence": [],
    "acceptable_actions": [],
    "forbidden_actions": [],
}


def snapshot(backends: Backends, incident: dict, change_lookback_hours: int = 24,
             traces_per_query: int = 20) -> dict:
    start, end = parse_ts(incident["window_start"]), parse_ts(incident["window_end"])
    ns = incident["namespace"]
    edges = [list(e) for e in backends.topology.edges()]
    services = sorted({s for e in edges for s in e} | {incident["service"]})

    metrics = {svc: {m: [list(p) for p in backends.metrics.series(svc, m, start, end)] for m in METRICS}
               for svc in services}

    trace_ids: list[str] = []
    for svc in services:
        for errors_only in (True, False):
            for t in backends.traces.search(svc, None, start, end, errors_only, None, traces_per_query):
                if t["trace_id"] not in trace_ids:
                    trace_ids.append(t["trace_id"])
    traces = [{"trace_id": tid, "spans": backends.traces.get(tid)} for tid in trace_ids]

    logs = []
    for svc in services:
        try:
            logs += backends.logs.search(svc, start, end, None, None, 2000)
        except Exception:
            pass  # no log backend: the scenario simply has no logs, like the incident did

    changes = []
    for c in backends.changes.list(ns, start - timedelta(hours=change_lookback_hours), end):
        for rev in (c["revision"] - 1, c["revision"]):
            if rev < 1 or any(x["release"] == c["release"] and x["revision"] == rev for x in changes):
                continue
            try:
                values = backends.changes.values(ns, c["release"], rev)
            except Exception:
                continue  # history no longer holds that revision
            if rev == c["revision"]:
                entry = dict(c)
            else:
                # Kept only so diffs work. Its real time is unknown, so date it
                # before the snapshot's change window, where no listing reaches.
                entry = {**c, "revision": rev, "status": "superseded", "description": "earlier revision (for diffs)",
                         "updated": fmt_ts(start - timedelta(hours=change_lookback_hours, seconds=1))}
            entry["values"] = values
            changes.append(entry)

    workloads = {}
    for svc in services:
        try:
            workloads[svc] = backends.k8s.workload(ns, svc)
        except Exception:
            pass

    return {
        "name": incident.get("alert_id", "incident"),
        "description": f"Recorded {fmt_ts(datetime.now(tz=start.tzinfo))} from the live lab.",
        "incident": incident,
        "ground_truth": dict(GROUND_TRUTH_TEMPLATE),
        "metrics": metrics,
        "traces": traces,
        "logs": logs,
        "changes": changes,
        "events": backends.k8s.events(ns, start, end, None),
        "workloads": workloads,
        "nodes": backends.k8s.nodes(),
        "topology": edges,
    }
