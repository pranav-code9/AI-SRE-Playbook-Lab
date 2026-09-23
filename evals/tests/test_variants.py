import json

import pytest
from conftest import ROOT

from investigator.graph import Deps, make_nodes, initial_state
from investigator.llm import ScriptedLLM
from investigator.state import IncidentContext
from investigator.tools.catalog import build_registry
from investigator.tools.fixture import fixture_backends
from sre_evals.variants import build, variants

NAMES = [v.name for v in variants()]


def test_there_are_ten_distinct_variants():
    assert len(NAMES) == 10 == len(set(NAMES))


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
