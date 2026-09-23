from conftest import ROOT, load, script

from investigator.llm import ScriptedLLM
from investigator.tools.fixture import fixture_backends
from sre_evals.grade import grade
from sre_evals.run import run_one
from sre_evals.snapshot import snapshot


def test_a_snapshot_replays_like_the_original():
    original = load("baseline")
    snap = snapshot(fixture_backends(original), original["incident"])
    assert snap["ground_truth"]["_fill_in"]
    assert {c["revision"] for c in snap["changes"]} == {2, 3}   # the release and the one before it
    snap["ground_truth"] = original["ground_truth"]
    state, _ = run_one(snap, ScriptedLLM(script(ROOT / "scripts" / "baseline.json")))
    assert grade(snap, state).passed
