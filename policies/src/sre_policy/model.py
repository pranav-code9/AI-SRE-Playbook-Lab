"""The policy file's schema."""

from __future__ import annotations

from enum import IntEnum
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator


class Rung(IntEnum):
    OBSERVE = 0
    SUGGEST = 1
    APPROVE = 2
    AUTONOMOUS = 3

    @classmethod
    def parse(cls, value: "str | int | Rung") -> "Rung":
        if isinstance(value, (Rung, int)):
            return cls(int(value))
        return cls[value.upper()]

    @property
    def label(self) -> str:
        return self.name.lower()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BlastRadius(Strict):
    namespaces: list[str] = Field(default_factory=list)
    releases: list[str] = Field(default_factory=list)
    max_revisions_back: Optional[int] = None
    max_changed_values: Optional[int] = None
    max_resources_changed: Optional[int] = None  # rendered resources; unknown counts as too many
    allow_chart_change: bool = False             # a rollback that also changes the chart isn't routine


class RateLimit(Strict):
    max: int
    per_minutes: int


class Autonomy(Strict):
    # Confidence is a ranking the rules have already capped, not a probability,
    # so it is deliberately not a gate here. Every precondition below is a
    # structural fact about the investigation that a human can re-check from the
    # evidence without trusting the model's self-report.
    max_change_age_minutes: Optional[int] = None
    require_change_anchor: bool = True    # the root cause is supported by change evidence
    require_complete_chain: bool = True   # the why-chain reaches the symptom
    require_sole_root_cause: bool = True  # no rival root-cause hypothesis is still standing


class Verify(Strict):
    # An absolute objective, not a relative drop: a 50% fall in error rate is
    # also what you get when traffic collapses, so a relative test can score an
    # outage as a success. min_request_rate refuses to judge a window too quiet
    # to be evidence of anything.
    service: str
    metric: str = "error_rate"
    must_fall_below: float = 0.01
    min_request_rate: float = 1.0
    after_minutes: int = 10


class Promotion(Strict):
    to_approve: dict[str, float] = Field(default_factory=dict)
    to_autonomous: dict[str, float] = Field(default_factory=dict)


class ActionPolicy(Strict):
    rung: Rung = Rung.OBSERVE
    approvers: list[str] = Field(default_factory=list)
    blast_radius: BlastRadius = Field(default_factory=BlastRadius)
    rate_limit: Optional[RateLimit] = None
    autonomy: Autonomy = Field(default_factory=Autonomy)
    verify: Optional[Verify] = None
    promotion: Promotion = Field(default_factory=Promotion)

    @field_validator("rung", mode="before")
    @classmethod
    def _rung(cls, v):
        return Rung.parse(v)


class Defaults(Strict):
    approval_ttl_minutes: int = 15


class Policy(Strict):
    version: int
    roles: dict[str, list[str]] = Field(default_factory=dict)
    defaults: Defaults = Field(default_factory=Defaults)
    actions: dict[str, ActionPolicy] = Field(default_factory=dict)

    def action(self, name: str) -> ActionPolicy:
        return self.actions.get(name, ActionPolicy())

    def is_approver(self, action: str, identity: str) -> bool:
        return any(identity in self.roles.get(role, []) for role in self.action(action).approvers)

    def problems(self) -> list[str]:
        out = []
        for name, a in self.actions.items():
            for role in a.approvers:
                if role not in self.roles:
                    out.append(f"{name}: approver role '{role}' is not defined")
            if a.rung >= Rung.APPROVE and not a.approvers:
                out.append(f"{name}: executing rungs need at least one approver role")
            if a.rung >= Rung.APPROVE and not (a.blast_radius.namespaces and a.blast_radius.releases):
                out.append(f"{name}: executing rungs need a blast radius (namespaces and releases)")
            if a.rung == Rung.AUTONOMOUS and a.rate_limit is None:
                out.append(f"{name}: rung autonomous needs a rate limit")
        return out


def load_policy(path: str | Path) -> Policy:
    return Policy.model_validate(yaml.safe_load(Path(path).read_text()))
