"""The investigation graph.

    intake -> triage -> hypothesize -> plan -> gather -> assess -+-> hypothesize (continue)
                                                                  +-> report      (conclude)
                                                                  +-> escalate    (escalate)

Code nodes: intake, triage, gather, routing. LLM nodes: hypothesize, plan,
assess, report/escalate. See DESIGN.md for why the split sits where it does.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Optional

from langgraph.graph import END, START, StateGraph

from investigator import prompts
from investigator.llm import LLMError, StructuredLLM
from investigator.render import render_evidence, render_hypotheses, render_state
from investigator.rules import chain_to_symptom, clamp_confidence, decide_route
from investigator.schemas import AssessOutput, HypothesizeOutput, PlanOutput, ReportOutput
from investigator.state import (
    ActionProposal,
    Budget,
    Conclusion,
    Evidence,
    Hypothesis,
    IncidentContext,
    InvestigationState,
    PlannedCheck,
    Stance,
    TimelineEvent,
)
from investigator.telemetry import Telemetry
from investigator.tools.base import InMemoryRawStore, RawStore, ToolContext, ToolRegistry, parse_ts

log = logging.getLogger(__name__)

SYMPTOM_ID = "H0"
MAX_TRIAGE_DEPENDENCIES = 6
# Arguments that narrow a query without changing what it asks about. Calls that
# differ only in these count as the same question for the repeated-query guard.
VOLATILE_ARGS = {"pattern", "level", "limit", "start", "end", "min_duration_ms", "errors_only", "operation"}
PROGRESS_DELTA = 0.05


@dataclass
class Deps:
    registry: ToolRegistry
    llm: StructuredLLM
    raw_store: RawStore = field(default_factory=InMemoryRawStore)
    widen_hours: int = 24
    max_checks: int = 3
    # Action tools the report may propose in structured form (Chapter 5).
    known_actions: list[str] = field(default_factory=lambda: [
        "rollback_release(release: str, to_revision: int): roll a Helm release back to an earlier revision",
    ])
    # False gives the Chapter 3 baseline: no change sweep, no confidence
    # rules, no why-chain requirement. For demonstration and evals only.
    structural_rules: bool = True
    # Chapter 7: loop guards, and telemetry for the agent itself.
    guards: bool = True
    max_same_question: int = 3
    telemetry: Telemetry = field(default_factory=Telemetry)


def initial_state(incident: IncidentContext, budget: Optional[Budget] = None) -> InvestigationState:
    return {
        "incident": incident,
        "evidence": [],
        "hypotheses": [],
        "plan": [],
        "timeline": [],
        "budget": budget or Budget(),
        "change_search": "window",
        "conclusion": None,
    }


# Helpers -------------------------------------------------------------------

def _ctx(state: InvestigationState, deps: Deps) -> ToolContext:
    i = state["incident"]
    ctx = ToolContext(
        namespace=i.namespace,
        window_start=parse_ts(i.window_start),
        window_end=parse_ts(i.window_end),
        raw_store=deps.raw_store,
    )
    if state["change_search"] != "window":
        ctx.change_window_start = parse_ts(i.alert_started_at) - timedelta(hours=deps.widen_hours)
    return ctx


class _Collector:
    """Runs tools and turns results into numbered evidence."""

    def __init__(self, state: InvestigationState, deps: Deps, ctx: ToolContext, step: int) -> None:
        self.deps, self.ctx, self.step = deps, ctx, step
        self.next_n = len(state["evidence"]) + 1
        self.items: list[Evidence] = []
        self.calls = 0

    def run(self, tool: str, args: dict) -> tuple[Evidence, dict]:
        with self.deps.telemetry.tool_call(tool, args) as record:
            result = self.deps.registry.call(self.ctx, tool, args)
            record(result)
        self.calls += 1
        spec = self.deps.registry.get(tool)
        ev = Evidence(
            id=f"E{self.next_n}",
            tool=tool,
            args=args,
            signal=spec.signal if spec else "metric",
            ok=result.ok,
            summary=result.summary,
            raw_ref=result.raw_ref,
            collected_at_step=self.step,
        )
        self.next_n += 1
        self.items.append(ev)
        return ev, (result.data if result.ok else {})


def _call_key(tool: str, args: dict) -> str:
    return tool + json.dumps(args, sort_keys=True)


def _question(tool: str, args: dict) -> str:
    return tool + json.dumps({k: v for k, v in args.items() if k not in VOLATILE_ARGS}, sort_keys=True)


def _spend(budget: Budget, *, cost: float = 0.0, calls: int = 0, tokens: int = 0, **changes) -> Budget:
    return budget.model_copy(update={
        "cost_usd": budget.cost_usd + cost, "tool_calls": budget.tool_calls + calls,
        "tokens": budget.tokens + tokens, **changes,
    })


class _MeteredLLM:
    """Wraps the model: every call gets a span, and its tokens are kept for the budget."""

    def __init__(self, inner, telemetry: Telemetry) -> None:
        self.inner, self.telemetry = inner, telemetry
        self.last_tokens = 0

    def structured(self, schema, system, user):
        self.last_tokens = 0
        model = getattr(self.inner, "model_name", type(self.inner).__name__)
        with self.telemetry.llm_call(schema.__name__, model) as record:
            out, cost = self.inner.structured(schema, system, user)
            usage = dict(getattr(self.inner, "last_usage", None) or {})
            record(cost, usage)
        self.last_tokens = usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
        return out, cost


# Nodes ---------------------------------------------------------------------

def make_nodes(deps: Deps):
    llm = _MeteredLLM(deps.llm, deps.telemetry)

    def intake(state: InvestigationState) -> dict:
        # The window is set by whoever builds the IncidentContext (the CLI uses
        # alert start minus 30 minutes). Intake validates it before any tool runs.
        i = state["incident"]
        if parse_ts(i.window_start) >= parse_ts(i.window_end):
            raise ValueError("incident window_start must be before window_end")
        return {}

    def triage(state: InvestigationState) -> dict:
        """Fixed first sweep: identical every run, so evals are comparable."""
        i = state["incident"]
        ctx = _ctx(state, deps)
        c = _Collector(state, deps, ctx, step=0)

        red, _ = c.run("get_service_red", {"service": i.service})
        _, dep_data = c.run("get_dependencies", {"service": i.service, "direction": "downstream", "depth": 1})
        for svc in dep_data.get("services", [])[:MAX_TRIAGE_DEPENDENCIES]:
            c.run("get_service_red", {"service": svc})
        c.run("search_traces", {"service": i.service, "errors_only": True, "limit": 5})

        change_search = "window"
        found: list[dict] = []
        changes = None
        if deps.structural_rules:
            changes, change_data = c.run("list_changes", {})
            found = change_data.get("changes", [])
        if changes is not None and changes.ok and not found:
            # Widen once, change tools only (DESIGN.md, Window widening).
            ctx.change_window_start = parse_ts(i.alert_started_at) - timedelta(hours=deps.widen_hours)
            changes, change_data = c.run("list_changes", {"start": ctx.change_window_start.strftime("%Y-%m-%dT%H:%M:%SZ")})
            found = change_data.get("changes", [])
            change_search = "widened_found" if found else "widened_empty"

        c.run("get_k8s_events", {})

        timeline = [
            TimelineEvent(at=ch["updated"], what=f"{ch['release']} revision {ch['revision']} deployed", evidence_id=changes.id)
            for ch in found
        ]
        symptom = Hypothesis(
            id=SYMPTOM_ID,
            statement=i.symptom,
            kind="symptom",
            component=i.service,
            status="supported",
            confidence=1.0,
            stances=[Stance(evidence_id=red.id, supports=True, note="alerted symptom")],
        )
        return {
            "evidence": c.items,
            "hypotheses": [symptom],
            "timeline": timeline,
            "change_search": change_search,
            "budget": _spend(state["budget"], calls=c.calls),
        }

    def hypothesize(state: InvestigationState) -> dict:
        budget = state["budget"]
        n = len(state["hypotheses"])
        next_ids = ", ".join(f"H{n + k}" for k in range(3))
        budget = budget.model_copy(update={"iterations": budget.iterations + 1})
        try:
            out, cost = llm.structured(
                HypothesizeOutput, prompts.HYPOTHESIZE.format(next_ids=next_ids), render_state(state)
            )
        except LLMError as exc:
            log.warning("hypothesize failed: %s", exc)
            return {"budget": budget}

        known = {h.id for h in state["hypotheses"]}
        new: list[Hypothesis] = []
        for k, nh in enumerate(out.hypotheses[:3]):
            hid = f"H{n + k}"
            explains = nh.explains if nh.explains in known | {h.id for h in new} else None
            kind = "mechanism" if nh.kind == "symptom" else nh.kind  # only triage creates symptoms
            new.append(Hypothesis(
                id=hid, statement=nh.statement, kind=kind, component=nh.component,
                explains=explains, predictions=nh.predictions[:3], confidence=0.2,
            ))
        return {"hypotheses": new, "budget": _spend(budget, cost=cost, tokens=llm.last_tokens)}

    def plan(state: InvestigationState) -> dict:
        system = prompts.PLAN.format(
            max_checks=deps.max_checks,
            tools=json.dumps(deps.registry.describe(), indent=1),
        )
        try:
            out, cost = llm.structured(PlanOutput, system, render_state(state))
        except LLMError as exc:
            log.warning("plan failed: %s", exc)
            return {"plan": []}

        known = {h.id for h in state["hypotheses"]}
        checks: list[PlannedCheck] = []
        for ch in out.checks:
            if deps.registry.get(ch.tool) is None:
                log.info("plan dropped unknown tool %s", ch.tool)
                continue
            try:
                args = json.loads(ch.args_json or "{}")
                assert isinstance(args, dict)
            except (json.JSONDecodeError, AssertionError):
                log.info("plan dropped %s: args are not a JSON object", ch.tool)
                continue
            checks.append(PlannedCheck(
                hypothesis_ids=[h for h in ch.hypothesis_ids if h in known],
                tool=ch.tool,
                args=args,
                expected={e.hypothesis_id: e.if_true for e in ch.expected if e.hypothesis_id in known},
            ))
            if len(checks) == deps.max_checks:
                break
        return {"plan": checks, "budget": _spend(state["budget"], cost=cost, tokens=llm.last_tokens)}

    def gather(state: InvestigationState) -> dict:
        budget = state["budget"]
        done = {_call_key(e.tool, e.args) for e in state["evidence"]}
        c = _Collector(state, deps, _ctx(state, deps), step=budget.iterations)
        asked: dict[str, int] = {}
        for e in state["evidence"]:
            asked[_question(e.tool, e.args)] = asked.get(_question(e.tool, e.args), 0) + 1
        trips = list(budget.guard_trips)
        for check in state["plan"]:
            if budget.tool_calls + c.calls >= budget.max_tool_calls:
                break
            key = _call_key(check.tool, check.args)
            if key in done:
                continue
            q = _question(check.tool, check.args)
            if deps.guards and asked.get(q, 0) >= deps.max_same_question:
                # The same question, reworded: stop paying for it.
                detail = f"{check.tool} asked {asked[q]} times with different filters"
                trips.append(f"repeated_question: {detail}")
                deps.telemetry.guard("repeated_question", detail)
                continue
            done.add(key)
            asked[q] = asked.get(q, 0) + 1
            c.run(check.tool, check.args)
        stalled = 0 if c.items else budget.stalled_iterations + 1
        return {"evidence": c.items,
                "budget": _spend(budget, calls=c.calls, stalled_iterations=stalled, guard_trips=trips)}

    def assess(state: InvestigationState) -> dict:
        step = state["budget"].iterations
        new_ev = [e for e in state["evidence"] if e.collected_at_step == step]
        if not new_ev:
            return {}
        system = prompts.ASSESS.format(new_ids=", ".join(e.id for e in new_ev))
        user = render_state(state) + "\n\nNEW EVIDENCE\n" + render_evidence(new_ev)
        try:
            out, cost = llm.structured(AssessOutput, system, user)
        except LLMError as exc:
            log.warning("assess failed: %s", exc)
            return {}

        hyps = {h.id: h.model_copy(deep=True) for h in state["hypotheses"]}
        evidence = {e.id: e for e in state["evidence"]}

        for s in out.stances:
            h = hyps.get(s.hypothesis_id)
            if h is None or s.evidence_id not in evidence or h.kind == "symptom":
                continue
            h.stances = [x for x in h.stances if x.evidence_id != s.evidence_id]
            h.stances.append(Stance(
                evidence_id=s.evidence_id, supports=s.supports, note=s.note,
                discounted=bool(s.discounted and not s.supports),
            ))

        proposed = {hid: h.confidence for hid, h in hyps.items()}
        for u in out.updates:
            h = hyps.get(u.hypothesis_id)
            if h is None or h.kind == "symptom":
                continue
            proposed[h.id] = u.confidence
            if u.kind and u.kind != "symptom":
                h.kind = u.kind
            if u.explains and u.explains in hyps and u.explains != h.id:
                h.explains = u.explains
            if u.status == "refuted":
                if u.refuted_reason:
                    h.status, h.refuted_reason = "refuted", u.refuted_reason
            elif u.status:
                h.status, h.refuted_reason = u.status, None

        for h in hyps.values():
            if h.kind == "symptom":
                continue
            if not deps.structural_rules:
                h.confidence = max(0.0, min(1.0, proposed[h.id]))
                continue
            value, applied = clamp_confidence(h, proposed[h.id], evidence, hyps, state["change_search"])
            if applied:
                log.info("confidence for %s clamped %.2f -> %.2f by %s", h.id, proposed[h.id], value, applied)
            h.confidence = value

        timeline = [
            TimelineEvent(at=t.at, what=t.what, evidence_id=t.evidence_id)
            for t in out.timeline
            if t.evidence_id in evidence
        ]
        before = {h.id: h for h in state["hypotheses"]}
        moved = [h.id for h in hyps.values() if h.id not in before
                 or abs(h.confidence - before[h.id].confidence) >= PROGRESS_DELTA
                 or (h.status, h.kind, h.explains) != (before[h.id].status, before[h.id].kind, before[h.id].explains)]
        budget = state["budget"]
        no_progress = 0 if (moved or not deps.guards) else budget.no_progress_iterations + 1
        trips = list(budget.guard_trips)
        if no_progress:
            detail = f"evidence {', '.join(e.id for e in new_ev)} changed no hypothesis"
            trips.append(f"no_progress: {detail}")
            deps.telemetry.guard("no_progress", detail)
        return {
            "hypotheses": list(hyps.values()),
            "timeline": timeline,
            "budget": _spend(budget, cost=cost, tokens=llm.last_tokens,
                             no_progress_iterations=no_progress, guard_trips=trips),
        }

    def route(state: InvestigationState) -> str:
        d = decide_route(state["hypotheses"], state["budget"], require_chain=deps.structural_rules)
        deps.telemetry.event("route", **{"sre.route": d.route, "sre.route.reason": d.reason})
        return d.route

    def report(state: InvestigationState) -> dict:
        d = decide_route(state["hypotheses"], state["budget"], require_chain=deps.structural_rules)
        chain = d.chain or []
        by_id = {h.id: h for h in state["hypotheses"]}
        system = prompts.REPORT.format(
            root_id=d.root_cause_id, chain=" -> ".join(chain),
            actions="\n".join(f"- {a}" for a in deps.known_actions) or "(none)",
        )
        out, cost = _write(system, state)
        return {
            "conclusion": Conclusion(
                outcome="root_cause_found",
                root_cause_id=d.root_cause_id,
                causal_chain=chain,
                confidence=by_id[d.root_cause_id].confidence,
                summary=out.summary,
                open_questions=out.open_questions,
                proposed_actions=out.proposed_actions,
                action_proposals=_proposals(out),
            ),
            "budget": _spend(state["budget"], cost=cost, tokens=llm.last_tokens),
        }

    def _proposals(out: ReportOutput) -> list[ActionProposal]:
        """Keep proposals for known action tools with well-formed arguments."""
        known = {a.split("(", 1)[0] for a in deps.known_actions}
        result = []
        for p in out.action_proposals:
            try:
                args = json.loads(p.args_json or "{}")
            except json.JSONDecodeError:
                continue
            if p.tool in known and isinstance(args, dict):
                result.append(ActionProposal(tool=p.tool, args=args, reason=p.reason))
        return result

    def escalate(state: InvestigationState) -> dict:
        d = decide_route(state["hypotheses"], state["budget"], require_chain=deps.structural_rules)
        reason = d.reason if d.route == "escalate" else "stopped before a root cause met the bar"
        by_id = {h.id: h for h in state["hypotheses"]}
        leaders = sorted(
            (h for h in state["hypotheses"] if h.kind == "root_cause" and h.status != "refuted"),
            key=lambda h: h.confidence, reverse=True,
        )
        best = leaders[0] if leaders else None
        chain = (chain_to_symptom(best.id, by_id) or []) if best else []
        out, cost = _write(prompts.ESCALATE.format(reason=reason), state)
        return {
            "conclusion": Conclusion(
                outcome="escalated",
                root_cause_id=best.id if best else None,
                causal_chain=chain,
                confidence=best.confidence if best else 0.0,
                summary=out.summary,
                open_questions=out.open_questions,
                proposed_actions=out.proposed_actions,
                escalation_reason=reason,
            ),
            "budget": _spend(state["budget"], cost=cost, tokens=llm.last_tokens),
        }

    def _write(system: str, state: InvestigationState) -> tuple[ReportOutput, float]:
        try:
            return llm.structured(ReportOutput, system, render_state(state))
        except LLMError as exc:
            log.warning("report writing failed: %s", exc)
            llm.last_tokens = 0
            fallback = ReportOutput(
                summary="The model could not write a summary. Hypotheses and evidence follow.\n\n"
                + render_hypotheses(state["hypotheses"]),
                open_questions=[],
                proposed_actions=[],
            )
            return fallback, 0.0

    return {
        "intake": intake, "triage": triage, "hypothesize": hypothesize, "plan": plan,
        "gather": gather, "assess": assess, "report": report, "escalate": escalate, "route": route,
    }


def build_graph(deps: Deps):
    n = make_nodes(deps)
    g = StateGraph(InvestigationState)
    for name in ("intake", "triage", "hypothesize", "plan", "gather", "assess", "report", "escalate"):
        g.add_node(name, _traced(name, n[name], deps.telemetry))
    g.add_edge(START, "intake")
    g.add_edge("intake", "triage")
    g.add_edge("triage", "hypothesize")
    g.add_edge("hypothesize", "plan")
    g.add_edge("plan", "gather")
    g.add_edge("gather", "assess")
    g.add_conditional_edges(
        "assess", n["route"], {"continue": "hypothesize", "conclude": "report", "escalate": "escalate"}
    )
    g.add_edge("report", END)
    g.add_edge("escalate", END)
    return g.compile()


def _traced(name: str, fn, telemetry: Telemetry):
    def node(state):
        with telemetry.node(name):
            return fn(state)
    node.__name__ = name
    return node


def investigate(deps: Deps, incident: IncidentContext, budget: Optional[Budget] = None) -> dict:
    """Run one investigation inside its own trace."""
    with deps.telemetry.investigation(incident) as handle:
        final = build_graph(deps).invoke(initial_state(incident, budget), {"recursion_limit": 100})
        handle.finish(final)
    return final

