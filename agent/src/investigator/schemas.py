"""Structured outputs the model returns from each LLM node.

Tool arguments travel as a JSON string rather than a free-form object, which
keeps the schemas valid for providers with strict structured-output modes.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field

from investigator.state import HypothesisKind


class NewHypothesis(BaseModel):
    statement: str = Field(description="One sentence, specific and testable")
    kind: HypothesisKind
    component: Optional[str] = Field(None, description="Service, node or release it concerns")
    explains: Optional[str] = Field(None, description="Id of the hypothesis this one explains (its parent in the why-chain)")
    predictions: list[str] = Field(description="1-3 observations that must be true if this hypothesis is right")


class HypothesizeOutput(BaseModel):
    reasoning: str = Field(description="Two or three sentences on what the evidence suggests so far")
    hypotheses: list[NewHypothesis] = Field(description="0-3 new hypotheses; empty if the current set is sufficient")


class Expectation(BaseModel):
    hypothesis_id: str
    if_true: str = Field(description="What this check should show if the hypothesis is right")


class Check(BaseModel):
    tool: str
    args_json: str = Field(description="Tool arguments as a JSON object string")
    hypothesis_ids: list[str]
    expected: list[Expectation]


class PlanOutput(BaseModel):
    reasoning: str = Field(description="Why these checks discriminate between the open hypotheses")
    checks: list[Check] = Field(description="1-3 checks")


class StanceUpdate(BaseModel):
    hypothesis_id: str
    evidence_id: str
    supports: bool
    note: str = Field(description="One line: why this evidence bears on the hypothesis")
    discounted: bool = Field(False, description="True only for refuting evidence you judge doesn't apply; the note must say why")


class HypothesisUpdate(BaseModel):
    hypothesis_id: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: Optional[Literal["open", "supported", "refuted"]] = None
    refuted_reason: Optional[str] = None
    explains: Optional[str] = Field(None, description="Set only to re-link this hypothesis in the why-chain")
    kind: Optional[HypothesisKind] = Field(None, description="Set only to reclassify")


class TimelineNote(BaseModel):
    at: str
    what: str
    evidence_id: str


class AssessOutput(BaseModel):
    stances: list[StanceUpdate]
    updates: list[HypothesisUpdate]
    timeline: list[TimelineNote] = Field(default_factory=list)


class ActionProposalOut(BaseModel):
    tool: str = Field(description="Name of a known action tool")
    args_json: str = Field(description="The tool's arguments as a JSON object string")
    reason: str = Field(description="Why, citing evidence ids")


class ReportOutput(BaseModel):
    summary: str = Field(description="Three to five sentences for the on-call engineer")
    open_questions: list[str]
    proposed_actions: list[str] = Field(description="Proposals only; nothing is executed")
    action_proposals: list[ActionProposalOut] = Field(
        default_factory=list, description="Proposed actions that match a known action tool, in structured form")
