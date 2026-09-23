from investigator.rules import chain_to_symptom, clamp_confidence, decide_route
from investigator.state import Budget, Evidence, Hypothesis, Stance


def ev(eid, signal, ok=True):
    return Evidence(id=eid, tool="t", args={}, signal=signal, ok=ok, summary="", raw_ref="", collected_at_step=1)


EVIDENCE = {e.id: e for e in [ev("E1", "metric"), ev("E2", "trace"), ev("E3", "change"), ev("E4", "log", ok=False)]}
SYMPTOM = Hypothesis(id="H0", statement="errors", kind="symptom", status="supported", confidence=1.0)


def hyp(hid="H1", kind="mechanism", explains="H0", stances=(), status="open", confidence=0.2):
    return Hypothesis(id=hid, statement=hid, kind=kind, explains=explains, stances=list(stances),
                      status=status, confidence=confidence)


def plus(eid):
    return Stance(evidence_id=eid, supports=True, note="")


def minus(eid, discounted=False):
    return Stance(evidence_id=eid, supports=False, note="", discounted=discounted)


def clamp(h, proposed, others=(), change_search="window"):
    by_id = {x.id: x for x in [SYMPTOM, h, *others]}
    return clamp_confidence(h, proposed, EVIDENCE, by_id, change_search)


def test_no_support_caps_at_0_3():
    assert clamp(hyp(), 0.9) == (0.3, ["no_supporting_evidence"])


def test_failed_evidence_does_not_count_as_support():
    value, _ = clamp(hyp(stances=[plus("E4")]), 0.9)
    assert value == 0.3


def test_single_signal_type_caps_at_0_6():
    value, applied = clamp(hyp(stances=[plus("E1")]), 0.9)
    assert value == 0.6 and applied == ["fewer_than_two_signal_types"]


def test_two_signal_types_allow_high_confidence():
    assert clamp(hyp(stances=[plus("E1"), plus("E2")]), 0.9) == (0.9, [])


def test_unexplained_refutation_caps_at_0_4_until_discounted():
    h = hyp(stances=[plus("E1"), plus("E2"), minus("E3")])
    assert clamp(h, 0.9)[0] == 0.4
    h.stances[-1] = minus("E3", discounted=True)
    assert clamp(h, 0.9)[0] == 0.9


def test_root_cause_needs_change_evidence():
    rc = hyp("H2", kind="root_cause", explains="H0", stances=[plus("E1"), plus("E2")])
    value, applied = clamp(rc, 0.9)
    assert value == 0.5 and "root_cause_not_linked_to_change" in applied


def test_root_cause_allowed_without_change_only_after_widened_search_is_empty():
    rc = hyp("H2", kind="root_cause", explains="H0", stances=[plus("E1"), plus("E2")])
    assert clamp(rc, 0.9, change_search="widened_found")[0] == 0.5
    assert clamp(rc, 0.9, change_search="widened_empty")[0] == 0.9


def test_root_cause_needs_chain_to_symptom():
    rc = hyp("H2", kind="root_cause", explains="H9", stances=[plus("E1"), plus("E3")])
    value, applied = clamp(rc, 0.9)
    assert value == 0.5 and applied == ["root_cause_chain_does_not_reach_symptom"]


def test_chain_breaks_on_refuted_link_and_loops():
    h1 = hyp("H1", explains="H0", status="refuted")
    h2 = hyp("H2", kind="root_cause", explains="H1")
    assert chain_to_symptom("H2", {x.id: x for x in (SYMPTOM, h1, h2)}) is None
    a, b = hyp("H1", explains="H2"), hyp("H2", explains="H1")
    assert chain_to_symptom("H1", {x.id: x for x in (SYMPTOM, a, b)}) is None


def test_chain_reaches_symptom():
    h1 = hyp("H1", explains="H0")
    h2 = hyp("H2", kind="root_cause", explains="H1")
    assert chain_to_symptom("H2", {x.id: x for x in (SYMPTOM, h1, h2)}) == ["H2", "H1", "H0"]


def test_route_concludes_when_root_cause_meets_bar():
    hs = [SYMPTOM, hyp("H1", confidence=0.7), hyp("H2", kind="root_cause", explains="H1", confidence=0.85)]
    d = decide_route(hs, Budget(iterations=1))
    assert d.route == "conclude" and d.root_cause_id == "H2" and d.chain == ["H2", "H1", "H0"]


def test_route_continues_when_rival_is_strong():
    hs = [SYMPTOM, hyp("H1", kind="root_cause", confidence=0.85), hyp("H2", kind="root_cause", confidence=0.4)]
    assert decide_route(hs, Budget(iterations=1)).route == "continue"


def test_route_ignores_refuted_rivals():
    hs = [SYMPTOM, hyp("H1", kind="root_cause", confidence=0.85),
          hyp("H2", kind="root_cause", confidence=0.6, status="refuted")]
    assert decide_route(hs, Budget(iterations=1)).route == "conclude"


def test_route_escalates_on_budget_stall_and_all_refuted():
    open_h = [SYMPTOM, hyp("H1")]
    assert decide_route(open_h, Budget(iterations=8)).reason == "budget exhausted"
    assert decide_route(open_h, Budget(tool_calls=30)).reason == "budget exhausted"
    assert decide_route(open_h, Budget(iterations=2, stalled_iterations=2)).reason.startswith("no new evidence")
    refuted = [SYMPTOM, hyp("H1", status="refuted")]
    assert decide_route(refuted, Budget(iterations=1)).reason == "every hypothesis refuted"
