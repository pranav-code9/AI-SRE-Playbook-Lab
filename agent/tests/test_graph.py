import copy

from investigator.graph import Deps, build_graph, initial_state
from investigator.llm import ScriptedLLM
from investigator.state import Budget, IncidentContext
from investigator.tools.catalog import build_registry
from investigator.tools.fixture import fixture_backends


def run(scenario, script, budget=None, structural_rules=True):
    llm = ScriptedLLM(copy.deepcopy(script))
    graph = build_graph(Deps(build_registry(fixture_backends(scenario)), llm, structural_rules=structural_rules))
    final = graph.invoke(initial_state(IncidentContext(**scenario["incident"]), budget), {"recursion_limit": 100})
    return final, llm


def test_case_study_finds_the_release(scenario, script):
    final, _ = run(scenario, script)
    c = final["conclusion"]
    assert c.outcome == "root_cause_found"
    assert c.root_cause_id == "H3"
    assert c.causal_chain == ["H3", "H2", "H1", "H0"]
    assert c.confidence >= 0.8
    assert final["budget"].iterations == 2
    assert any("revision 3 deployed" in t.what for t in final["timeline"])
    [proposal] = c.action_proposals
    assert (proposal.tool, proposal.args) == ("rollback_release", {"release": "otel-demo", "to_revision": 2})


def test_triage_is_identical_across_runs(scenario, script):
    a, _ = run(scenario, script)
    b, _ = run(scenario, script)
    triage = lambda s: [(e.tool, e.args, e.summary) for e in s["evidence"] if e.collected_at_step == 0]
    assert triage(a) == triage(b)
    assert [e.tool for e in a["evidence"] if e.collected_at_step == 0].count("list_changes") == 1


def test_prompts_carry_no_transcript(scenario, script):
    _, llm = run(scenario, script)
    # every LLM call is built from rendered state, starting with the incident
    assert all(user.startswith("INCIDENT") for _, user in llm.calls)


def naive_script():
    """The Chapter 3 opening failure: the model blames payment and is sure of it."""
    blame_payment = {
        "reasoning": "Payment latency is up, so payment is the problem.",
        "hypotheses": [{"statement": "The payment service is degraded", "kind": "root_cause",
                        "component": "payment", "explains": "H0", "predictions": ["payment latency is high"]}],
    }
    assess = {
        "stances": [{"hypothesis_id": "H1", "evidence_id": "E5", "supports": True, "note": "payment latency elevated"},
                    {"hypothesis_id": "H1", "evidence_id": "E12", "supports": True, "note": "Charge spans fail"}],
        "updates": [{"hypothesis_id": "H1", "confidence": 0.95, "status": "supported"}],
        "timeline": [],
    }
    plan = {"reasoning": "Look at a failed trace.", "checks": [
        {"tool": "summarize_trace", "args_json": "{\"trace_id\": \"err0001\"}", "hypothesis_ids": ["H1"], "expected": []}]}
    empty = {"reasoning": "Nothing new.", "hypotheses": []}
    return {
        "HypothesizeOutput": [blame_payment] + [empty] * 10,
        "PlanOutput": [plan] * 10,
        "AssessOutput": [assess] * 10,
        "ReportOutput": [{"summary": "Handing over.", "open_questions": [], "proposed_actions": []}],
    }


def test_rules_stop_the_agent_blaming_payment(scenario):
    final, _ = run(scenario, naive_script())
    h1 = next(h for h in final["hypotheses"] if h.id == "H1")
    assert h1.confidence == 0.5  # capped: no change evidence behind it
    c = final["conclusion"]
    assert c.outcome == "escalated"
    assert c.escalation_reason == "no new evidence in consecutive iterations"


def test_budget_limits_tool_calls(scenario):
    final, _ = run(scenario, naive_script(), Budget(max_tool_calls=11))
    assert final["budget"].tool_calls == 11  # triage only; planned checks were skipped
    assert final["conclusion"].escalation_reason == "budget exhausted"


def test_change_window_widens_when_nothing_changed_recently(scenario):
    scenario["changes"][1]["updated"] = "2026-09-22T02:00:00Z"  # the bad release landed hours earlier
    final, _ = run(scenario, naive_script())
    changes = [e for e in final["evidence"] if e.tool == "list_changes"]
    assert len(changes) == 2 and "start" in changes[1].args
    assert final["change_search"] == "widened_found"
    assert "revision 3" in changes[1].summary


def test_widened_search_can_come_back_empty(scenario):
    scenario["changes"] = []
    final, _ = run(scenario, naive_script())
    assert final["change_search"] == "widened_empty"


def test_model_failures_escalate_instead_of_crashing(scenario):
    final, _ = run(scenario, {})  # the model never returns anything usable
    c = final["conclusion"]
    assert c.outcome == "escalated"
    assert "could not write a summary" in c.summary


def test_plan_drops_invalid_checks(scenario, script):
    bad = copy.deepcopy(script)
    bad["PlanOutput"][0]["checks"].insert(0, {"tool": "delete_cluster", "args_json": "{}", "hypothesis_ids": [], "expected": []})
    bad["PlanOutput"][0]["checks"].insert(0, {"tool": "get_service_red", "args_json": "not json", "hypothesis_ids": [], "expected": []})
    final, _ = run(scenario, bad)
    assert not any(e.tool == "delete_cluster" for e in final["evidence"])


def test_baseline_without_structural_rules_blames_payment(scenario):
    import json
    from conftest import SCENARIOS

    baseline = json.loads((SCENARIOS / "checkout_retry_storm.baseline.script.json").read_text())
    baseline.pop("_about")
    final, _ = run(scenario, baseline, structural_rules=False)
    c = final["conclusion"]
    assert c.outcome == "root_cause_found" and c.root_cause_id == "H1"  # confidently wrong
    assert not any(e.tool == "list_changes" for e in final["evidence"])
    assert final["budget"].iterations == 1


def test_unknown_or_malformed_proposals_are_dropped(scenario, script):
    bad = copy.deepcopy(script)
    bad["ReportOutput"][0]["action_proposals"] = [
        {"tool": "delete_namespace", "args_json": "{}", "reason": "no"},
        {"tool": "rollback_release", "args_json": "not json", "reason": "no"},
    ]
    final, _ = run(scenario, bad)
    assert final["conclusion"].action_proposals == []


def test_prompts_mark_evidence_as_untrusted(scenario, script):
    _, llm = run(scenario, script)
    assert all("untrusted data" in user for _, user in llm.calls)
