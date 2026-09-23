import json
from datetime import timedelta

import anyio
import pytest
import yaml
from conftest import ROOT
from mcp import Client

from investigator.tools.base import parse_ts
from investigator.tools.fixture import fixture_backends
from sre_mcp import flows
from sre_mcp.actions import RecordingActions, RequestRollbackApproval, Rollback
from sre_mcp.servers import build_actions_server
from sre_policy import Gate
from sre_policy.model import Policy

POLICY = ROOT.parent / "policies" / "trust-ladder.yaml"
REASON = "Revision 3 cut the payment timeout (E13) and errors began after it (E16)"


def policy(rung):
    data = yaml.safe_load(POLICY.read_text())
    data["actions"]["rollback_release"]["rung"] = rung
    return Policy.model_validate(data)


@pytest.fixture
def now(scenario):
    return parse_ts(scenario["incident"]["window_end"])


def make(tmp_path, scenario, registry, context, now, rung="approve"):
    gate = Gate(policy(rung), tmp_path, clock=lambda: now)
    backend = RecordingActions(scenario)
    notified = []
    kw = dict(gate=gate, actor="agent", notify=notified.append)
    server = build_actions_server([Rollback(backend, registry, **kw), RequestRollbackApproval(backend, registry, **kw)], context)
    return gate, backend, server, notified


def test_approve_rung_over_mcp(tmp_path, scenario, registry, context, now):
    gate, backend, server, notified = make(tmp_path, scenario, registry, context, now)
    args = {"release": "otel-demo", "to_revision": 2, "reason": REASON}

    async def go():
        async with Client(server) as c:
            names = [t.name for t in (await c.list_tools()).tools]
            blocked = await c.call_tool("rollback_release", {**args, "dry_run": False})
            requested = await c.call_tool("request_rollback_approval", args)
            approval_id = requested.structured_content["data"]["approval_id"]
            early = await c.call_tool("rollback_release", {**args, "dry_run": False, "approval_id": approval_id})
            gate.decide_approval(approval_id, "cli:ic-oncall", approve=True)
            done = await c.call_tool("rollback_release", {**args, "dry_run": False, "approval_id": approval_id})
            return names, blocked, requested, early, done

    names, blocked, requested, early, done = anyio.run(go)
    assert names == ["rollback_release", "request_rollback_approval"]
    assert blocked.is_error and "needs_approval" in blocked.content[0].text
    assert not requested.is_error and len(notified) == 1
    assert early.is_error and "approval_granted" in early.content[0].text
    assert not done.is_error and done.structured_content["data"]["executed"]
    assert backend.performed == [("otel-demo", "otel-demo", 2)]
    assert gate.audit.verify() == []


def test_suggest_rung_never_executes(tmp_path, scenario, registry, context, now):
    gate, backend, server, _ = make(tmp_path, scenario, registry, context, now, rung="suggest")

    async def go():
        async with Client(server) as c:
            return await c.call_tool("rollback_release", {"release": "otel-demo", "to_revision": 2,
                                                          "reason": REASON, "dry_run": False})

    r = anyio.run(go)
    assert r.is_error and "helm rollback otel-demo 2" in r.content[0].text
    assert backend.performed == []


def agent_state(tmp_path, confidence=0.9, anchored=True, rival=False):
    stance = {"evidence_id": "E13", "supports": True, "note": "diff"}
    hypotheses = [{"id": "H0", "kind": "symptom", "stances": []},
                  {"id": "H3", "kind": "root_cause", "status": "supported", "stances": [stance] if anchored else []}]
    if rival:
        # A second explanation the investigation never knocked down.
        hypotheses.append({"id": "H4", "kind": "root_cause", "status": "supported", "stances": []})
    state = {
        "conclusion": {"confidence": confidence, "root_cause_id": "H3", "causal_chain": ["H3", "H0"], "action_proposals": [
            {"tool": "rollback_release", "args": {"release": "otel-demo", "to_revision": 2}, "reason": REASON}]},
        "hypotheses": hypotheses,
        "evidence": [{"id": "E13", "signal": "change", "ok": True}],
        "timeline": [{"at": "2026-09-22T10:05:00Z", "what": "otel-demo revision 3 deployed", "evidence_id": "E10"}],
    }
    path = tmp_path / "state.json"
    path.write_text(json.dumps(state))
    return path


