"""Run an investigation from the command line.

Offline, no model (replays the scripted case study):
    investigate --scenario scenarios/checkout_retry_storm.json --scripted scenarios/checkout_retry_storm.script.json

Offline, with a model:
    investigate --scenario scenarios/checkout_retry_storm.json --model <provider:model>

Against the lab (see README.md before doing this):
    investigate --live --service checkout --namespace otel-demo \\
        --symptom "checkout error rate above SLO" --model <provider:model>
"""

from __future__ import annotations

import argparse
import os
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from investigator.graph import Deps, investigate
from investigator.llm import LangChainLLM, ScriptedLLM
from investigator.render import render_evidence, render_hypotheses
from investigator.state import Budget, IncidentContext
from investigator.tools.base import DirRawStore, fmt_ts
from investigator.tools.catalog import build_registry


def _parse(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="investigate", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = p.add_mutually_exclusive_group(required=True)
    src.add_argument("--scenario", help="recorded scenario JSON (offline)")
    src.add_argument("--live", action="store_true", help="query the running lab")

    llm = p.add_mutually_exclusive_group(required=True)
    llm.add_argument("--model", help="model for init_chat_model, e.g. provider:model-name")
    llm.add_argument("--scripted", help="scripted responses JSON (no model)")
    p.add_argument("--price-in", type=float, default=0.0, help="USD per million input tokens, for the budget")
    p.add_argument("--price-out", type=float, default=0.0, help="USD per million output tokens, for the budget")

    p.add_argument("--service", help="alerted service (live)")
    p.add_argument("--namespace", default="otel-demo")
    p.add_argument("--symptom", help="alert description (live)")
    p.add_argument("--alert-id", default="manual")
    p.add_argument("--alert-start", help="ISO 8601; default now (live)")
    p.add_argument("--prometheus-url", default="http://localhost:9090")
    p.add_argument("--jaeger-url", default="http://localhost:8080/jaeger/ui")
    p.add_argument("--changes-url", help="read change history from sre-change-exporter instead of helm directly (live)")
    p.add_argument("--neo4j-uri", help="use the Chapter 2 topology graph instead of Jaeger's dependency view (live)")
    p.add_argument("--neo4j-user", default="neo4j")
    p.add_argument("--neo4j-password", default=os.environ.get("NEO4J_PASSWORD"), help="defaults to $NEO4J_PASSWORD")

    p.add_argument("--no-structural-rules", action="store_true",
                   help="Chapter 3 baseline: no change sweep, confidence rules or why-chain requirement")
    p.add_argument("--no-guards", action="store_true", help="Chapter 7 demonstration: disable the loop guards")
    p.add_argument("--otlp-endpoint", help="send the agent's own traces and metrics here, e.g. localhost:4317")
    p.add_argument("--max-tokens", type=int, default=300_000)
    p.add_argument("--max-iterations", type=int, default=8)
    p.add_argument("--max-tool-calls", type=int, default=30)
    p.add_argument("--max-cost", type=float, default=1.0)
    p.add_argument("--out", default="runs", help="directory for the state dump and raw tool results")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = _parse(argv if argv is not None else sys.argv[1:])
    logging.basicConfig(level=logging.INFO if a.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    if a.scenario:
        from investigator.tools.fixture import fixture_backends, load_scenario

        scenario = load_scenario(a.scenario)
        backends = fixture_backends(scenario)
        incident = IncidentContext(**scenario["incident"])
    else:
        from investigator.tools.live import live_backends

        if not (a.service and a.symptom):
            print("--live needs --service and --symptom", file=sys.stderr)
            return 2
        backends = live_backends(a.prometheus_url, a.jaeger_url)
        if a.changes_url:
            from investigator.change_exporter import HttpChanges

            backends.changes = HttpChanges(a.changes_url)
        if a.neo4j_uri:
            from neo4j import GraphDatabase

            from investigator.topology import Neo4jTopology

            driver = GraphDatabase.driver(a.neo4j_uri, auth=(a.neo4j_user, a.neo4j_password))
            backends.topology = Neo4jTopology(driver)
        now = datetime.now(tz=timezone.utc)
        alert = datetime.fromisoformat(a.alert_start.replace("Z", "+00:00")) if a.alert_start else now
        incident = IncidentContext(
            alert_id=a.alert_id, service=a.service, namespace=a.namespace, symptom=a.symptom,
            alert_started_at=fmt_ts(alert),
            window_start=fmt_ts(alert - timedelta(minutes=30)),
            window_end=fmt_ts(now),
        )

    if a.scripted:
        model = ScriptedLLM.from_file_data(json.loads(Path(a.scripted).read_text()))
    else:
        model = LangChainLLM(a.model, input_usd_per_mtok=a.price_in, output_usd_per_mtok=a.price_out)

    run_dir = Path(a.out) / datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    telemetry = None
    if a.otlp_endpoint:
        from investigator.telemetry import otlp_telemetry

        telemetry = otlp_telemetry(a.otlp_endpoint)
    deps = Deps(
        registry=build_registry(backends), llm=model, raw_store=DirRawStore(run_dir / "raw"),
        structural_rules=not a.no_structural_rules, guards=not a.no_guards,
    )
    if telemetry:
        deps.telemetry = telemetry
    budget = Budget(max_iterations=a.max_iterations, max_tool_calls=a.max_tool_calls, max_cost_usd=a.max_cost,
                    max_tokens=a.max_tokens)

    final = investigate(deps, incident, budget)
    if telemetry:
        from investigator.telemetry import shutdown

        shutdown()

    c = final["conclusion"]
    b = final["budget"]
    print(f"\n=== {c.outcome.replace('_', ' ').upper()} ===")
    if c.escalation_reason:
        print(f"Reason: {c.escalation_reason}")
    if c.causal_chain:
        print(f"Causal chain: {' -> '.join(c.causal_chain)} (confidence {c.confidence:.2f})")
    print(f"\n{c.summary}\n")
    for title, items in (("Open questions", c.open_questions), ("Proposed actions (not executed)", c.proposed_actions)):
        if items:
            print(title + ":")
            for item in items:
                print(f"  - {item}")
    if c.action_proposals:
        print("Action proposals (for the trust ladder):")
        for ap in c.action_proposals:
            print(f"  - {ap.tool}({json.dumps(ap.args)}): {ap.reason}")
    print("\nHypotheses:\n" + render_hypotheses(final["hypotheses"]))
    print("\nEvidence:\n" + render_evidence(final["evidence"]))
    print(f"\nIterations {b.iterations}, tool calls {b.tool_calls}, tokens {b.tokens}, cost ${b.cost_usd:.4f}")
    for trip in b.guard_trips:
        print(f"Guard: {trip}")

    run_dir.mkdir(parents=True, exist_ok=True)
    dump = {k: (v.model_dump() if hasattr(v, "model_dump") else [x.model_dump() for x in v] if isinstance(v, list) else v)
            for k, v in final.items()}
    (run_dir / "state.json").write_text(json.dumps(dump, indent=2, default=str))
    print(f"State and raw tool results: {run_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
