"""Deterministic rules the model can't override.

The model proposes confidence updates in `assess`; `clamp_confidence` enforces
the limits from DESIGN.md before anything reaches the state. `decide_route`
chooses between continuing, concluding and escalating. Both are pure functions
so they can be unit-tested and tuned in Chapter 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from investigator.state import Budget, ChangeSearch, Evidence, Hypothesis

NO_SUPPORT_CAP = 0.3
SINGLE_SIGNAL_CAP = 0.6
UNEXPLAINED_REFUTATION_CAP = 0.4
UNANCHORED_ROOT_CAUSE_CAP = 0.5

CONCLUDE_MIN_CONFIDENCE = 0.8
CONCLUDE_MAX_RIVAL = 0.3
MAX_STALLED_ITERATIONS = 2
MAX_NO_PROGRESS_ITERATIONS = 2


def chain_to_symptom(hypothesis_id: str, by_id: dict[str, Hypothesis]) -> Optional[list[str]]:
    """Follow `explains` links from a hypothesis. Returns the chain of ids,
    starting at `hypothesis_id` and ending at a symptom, or None if the chain
    breaks, loops, or passes through a refuted hypothesis."""
    chain: list[str] = []
    current = by_id.get(hypothesis_id)
    while current is not None:
        if current.id in chain or current.status == "refuted":
            return None
        chain.append(current.id)
        if current.kind == "symptom":
            return chain
        current = by_id.get(current.explains) if current.explains else None
    return None


def _supporting_signals(h: Hypothesis, evidence: dict[str, Evidence]) -> set[str]:
    return {
        evidence[s.evidence_id].signal
        for s in h.stances
        if s.supports and s.evidence_id in evidence and evidence[s.evidence_id].ok
    }


def _is_change_anchored(
    h: Hypothesis, evidence: dict[str, Evidence], change_search: ChangeSearch
) -> bool:
    if "change" in _supporting_signals(h, evidence):
        return True
    return change_search == "widened_empty"


def clamp_confidence(
    h: Hypothesis,
    proposed: float,
    evidence: dict[str, Evidence],
    by_id: dict[str, Hypothesis],
    change_search: ChangeSearch,
) -> tuple[float, list[str]]:
    """Apply the confidence rules. Returns the allowed value and the names of
    any rules that lowered it, so the clamp is visible in traces and evals."""
    value = max(0.0, min(1.0, proposed))
    applied: list[str] = []

    def cap(limit: float, rule: str) -> None:
        nonlocal value
        if value > limit:
            value = limit
            applied.append(rule)

    signals = _supporting_signals(h, evidence)
    if not signals:
        cap(NO_SUPPORT_CAP, "no_supporting_evidence")
    if len(signals) < 2:
        cap(SINGLE_SIGNAL_CAP, "fewer_than_two_signal_types")
    if any(not s.supports and not s.discounted for s in h.stances):
        cap(UNEXPLAINED_REFUTATION_CAP, "unexplained_refuting_evidence")
    if h.kind == "root_cause":
        if not _is_change_anchored(h, evidence, change_search):
            cap(UNANCHORED_ROOT_CAUSE_CAP, "root_cause_not_linked_to_change")
        if chain_to_symptom(h.id, by_id) is None:
            cap(UNANCHORED_ROOT_CAUSE_CAP, "root_cause_chain_does_not_reach_symptom")
    return value, applied


Route = Literal["continue", "conclude", "escalate"]


@dataclass
class RouteDecision:
    route: Route
    reason: str
    root_cause_id: Optional[str] = None
    chain: Optional[list[str]] = None


def decide_route(hypotheses: list[Hypothesis], budget: Budget, require_chain: bool = True) -> RouteDecision:
    by_id = {h.id: h for h in hypotheses}
    root_causes = [h for h in hypotheses if h.kind == "root_cause" and h.status != "refuted"]

    ranked = sorted(root_causes, key=lambda h: h.confidence, reverse=True)
    if ranked:
        best = ranked[0]
        rival = ranked[1].confidence if len(ranked) > 1 else 0.0
        chain = chain_to_symptom(best.id, by_id)
        if not require_chain:  # Chapter 3 baseline only
            chain = chain or [best.id]
        if best.confidence >= CONCLUDE_MIN_CONFIDENCE and chain and rival <= CONCLUDE_MAX_RIVAL:
            return RouteDecision("conclude", "root cause meets the bar", best.id, chain)

    if budget.exhausted():
        return RouteDecision("escalate", "budget exhausted")
    if budget.stalled_iterations >= MAX_STALLED_ITERATIONS:
        return RouteDecision("escalate", "no new evidence in consecutive iterations")
    if budget.no_progress_iterations >= MAX_NO_PROGRESS_ITERATIONS:
        return RouteDecision("escalate", "new evidence changed nothing in consecutive iterations")

    candidates = [h for h in hypotheses if h.kind != "symptom"]
    if budget.iterations >= 1 and candidates and all(h.status == "refuted" for h in candidates):
        return RouteDecision("escalate", "every hypothesis refuted")

    return RouteDecision("continue", "open questions remain")