def test_propose_approve_execute(tmp_path, scenario, registry, context, now):
    gate, backend, _, notified = make(tmp_path / "st", scenario, registry, context, now)
    lines = []
    flows.propose(agent_state(tmp_path), gate, backend, registry, context, notified.append, now, out=lines.append)
    assert "needs_approval at rung approve" in lines[0]
    [approval] = gate.pending()
    gate.decide_approval(approval.id, "cli:ic-oncall", approve=True)
    assert flows.execute(approval.id, gate, backend, registry, context, actor="cli:ic-oncall", out=lines.append) == 0
    assert backend.performed == [("otel-demo", "otel-demo", 2)]


def test_propose_at_autonomous_executes_within_preconditions(tmp_path, scenario, registry, context, now):
    gate, backend, _, _ = make(tmp_path / "st", scenario, registry, context, now, rung="autonomous")
    lines = []
    flows.propose(agent_state(tmp_path), gate, backend, registry, context, None, now, out=lines.append)
    assert "allowed at rung autonomous" in lines[0] and "Executed" in lines[1]

    gate2, backend2, _, _ = make(tmp_path / "st2", scenario, registry, context, now, rung="autonomous")
    lines = []
    flows.propose(agent_state(tmp_path, rival=True), gate2, backend2, registry, context, None, now, out=lines.append)
    assert "needs_approval" in lines[0] and "sole_root_cause" in lines[0] and backend2.performed == []


def test_verify_demotes_after_a_bad_rollback(tmp_path, scenario, registry, context, now):
    gate, backend, _, _ = make(tmp_path / "st", scenario, registry, context, now, rung="autonomous")
    flows.propose(agent_state(tmp_path), gate, backend, registry, context, None, now, out=lambda _: None)
    execution = next(e["data"]["execution_id"] for e in gate.audit.entries() if e["event"] == "executed")
    # The recorded scenario has no data after the rollback, so there's nothing
    # to show errors falling: verification must fail safe and demote.
    later = now + timedelta(minutes=11)
    gate.now = lambda: later
    assert flows.verify(execution, gate, fixture_backends(scenario), out=lambda _: None) == 1
    assert gate.effective_rung("rollback_release").label == "approve"


def test_autonomy_needs_the_structural_facts_not_just_confidence(tmp_path, scenario, registry, context, now):
    gate, backend, _, _ = make(tmp_path / "st", scenario, registry, context, now, rung="autonomous")
    lines = []
    flows.propose(agent_state(tmp_path, confidence=0.99, anchored=False), gate, backend, registry, context, None, now,
                  out=lines.append)
    assert "needs_approval" in lines[0] and "change_anchored" in lines[0] and backend.performed == []

    # The converse: a modest self-report is no obstacle, because nothing reads it.
    gate2, backend2, _, _ = make(tmp_path / "st2", scenario, registry, context, now, rung="autonomous")
    lines = []
    flows.propose(agent_state(tmp_path, confidence=0.4), gate2, backend2, registry, context, None, now, out=lines.append)
    assert "allowed at rung autonomous" in lines[0]


def test_plan_shows_chart_and_rendered_resources(scenario, registry, context):
    from sre_mcp.actions import RollbackArgs
    backend = RecordingActions(scenario)
    plan, summary, _ = Rollback(backend, registry).plan(context(), RollbackArgs(release="otel-demo", to_revision=2, reason=REASON))
    assert plan["resources_changed"] == ["Deployment/checkout"]
    assert plan["chart"]["current"] == plan["chart"]["target"] and "Chart unchanged" in summary


def test_a_stale_plan_is_not_executed(tmp_path, scenario, registry, context, now):
    gate, backend, _, _ = make(tmp_path / "st", scenario, registry, context, now)
    flows.propose(agent_state(tmp_path), gate, backend, registry, context, None, now, out=lambda _: None)
    [approval] = gate.pending()
    gate.decide_approval(approval.id, "cli:ic-oncall", approve=True)
    real = backend.current_revision
    calls = {"n": 0}

    def moving(ns, release):   # someone deploys revision 4 between the decision and the rollback
        calls["n"] += 1
        return real(ns, release) if calls["n"] == 1 else 4
    backend.current_revision = moving
    lines = []
    assert flows.execute(approval.id, gate, backend, registry, context, "cli:ic-oncall", out=lines.append) == 1
    assert "moved on" in lines[0] and backend.performed == []
