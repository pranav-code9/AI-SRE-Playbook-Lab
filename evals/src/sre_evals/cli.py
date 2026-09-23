"""Evaluation harness for the investigation agent (Chapter 6).

    sre-evals build-scenarios                              regenerate the ten variants
    sre-evals run --scripted --out results.json            free: scripted scenarios only (CI)
    sre-evals run --model provider:model --trials 3 --out results.json
    sre-evals compare results.json baselines/scripted.json fail on regression
    sre-evals snapshot --service checkout --alert-start ... --out scenarios/my-incident.json   (live lab)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _parse(argv):
    p = argparse.ArgumentParser(prog="sre-evals", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("build-scenarios")

    r = sub.add_parser("run")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--scripted", action="store_true", help="scripted responses from scripts/ (no model, no cost)")
    src.add_argument("--model", help="provider:model for init_chat_model")
    r.add_argument("--price-in", type=float, default=0.0)
    r.add_argument("--price-out", type=float, default=0.0)
    r.add_argument("--trials", type=int, default=1)
    r.add_argument("--scenarios", nargs="*", help="scenario names (default: all)")
    r.add_argument("--no-structural-rules", action="store_true", help="evaluate the Chapter 3 baseline")
    r.add_argument("--out", default="results.json")
    r.add_argument("--report", default="report.md")
    r.add_argument("--baseline", help="also compare with this baseline and fail on regression")

    c = sub.add_parser("compare")
    c.add_argument("results")
    c.add_argument("baseline")
    c.add_argument("--tolerance", type=float, default=0.05)

    s = sub.add_parser("snapshot")
    s.add_argument("--service", required=True)
    s.add_argument("--namespace", default="otel-demo")
    s.add_argument("--symptom", required=True)
    s.add_argument("--alert-start", required=True, help="ISO time the alert fired")
    s.add_argument("--prometheus-url", default="http://localhost:9090")
    s.add_argument("--jaeger-url", default="http://localhost:8080/jaeger/ui")
    s.add_argument("--out", required=True)
    return p.parse_args(argv)


def _scripted(name):
    from investigator.llm import ScriptedLLM

    path = ROOT / "scripts" / f"{name}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    data.pop("_about", None)
    return ScriptedLLM(data)


def main(argv=None) -> int:
    a = _parse(argv if argv is not None else sys.argv[1:])
    if a.cmd == "build-scenarios":
        from sre_evals.variants import write_all

        for path in write_all(ROOT / "scenarios"):
            print(path)
        return 0

    if a.cmd == "compare":
        from sre_evals.run import compare

        problems = compare(json.loads(Path(a.results).read_text()), json.loads(Path(a.baseline).read_text()), a.tolerance)
        print("no regression" if not problems else "REGRESSION:\n  " + "\n  ".join(problems))
        return 1 if problems else 0

    if a.cmd == "snapshot":
        from investigator.tools.base import fmt_ts
        from investigator.tools.live import live_backends
        from sre_evals.snapshot import snapshot

        alert = datetime.fromisoformat(a.alert_start.replace("Z", "+00:00"))
        incident = {
            "alert_id": f"{a.service}-{alert:%Y%m%d%H%M}", "service": a.service, "namespace": a.namespace,
            "symptom": a.symptom, "alert_started_at": fmt_ts(alert),
            "window_start": fmt_ts(alert - timedelta(minutes=30)), "window_end": fmt_ts(datetime.now(tz=timezone.utc)),
        }
        scenario = snapshot(live_backends(a.prometheus_url, a.jaeger_url), incident)
        Path(a.out).write_text(json.dumps(scenario, indent=1))
        print(f"wrote {a.out}; fill in its ground_truth before using it")
        return 0

    from sre_evals.run import compare, eligible_rung, report, run_suite

    paths = sorted((ROOT / "scenarios").glob("*.json"))
    if a.scenarios:
        paths = [p for p in paths if p.stem in a.scenarios]
    if a.scripted:
        llm_for = _scripted
        title = "Scripted evaluation"
    else:
        from investigator.llm import LangChainLLM

        model = LangChainLLM(a.model, input_usd_per_mtok=a.price_in, output_usd_per_mtok=a.price_out)
        llm_for = lambda name: model  # noqa: E731
        title = f"Evaluation: {a.model}"
    if a.no_structural_rules:
        title += " (baseline, no structural rules)"

    summary = run_suite(paths, llm_for, a.trials, structural_rules=not a.no_structural_rules,
                        progress=lambda m: print(m, file=sys.stderr))
    thresholds = yaml.safe_load((ROOT / "thresholds.yaml").read_text())
    summary["eligible_rung"] = eligible_rung(summary, thresholds)
    Path(a.out).write_text(json.dumps(summary, indent=1))
    Path(a.report).write_text(report(summary, title, thresholds))
    print(report(summary, title, thresholds))
    if a.baseline:
        problems = compare(summary, json.loads(Path(a.baseline).read_text()))
        print("no regression" if not problems else "REGRESSION:\n  " + "\n  ".join(problems))
        return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
