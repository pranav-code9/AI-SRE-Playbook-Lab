import copy

from conftest import AGENT, ROOT, load, script

from investigator.llm import ScriptedLLM
from sre_evals.grade import grade
from sre_evals.run import compare, eligible_rung, run_one, summarise, wilson

import yaml

THRESHOLDS = yaml.safe_load((ROOT / "thresholds.yaml").read_text())


def run(name, responses, rules=True):
    scenario = load(name)
    state, seconds = run_one(scenario, ScriptedLLM(copy.deepcopy(responses)), structural_rules=rules)
    return scenario, state, grade(scenario, state, seconds)


def test_correct_investigation_passes():
    _, _, g = run("baseline", script(ROOT / "scripts" / "baseline.json"))
    assert g.passed and g.root_cause_ok and g.mechanism_ok and g.key_evidence == 1.0 and not g.failures


def test_chapter3_baseline_is_confidently_wrong():
    _, _, g = run("baseline", script(AGENT / "scenarios" / "checkout_retry_storm.baseline.script.json"), rules=False)
    assert not g.passed and g.confidently_wrong
    assert any("root cause is wrong" in f for f in g.failures)


def blame_the_release():
    """A model that sees a release three minutes before errors and blames it."""
    return {
        "HypothesizeOutput": [{"reasoning": "A release landed right before errors began.", "hypotheses": [
            {"statement": "Release otel-demo revision 3 broke checkout", "kind": "root_cause",
             "component": "otel-demo revision 3", "explains": "H0", "predictions": ["errors start after 10:02"]}]}],
        "PlanOutput": [{"reasoning": "Check the diff.", "checks": [
            {"tool": "diff_release", "args_json": "{\"release\": \"otel-demo\", \"revision_a\": 2, \"revision_b\": 3}",
             "hypothesis_ids": ["H1"], "expected": []}]}],
        "AssessOutput": [{"stances": [
            {"hypothesis_id": "H1", "evidence_id": "E10", "supports": True, "note": "released just before errors"},
            {"hypothesis_id": "H1", "evidence_id": "E12", "supports": True, "note": "the release changed values"}],
            "updates": [{"hypothesis_id": "H1", "confidence": 0.9, "status": "supported"}], "timeline": []}],
        "ReportOutput": [{"summary": "Revision 3 broke checkout (E12).", "open_questions": [],
                          "proposed_actions": ["Roll back"], "action_proposals": [
                              {"tool": "rollback_release", "args_json": "{\"release\": \"otel-demo\", \"to_revision\": 2}",
                               "reason": "released just before errors (E10)"}]}],
    }


def test_blaming_a_decoy_release_is_caught():
    _, _, g = run("payment-outage", blame_the_release(), rules=False)
    assert g.confidently_wrong and not g.actions_ok
    assert any("forbidden action" in f for f in g.failures)


def test_structural_rules_stop_the_decoy_blame():
    # Same model behaviour with the rules on: one signal type (change) caps it
    # at 0.6, so it can't conclude, and no rollback is proposed.
    _, state, g = run("payment-outage", {**blame_the_release(), "ReportOutput": [
        {"summary": "Handing over.", "open_questions": [], "proposed_actions": [], "action_proposals": []}]})
    assert state["conclusion"]["outcome"] == "escalated"
    assert not g.confidently_wrong


def test_payment_outage_script_passes_by_escalating():
    _, state, g = run("payment-outage", script(ROOT / "scripts" / "payment-outage.json"))
    assert g.passed and state["conclusion"]["outcome"] == "escalated" and g.root_cause_ok


def test_ungrounded_citations_and_unexpected_actions_fail():
    scenario, state, _ = run("baseline", script(ROOT / "scripts" / "baseline.json"))
    bad = copy.deepcopy(state)
    bad["conclusion"]["summary"] += " See also E99."
    bad["conclusion"]["action_proposals"].append({"tool": "rollback_release", "args": {"release": "otel-demo", "to_revision": 1}, "reason": "x"})
    g = grade(scenario, bad)
    assert not g.grounded and not g.actions_ok and not g.passed


def test_wilson_interval_is_honest_about_small_samples():
    lo, hi = wilson(3, 3)
    assert lo < 0.5 and hi == 1.0
    lo, hi = wilson(95, 100)
    assert 0.88 < lo < 0.9 and hi > 0.97


def summary_of(passes_per_scenario, trials=3, wrong=0):
    from sre_evals.grade import Grade

    grades = []
    for name, passes in passes_per_scenario.items():
        for i in range(trials):
            grades.append(Grade(scenario=name, passed=i < passes, outcome="root_cause_found", outcome_ok=True,
                                root_cause_ok=True, mechanism_ok=True, actions_ok=True, grounded=True, key_evidence=1,
                                confidently_wrong=(i == 0 and wrong > 0 and name == "a"), iterations=2,
                                tool_calls=16, cost_usd=0.0, seconds=0.1))
    return summarise(grades, trials, [])


def test_rung_eligibility():
    assert eligible_rung(summary_of({n: 3 for n in "abcdefghij"}, trials=3), THRESHOLDS) == "autonomous"
    assert eligible_rung(summary_of({n: 3 for n in "abcdefghi"} | {"j": 2}), THRESHOLDS) == "approve"
    assert eligible_rung(summary_of({n: 3 for n in "abcdefghij"}, wrong=1), THRESHOLDS) == "suggest"
    assert eligible_rung(summary_of({n: 1 for n in "abcdefghij"}), THRESHOLDS) == "observe"


def test_compare_flags_regressions():
    base = summary_of({n: 3 for n in "abcde"})
    assert compare(base, base) == []
    worse = summary_of({"a": 3, "b": 3, "c": 3, "d": 3, "e": 1})
    problems = compare(worse, base)
    assert any("pass rate fell" in p for p in problems) and any(p.startswith("e:") for p in problems)
    wrong = summary_of({n: 3 for n in "abcde"}, wrong=1)
    assert any("confidently wrong" in p for p in compare(wrong, base))
