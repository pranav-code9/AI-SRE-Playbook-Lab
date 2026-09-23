"""Investigation state: the single record of what the agent saw and concluded.

The state holds structured data only, never a chat transcript. Each LLM node
renders the parts it needs into its own prompt (see render.py).
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field
from typing_extensions import TypedDict

SignalType = Literal["metric", "trace", "log", "change", "k8s", "topology"]
HypothesisKind = Literal["symptom", "mechanism", "root_cause"]
HypothesisStatus = Literal["open", "supported", "refuted"]
ChangeSearch = Literal["window", "widened_found", "widened_empty"]


class IncidentContext(BaseModel):
    alert_id: str
    service: str
    namespace: str
    symptom: str
    alert_started_at: str
    window_start: str
    window_end: str


class Evidence(BaseModel):
    id: str
    tool: str
    args: dict
    signal: SignalType
    ok: bool
    summary: str
    raw_ref: str
    collected_at_step: int


class Stance(BaseModel):
    evidence_id: str
    supports: bool
    note: str
    # A refuting stance the model has explained away. The note must say why
    # the evidence doesn't apply; until then, refutation caps confidence.
    discounted: bool = False


class Hypothesis(BaseModel):
    id: str
    statement: str
    kind: HypothesisKind
    component: Optional[str] = None
    explains: Optional[str] = None
    predictions: list[str] = Field(default_factory=list)
    status: HypothesisStatus = "open"
    confidence: float = 0.2
    stances: list[Stance] = Field(default_factory=list)
    refuted_reason: Optional[str] = None


class PlannedCheck(BaseModel):
    hypothesis_ids: list[str]
    tool: str
    args: dict
    expected: dict[str, str] = Field(default_factory=dict)


class TimelineEvent(BaseModel):
    at: str
    what: str
    evidence_id: str


class Budget(BaseModel):
    max_iterations: int = 8
    max_tool_calls: int = 30
    max_cost_usd: float = 1.00
    max_tokens: int = 300_000
    iterations: int = 0
    tool_calls: int = 0
    cost_usd: float = 0.0
    tokens: int = 0
    stalled_iterations: int = 0
    no_progress_iterations: int = 0
    guard_trips: list[str] = Field(default_factory=list)

    def exhausted(self) -> bool:
        return (
            self.iterations >= self.max_iterations
            or self.tool_calls >= self.max_tool_calls
            or self.cost_usd >= self.max_cost_usd
            or self.tokens >= self.max_tokens
        )


class ActionProposal(BaseModel):
    """A proposed call to a known action tool. Proposals only: the agent has no
    write tools. Chapter 5's policy gate decides what happens to them."""

    tool: str
    args: dict
    reason: str


class Conclusion(BaseModel):
    outcome: Literal["root_cause_found", "escalated"]
    root_cause_id: Optional[str]
    causal_chain: list[str]
    confidence: float
    summary: str
    open_questions: list[str]
    proposed_actions: list[str]
    action_proposals: list[ActionProposal] = Field(default_factory=list)
    escalation_reason: Optional[str] = None


def merge_by_id(old: list, new: list) -> list:
    """Reducer: items with an existing id replace it; new ids are appended."""
    merged = {item.id: item for item in old}
    for item in new:
        merged[item.id] = item
    return list(merged.values())


class InvestigationState(TypedDict):
    incident: IncidentContext
    evidence: Annotated[list[Evidence], operator.add]
    hypotheses: Annotated[list[Hypothesis], merge_by_id]
    plan: list[PlannedCheck]
    timeline: Annotated[list[TimelineEvent], operator.add]
    budget: Budget
    change_search: ChangeSearch
    conclusion: Optional[Conclusion]
