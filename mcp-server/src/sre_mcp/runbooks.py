"""Runbooks as versioned, validated tool bundles.

A runbook YAML file lists steps of three kinds:
- diagnosis: a read tool call the server can run
- action: something that changes the system; listed, never run from a runbook
- judgment: a decision or check that stays with a human

Running a runbook executes its diagnosis steps and returns their findings,
with the judgment and action steps listed for the human.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from investigator.tools.base import ToolContext, ToolRegistry, ToolResult


class Step(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["diagnosis", "action", "judgment"]
    title: str
    tool: Optional[str] = None
    args: dict = Field(default_factory=dict)
    when: Optional[str] = None
    requires: Optional[str] = None
    guidance: Optional[str] = None


class Runbook(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    version: int
    title: str
    source: str
    alert: Optional[str] = None
    params: dict[str, str] = Field(default_factory=dict)
    steps: list[Step]

    @property
    def tool_name(self) -> str:
        return "runbook_" + self.name.replace("-", "_")

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"


def load_runbooks(directory: str | Path) -> list[Runbook]:
    books = []
    for path in sorted(Path(directory).glob("*.yaml")):
        books.append(Runbook.model_validate(yaml.safe_load(path.read_text())))
    return books


def render_args(args: dict, params: dict[str, str]) -> dict:
    out = {}
    for k, v in args.items():
        out[k] = v.format(**params) if isinstance(v, str) else v
    return out


def validate_runbook(book: Runbook, registry: ToolRegistry, action_tools: set[str], directory: Path) -> list[str]:
    """Problems that would make the runbook lie. Empty means it's consistent."""
    problems = []
    if not (directory / book.source).exists():
        problems.append(f"source page {book.source} is missing")
    ids = [s.id for s in book.steps]
    if len(ids) != len(set(ids)):
        problems.append("step ids are not unique")
    for s in book.steps:
        where = f"step '{s.id}'"
        if s.kind == "diagnosis":
            tool = registry.get(s.tool or "")
            if tool is None:
                problems.append(f"{where}: unknown read tool '{s.tool}'")
                continue
            try:
                tool.args_model(**render_args(s.args, book.params))
            except (ValidationError, KeyError) as exc:
                problems.append(f"{where}: arguments don't fit {s.tool}: {exc}")
        elif s.kind == "action":
            if s.tool and s.tool not in action_tools:
                problems.append(f"{where}: unknown action tool '{s.tool}'")
            if not s.requires:
                problems.append(f"{where}: actions must say what approval they require")
        elif s.tool:
            problems.append(f"{where}: judgment steps can't name a tool")
    return problems


def run_runbook(book: Runbook, registry: ToolRegistry, ctx: ToolContext, params: dict[str, str]) -> ToolResult:
    values = {**book.params, **{k: v for k, v in params.items() if v}}
    lines = [f"Runbook {book.ref}: {book.title}"]
    findings, pending = [], []
    ok = True
    for s in book.steps:
        if s.kind == "diagnosis":
            args = render_args(s.args, values)
            r = registry.call(ctx, s.tool, args)
            ok = ok and r.ok
            lines.append(f"[{s.id}] {s.title}: {r.summary}")
            findings.append({"step": s.id, "tool": s.tool, "args": args, "ok": r.ok,
                             "summary": r.summary, "raw_ref": r.raw_ref})
        else:
            pending.append({"step": s.id, "kind": s.kind, "title": s.title, "tool": s.tool,
                            "when": s.when, "requires": s.requires, "guidance": s.guidance})
    humans = [p for p in pending if p["kind"] == "judgment"]
    actions = [p for p in pending if p["kind"] == "action"]
    if humans:
        lines.append("For a human: " + "; ".join(p["title"] for p in humans) + ".")
    if actions:
        lines.append("Actions available, not run: " + "; ".join(
            f"{p['title']} (when: {(p['when'] or '').rstrip('.')}; requires: {p['requires']})" for p in actions) + ".")
    return ToolResult(
        ok=ok,
        summary="\n".join(lines),
        data={"runbook": book.ref, "findings": findings, "for_humans": humans, "actions": actions},
        query={"runbook": book.ref, "params": values},
    )
