"""The month-later recurrence, end to end, offline:

    python -m sre_rollout.demo examples/

Investigates the recorded recurrence with scripted model responses, routes the
rollback through the trust ladder, approves and executes it, verifies it,
then writes the postmortem draft, shadow report and impact report.
"""

from __future__ import annotations

import json
import sys
from datetime import timedelta
from pathlib import Path

import yaml

from investigator.graph import Deps, investigate
from investigator.llm import ScriptedLLM
from investigator.state import IncidentContext
from investigator.tools.base import InMemoryRawStore, ToolContext, parse_ts
from investigator.tools.catalog import build_registry
from investigator.tools.fixture import fixture_backends
from sre_evals.run import dump_state
from sre_mcp import flows
from sre_mcp.actions import RecordingActions
from sre_policy import Gate, load_policy
from sre_rollout.impact import approval_stats, render_impact
from sre_rollout.postmortem import draft
from sre_rollout.shadow import ShadowLog, render_shadow_report, shadow_report

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT.parent / "policies" / "trust-ladder.yaml"
GROUND_TRUTH = {
    "acceptable_outcomes": ["root_cause_found"],
    "root_cause": {"kind": "change", "release": "otel-demo", "revision": 7},
    "mechanism_keywords": ["retr"], "key_evidence": ["diff_release"],
    "acceptable_actions": [{"tool": "rollback_release", "args": {"release": "otel-demo", "to_revision": 6}}],
    "forbidden_actions": [],
}


def run(out_dir: Path, approve_after_min: int = 2, agent_min: int = 4, human_min: int = 18) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    scenario = json.loads((ROOT / "scenarios" / "checkout_retry_storm_recurrence.json").read_text())
    script = json.loads((ROOT / "scenarios" / "checkout_retry_storm_recurrence.script.json").read_text())
    end = parse_ts(scenario["incident"]["window_end"])
    now = {"t": end}
    state_dir = out_dir / "state"
    for f in ("audit.jsonl", "approvals.json", "trust-state.json"):
        (state_dir / f).unlink(missing_ok=True)
    gate = Gate(load_policy(POLICY), state_dir, clock=lambda: now["t"])
    backends = fixture_backends(scenario)
    registry = build_registry(backends)
    store = InMemoryRawStore()
    i = scenario["incident"]

    def context():
        return ToolContext(i["namespace"], parse_ts(i["window_start"]), end, raw_store=store)

    actions = RecordingActions(scenario)
    log: list[str] = []

    final = investigate(Deps(registry, ScriptedLLM.from_file_data(script)), IncidentContext(**i))
    state = dump_state(final)
    state_path = out_dir / "state.json"
    state_path.write_text(json.dumps(state, indent=1, default=str))

    flows.propose(state_path, gate, actions, registry, context, None, now["t"], out=log.append)
    [approval] = gate.pending()
    now["t"] += timedelta(minutes=approve_after_min)
    gate.decide_approval(approval.id, "slack:U000EXAMPLE", approve=True)
    flows.execute(approval.id, gate, actions, registry, context, "cli:pranav", out=log.append)
    now["t"] += timedelta(minutes=11)
    execution = next(e["data"]["execution_id"] for e in gate.audit.entries() if e["event"] == "executed")
    flows.verify(execution, gate, backends, out=log.append)
    audit = list(gate.audit.entries())

    roles = yaml.safe_load(POLICY.read_text())["roles"]
    (out_dir / "postmortem-draft.md").write_text(draft(state, audit, roles, "Checkout retry storm, again"))

    shadow = ShadowLog(out_dir / "shadow.jsonl")
    shadow.path.write_text("")
    alert = parse_ts(i["alert_started_at"])
    shadow.record_agent(state, (alert + timedelta(minutes=agent_min)).isoformat())
    shadow.record_human(i["alert_id"], GROUND_TRUTH, (alert + timedelta(minutes=human_min)).isoformat())
    s = shadow_report(shadow)
    (out_dir / "shadow-report.md").write_text(render_shadow_report(s))
    (out_dir / "impact.md").write_text(render_impact(s, approval_stats(audit)))
    return {"log": log, "state": state, "audit": audit, "shadow": s}


if __name__ == "__main__":
    result = run(Path(sys.argv[1] if len(sys.argv) > 1 else "examples"))
    print("\n".join(result["log"]))
