import json

import pytest
from conftest import ROOT

from investigator.graph import Deps, make_nodes, initial_state
from investigator.llm import ScriptedLLM
from investigator.state import IncidentContext
from investigator.tools.catalog import build_registry
from investigator.tools.fixture import fixture_backends
from sre_evals.variants import OTHER_SHAPES, build, variants

NAMES = [v.name for v in variants()]
OTHERS = [make() for make in OTHER_SHAPES]
OTHER_NAMES = [s["name"] for s in OTHERS]


def test_there_are_ten_distinct_variants():
    assert len(NAMES) == 10 == len(set(NAMES))


def test_the_suite_is_not_all_one_incident():
    """A suite of near-copies only proves the agent is good at one incident."""
    assert len(OTHERS) == 2 and not set(OTHER_NAMES) & set(NAMES)
    retry_truths = [build(v)["ground_truth"] for v in variants()]
    assert all(gt["root_cause"].get("release") in (None, "otel-demo", "deployment/checkout")
               for gt in retry_truths), "the ten variants all blame the same release"
    kinds = {s["ground_truth"]["root_cause"]["kind"] for s in OTHERS}
    releases = {s["ground_truth"]["root_cause"].get("release") for s in OTHERS}
    assert kinds == {"change", "component"}, "the extra scenarios repeat one root-cause shape"
    assert "product-catalog" in releases, "no scenario points at a release other than the main one"


@pytest.mark.parametrize("s", OTHERS, ids=OTHER_NAMES)
def test_committed_other_shapes_match_the_generator(s):
    committed = json.loads((ROOT / "scenarios" / f"{s['name']}.json").read_text())
    assert committed == json.loads(json.dumps(s)), "run `sre-evals build-scenarios`"


@pytest.mark.parametrize("s", OTHERS, ids=OTHER_NAMES)
def test_triage_runs_cleanly_on_the_other_shapes(s):
    out = make_nodes(Deps(build_registry(fixture_backends(s)), ScriptedLLM({})))["triage"](
        initial_state(IncidentContext(**s["incident"])))
    assert all(e.ok for e in out["evidence"])
    assert out["change_search"] == "window"


@pytest.mark.parametrize("v", variants(), ids=NAMES)
def test_committed_scenarios_match_the_generator(v):
    committed = json.loads((ROOT / "scenarios" / f"{v.name}.json").read_text())
    assert committed == json.loads(json.dumps(build(v))), "run `sre-evals build-scenarios`"


@pytest.mark.parametrize("v", variants(), ids=NAMES)
def test_ground_truth_is_consistent(v):
    s = build(v)
    gt = s["ground_truth"]
    assert gt["acceptable_outcomes"] and set(gt["acceptable_outcomes"]) <= {"root_cause_found", "escalated"}
    rc = gt["root_cause"]
    if rc["kind"] == "change":
        found = [(c["release"], c["revision"]) for c in s["changes"]]
        found += [(f"deployment/{r['deployment']}", r["revision"]) for r in s.get("rollouts", [])]
        assert (rc["release"], rc["revision"]) in found
    for action in gt["acceptable_actions"]:
        release, rev = action["args"]["release"], action["args"]["to_revision"]
        assert any(c["release"] == release and c["revision"] == rev for c in s["changes"])


@pytest.mark.parametrize("v", variants(), ids=NAMES)
def test_triage_runs_cleanly_on_every_variant(v):
    s = build(v)
    out = make_nodes(Deps(build_registry(fixture_backends(s)), ScriptedLLM({})))["triage"](
        initial_state(IncidentContext(**s["incident"])))
    assert all(e.ok for e in out["evidence"])
    expected = {"release-hours-earlier": "widened_found"}.get(v.name, "window")   # manual-change: found as a rollout
    assert out["change_search"] == expected
