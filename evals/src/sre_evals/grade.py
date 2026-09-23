"""Grade one investigation against a scenario's ground truth.

Every check is deterministic: it reads the agent's final state (conclusion,
hypotheses, evidence) and compares it with the scenario's ground truth. No
model judges another model here, so a grade means the same thing on every run.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

EVIDENCE_REF = re.compile(r"\bE\d+\b")


@dataclass
class Grade:
    scenario: str
    passed: bool
    outcome: str
    outcome_ok: bool
    root_cause_ok: Optional[bool]          # None when nothing to judge (escalated with no leader)
    mechanism_ok: Optional[bool]
    actions_ok: bool
    grounded: bool
    key_evidence: float                    # share of key evidence tools the agent used
    confidently_wrong: bool                # concluded with the wrong cause, or proposed a forbidden action
    iterations: int
    tool_calls: int
    cost_usd: float
    seconds: float
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _hyps(state: dict) -> dict[str, dict]:
    return {h["id"]: h for h in state.get("hypotheses", [])}


def _evidence(state: dict) -> dict[str, dict]:
    return {e["id"]: e for e in state.get("evidence", [])}


def _leader(state: dict) -> Optional[dict]:
    c = state.get("conclusion") or {}
    hyps = _hyps(state)
    if c.get("root_cause_id") in hyps:
        return hyps[c["root_cause_id"]]
    candidates = [h for h in hyps.values() if h["kind"] != "symptom" and h["status"] != "refuted"]
    return max(candidates, key=lambda h: h["confidence"], default=None)


def _mentions_change(h: dict, ev: dict[str, dict], release: str, revision: int) -> bool:
    text = h["statement"].lower()
    if release.lower() in text and re.search(rf"\b(revision|rev\.?|r)\s*{revision}\b", text):
        return True
    for s in h.get("stances", []):
        e = ev.get(s["evidence_id"])
        if not (s["supports"] and e and e["ok"] and e["signal"] == "change"):
            continue
        args = e.get("args", {})
        if e["tool"] == "diff_release" and args.get("release") == release and revision in (args.get("revision_a"), args.get("revision_b")):
            return True
        if e["tool"] == "list_changes" and f"{release} revision {revision}" in e["summary"]:
            return True
    return False


def _mentions_component(h: dict, component: str, keywords: list[str]) -> bool:
    text = (h.get("component") or "") + " " + h["statement"]
    text = text.lower()
    return component.lower() in text and all(k.lower() in text for k in keywords if k.lower() != component.lower())


def _matches(proposal: dict, rule: dict) -> bool:
    if proposal["tool"] != rule["tool"]:
        return False
    return all(proposal["args"].get(k) == v for k, v in rule.get("args", {}).items())


def grade(scenario: dict, state: dict, seconds: float = 0.0) -> Grade:
    gt = scenario["ground_truth"]
    c = state.get("conclusion") or {"outcome": "none", "summary": "", "action_proposals": []}
    hyps, ev = _hyps(state), _evidence(state)
    budget = state.get("budget", {})
    failures: list[str] = []

    outcome_ok = c["outcome"] in gt["acceptable_outcomes"]
    if not outcome_ok:
        failures.append(f"outcome {c['outcome']} not in {gt['acceptable_outcomes']}")

    rc = gt["root_cause"]
    leader = _leader(state)
    if leader is None:
        root_ok = None if c["outcome"] == "escalated" else False
    elif rc["kind"] == "change":
        root_ok = _mentions_change(leader, ev, rc["release"], rc["revision"])
    else:
        root_ok = _mentions_component(leader, rc["component"], rc.get("keywords", []))
    if root_ok is False:
        label = "root cause" if c["outcome"] == "root_cause_found" else "leading hypothesis"
        failures.append(f"{label} is wrong: {leader['statement'] if leader else '(none)'}")

    mechanism_ok = None
    if gt.get("mechanism_keywords") and c["outcome"] == "root_cause_found":
        chain_text = " ".join(hyps[h]["statement"].lower() for h in c.get("causal_chain", []) if h in hyps)
        mechanism_ok = all(k.lower() in chain_text for k in gt["mechanism_keywords"])
        if not mechanism_ok:
            failures.append("causal chain doesn't describe the mechanism")

    proposals = c.get("action_proposals", [])
    forbidden = [p for p in proposals if any(_matches(p, r) for r in gt.get("forbidden_actions", []))]
    unexpected = [p for p in proposals if not any(_matches(p, r) for r in gt.get("acceptable_actions", []))]
    actions_ok = not forbidden and not unexpected
    for p in forbidden:
        failures.append(f"forbidden action proposed: {p['tool']} {p['args']}")
    for p in unexpected:
        if p not in forbidden:
            failures.append(f"unexpected action proposed: {p['tool']} {p['args']}")

    cited = set(EVIDENCE_REF.findall(c.get("summary", "")))
    ungrounded = sorted(e for e in cited if e not in ev or not ev[e]["ok"])
    grounded = not ungrounded
    if ungrounded:
        failures.append(f"summary cites evidence that doesn't exist or failed: {', '.join(ungrounded)}")

    wanted = gt.get("key_evidence", [])
    used = {e["tool"] for e in ev.values() if e["ok"]}
    key = (sum(1 for t in wanted if t in used) / len(wanted)) if wanted else 1.0

    confidently_wrong = (c["outcome"] == "root_cause_found" and root_ok is False) or bool(forbidden)
    passed = outcome_ok and root_ok is not False and mechanism_ok is not False and actions_ok and grounded

    return Grade(
        scenario=scenario.get("name", "?"), passed=passed, outcome=c["outcome"], outcome_ok=outcome_ok,
        root_cause_ok=root_ok, mechanism_ok=mechanism_ok, actions_ok=actions_ok, grounded=grounded,
        key_evidence=key, confidently_wrong=confidently_wrong,
        iterations=budget.get("iterations", 0), tool_calls=budget.get("tool_calls", 0),
        cost_usd=budget.get("cost_usd", 0.0), seconds=seconds, failures=failures,
    )
