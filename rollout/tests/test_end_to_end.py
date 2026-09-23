"""The whole book in one test: a month after the case study, the retry storm
comes back. The agent investigates, the trust ladder routes its rollback for
approval, a human approves, the rollback runs and is verified, the postmortem
draft is assembled, and shadow-mode grading and impact measures read the
records."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
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
from sre_rollout.recurrence import recurrence_scenario, recurrence_script
from sre_rollout.shadow import ShadowLog, render_shadow_report, shadow_report

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT.parent / "agent" / "scenarios"
POLICY = ROOT.parent / "policies" / "trust-ladder.yaml"

HUMAN_GROUND_TRUTH = {
    "acceptable_outcomes": ["root_cause_found"],
    "root_cause": {"kind": "change", "release": "otel-demo", "revision": 7},
    "mechanism_keywords": ["retr"],
    "key_evidence": ["diff_release"],
    "acceptable_actions": [{"tool": "rollback_release", "args": {"release": "otel-demo", "to_revision": 6}}],
    "forbidden_actions": [],
}


@pytest.fixture
def world(tmp_path):
    scenario = recurrence_scenario(json.loads((AGENT / "checkout_retry_storm.json").read_text()))
    script = recurrence_script(json.loads((AGENT / "checkout_retry_storm.script.json").read_text()))
    end = parse_ts(scenario["incident"]["window_end"])
    now = {"t": end}
    gate = Gate(load_policy(POLICY), tmp_path / "state", clock=lambda: now["t"])
    backends = fixture_backends(scenario)
    registry = build_registry(backends)
    store = InMemoryRawStore()
    i = scenario["incident"]
    context = lambda: ToolContext(i["namespace"], parse_ts(i["window_start"]), end, raw_store=store)  # noqa: E731
    return dict(scenario=scenario, script=script, gate=gate, now=now, backends=backends, registry=registry,
                context=context, actions=RecordingActions(scenario), tmp=tmp_path)


def test_the_recurrence_end_to_end(world):
    w = world
    # 1. The agent investigates.
    final = investigate(Deps(w["registry"], ScriptedLLM.from_file_data(w["script"])),
                        IncidentContext(**w["scenario"]["incident"]))
    state = dump_state(final)
    c = state["conclusion"]
    assert c["outcome"] == "root_cause_found" and c["causal_chain"] == ["H3", "H2", "H1", "H0"]
    assert c["action_proposals"][0]["args"] == {"release": "otel-demo", "to_revision": 6}
    state_path = w["tmp"] / "state.json"
    state_path.write_text(json.dumps(state, default=str))

    # 2. The trust ladder routes the proposal for approval.
    lines = []
    flows.propose(state_path, w["gate"], w["actions"], w["registry"], w["context"], None, w["now"]["t"], out=lines.append)
    assert "needs_approval at rung approve" in lines[0]
    [approval] = w["gate"].pending()
    assert approval.plan["current_revision"] == 7 and approval.plan["target_revision"] == 6

    # 3. The incident commander approves from Slack two minutes later; the on-call engineer runs it.
    w["now"]["t"] += timedelta(minutes=2)
    w["gate"].decide_approval(approval.id, "slack:U000EXAMPLE", approve=True)
    assert flows.execute(approval.id, w["gate"], w["actions"], w["registry"], w["context"], "cli:pranav",
                         out=lines.append) == 0
    assert w["actions"].performed == [("otel-demo", "otel-demo", 6)]

    # 4. Ten minutes later, verification finds errors have fallen.
    w["now"]["t"] += timedelta(minutes=11)
    execution = next(e["data"]["execution_id"] for e in w["gate"].audit.entries() if e["event"] == "executed")
    assert flows.verify(execution, w["gate"], w["backends"], out=lines.append) == 0
    assert w["gate"].effective_rung("rollback_release").label == "approve"
    audit = list(w["gate"].audit.entries())
    assert w["gate"].audit.verify() == []

    # 5. The postmortem draft: facts from the records, roles instead of names.
    roles = yaml.safe_load(POLICY.read_text())["roles"]
    pm = draft(state, audit, roles, "Checkout retry storm, again")
    assert "pranav" not in pm and "U000EXAMPLE" not in pm
    assert "the incident commander approved the plan" in pm.lower()
    assert "revision 7" in pm and "Outcome of the action recorded as good" in pm
    assert "Approval took 2 minute(s)" in pm
    assert pm.count("TO WRITE") >= 5
    order = [pm.index(s) for s in ("otel-demo revision 7 deployed", "Alert `", "requested approval", "approved the plan",
                                   "rollback_release executed", "recorded as good")]
    assert order == sorted(order)

    # 6. Shadow-mode grading against what the responders concluded.
    log = ShadowLog(w["tmp"] / "shadow.jsonl")
    alert = state["incident"]["alert_started_at"]
    log.record_agent(state, reported_at=(parse_ts(alert) + timedelta(minutes=4)).isoformat())
    log.record_human(state["incident"]["alert_id"], HUMAN_GROUND_TRUTH,
                     diagnosed_at=(parse_ts(alert) + timedelta(minutes=18)).isoformat())
    report = shadow_report(log)
    assert report["agreement"] == 1.0 and report["agreed_and_faster"] == 1
    assert (report["median_agent_minutes"], report["median_human_minutes"]) == (4.0, 18.0)
    assert "| yes |" in render_shadow_report(report)

    # 7. Impact, beyond MTTR.
    stats = approval_stats(audit)
    assert stats["approved"] == 1 and stats["median_approval_minutes"] == 2.0 and stats["bad_or_reverted"] == 0
    assert "override rate 0%" in render_impact(report, stats)


def test_shadow_disagreement_is_visible(world, tmp_path):
    w = world
    baseline = json.loads((AGENT / "checkout_retry_storm.baseline.script.json").read_text())
    final = investigate(Deps(w["registry"], ScriptedLLM.from_file_data(baseline), structural_rules=False),
                        IncidentContext(**w["scenario"]["incident"]))
    log = ShadowLog(tmp_path / "shadow.jsonl")
    state = dump_state(final)
    alert = state["incident"]["alert_started_at"]
    log.record_agent(state, reported_at=alert)
    log.record_human(state["incident"]["alert_id"], HUMAN_GROUND_TRUTH, diagnosed_at=alert)
    report = shadow_report(log)
    assert report["agreement"] == 0.0 and report["confidently_wrong"] == 1


def test_committed_recurrence_files_are_current():
    base = json.loads((AGENT / "checkout_retry_storm.json").read_text())
    committed = json.loads((ROOT / "scenarios" / "checkout_retry_storm_recurrence.json").read_text())
    assert committed == json.loads(json.dumps(recurrence_scenario(base)))


def test_unknown_identities_are_anonymous():
    from sre_rollout.postmortem import role_of

    roles = {"incident-commander": ["slack:U1"]}
    assert role_of("slack:U1", roles) == "the incident commander"
    assert role_of("cli:someone", roles) == "an engineer"
    assert role_of("agent", roles) == "the agent"
