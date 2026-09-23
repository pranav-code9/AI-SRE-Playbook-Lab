"""Rollout tools (Chapter 8).

    sre-rollout recurrence                                     write the month-later scenario and script
    sre-rollout shadow record-agent <state.json> --reported-at ISO
    sre-rollout shadow record-human <alert id> --ground-truth gt.json --diagnosed-at ISO
    sre-rollout shadow report
    sre-rollout postmortem <state.json> --audit <state dir>/audit.jsonl --title "..." --out postmortem.md
    sre-rollout impact --audit <state dir>/audit.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
AGENT = ROOT.parent / "agent"
POLICY = ROOT.parent / "policies" / "trust-ladder.yaml"


def _audit(path) -> list[dict]:
    p = Path(path)
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()] if p.exists() else []


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sre-rollout", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--shadow-log", default="shadow.jsonl")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("recurrence")
    sh = sub.add_parser("shadow")
    shs = sh.add_subparsers(dest="shadow_cmd", required=True)
    ra = shs.add_parser("record-agent")
    ra.add_argument("state")
    ra.add_argument("--reported-at", required=True)
    rh = shs.add_parser("record-human")
    rh.add_argument("alert_id")
    rh.add_argument("--ground-truth", required=True, help="JSON file in the Chapter 6 ground-truth format")
    rh.add_argument("--diagnosed-at", required=True)
    rh.add_argument("--notes", default="")
    shs.add_parser("report")
    pm = sub.add_parser("postmortem")
    pm.add_argument("state")
    pm.add_argument("--audit", required=True)
    pm.add_argument("--policy", default=str(POLICY))
    pm.add_argument("--title", required=True)
    pm.add_argument("--out", default="postmortem.md")
    im = sub.add_parser("impact")
    im.add_argument("--audit", required=True)
    a = p.parse_args(argv if argv is not None else sys.argv[1:])

    if a.cmd == "recurrence":
        from sre_rollout.recurrence import recurrence_scenario, recurrence_script

        base = json.loads((AGENT / "scenarios" / "checkout_retry_storm.json").read_text())
        script = json.loads((AGENT / "scenarios" / "checkout_retry_storm.script.json").read_text())
        out = ROOT / "scenarios"
        out.mkdir(exist_ok=True)
        (out / "checkout_retry_storm_recurrence.json").write_text(json.dumps(recurrence_scenario(base), indent=1))
        s = recurrence_script(script)
        s["_about"] = "The Chapter 3 script with revision numbers and dates moved to the recurrence, a month later."
        (out / "checkout_retry_storm_recurrence.script.json").write_text(json.dumps(s, indent=1))
        print(f"wrote {out}/checkout_retry_storm_recurrence.json and .script.json")
        return 0

    from sre_rollout.shadow import ShadowLog, render_shadow_report, shadow_report

    log = ShadowLog(a.shadow_log)
    if a.cmd == "shadow":
        if a.shadow_cmd == "record-agent":
            log.record_agent(json.loads(Path(a.state).read_text()), a.reported_at)
        elif a.shadow_cmd == "record-human":
            log.record_human(a.alert_id, json.loads(Path(a.ground_truth).read_text()), a.diagnosed_at, a.notes)
        else:
            print(render_shadow_report(shadow_report(log)))
        return 0

    if a.cmd == "postmortem":
        from sre_rollout.postmortem import draft

        roles = yaml.safe_load(Path(a.policy).read_text()).get("roles", {})
        text = draft(json.loads(Path(a.state).read_text()), _audit(a.audit), roles, a.title)
        Path(a.out).write_text(text)
        print(f"wrote {a.out}")
        return 0

    from sre_rollout.impact import approval_stats, render_impact

    print(render_impact(shadow_report(log), approval_stats(_audit(a.audit))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
