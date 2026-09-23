"""The tool contract: bounded, summarised, deterministic, errors as data."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from investigator.state import SignalType


def parse_ts(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class ToolResult(BaseModel):
    ok: bool
    summary: str
    data: dict = {}
    raw_ref: str = ""
    query: dict = {}
    error: Optional[str] = None


class ToolArgs(BaseModel):
    """Base for tool arguments. Unknown arguments are rejected, not ignored."""

    model_config = ConfigDict(extra="forbid")


class RawStore(Protocol):
    def put(self, tool: str, args: dict, payload: Any) -> str: ...
    def get(self, ref: str) -> Any: ...


class InMemoryRawStore:
    def __init__(self) -> None:
        self._items: dict[str, Any] = {}

    def put(self, tool: str, args: dict, payload: Any) -> str:
        ref = _ref_for(tool, args)
        self._items[ref] = payload
        return ref

    def get(self, ref: str) -> Any:
        return self._items[ref]


class DirRawStore:
    """Stores full tool results as JSON files, outside the graph state."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, tool: str, args: dict, payload: Any) -> str:
        ref = _ref_for(tool, args)
        (self.root / f"{ref}.json").write_text(json.dumps(payload, indent=2, default=str))
        return ref

    def get(self, ref: str) -> Any:
        return json.loads((self.root / f"{ref}.json").read_text())


def _ref_for(tool: str, args: dict) -> str:
    digest = hashlib.sha256(json.dumps([tool, args], sort_keys=True, default=str).encode())
    return f"{tool}-{digest.hexdigest()[:12]}"


@dataclass
class ToolContext:
    """Per-investigation limits every tool enforces."""

    namespace: str
    window_start: datetime
    window_end: datetime
    change_window_start: Optional[datetime] = None  # set once the change search widens
    raw_store: RawStore = field(default_factory=InMemoryRawStore)

    def clamp(self, start: Optional[str], end: Optional[str], *, change: bool = False) -> tuple[datetime, datetime]:
        lower = self.change_window_start if (change and self.change_window_start) else self.window_start
        s = max(parse_ts(start), lower) if start else lower
        e = min(parse_ts(end), self.window_end) if end else self.window_end
        if s >= e:
            raise ValueError("time window is empty after clamping to the investigation window")
        return s, e

    def widen_changes(self, hours: int) -> None:
        self.change_window_start = self.window_end - timedelta(hours=hours)


ToolFn = Callable[[ToolContext, Any], ToolResult]


@dataclass
class Tool:
    name: str
    signal: SignalType
    description: str
    args_model: type[ToolArgs]
    fn: ToolFn

    def describe(self) -> dict:
        return {
            "name": self.name,
            "signal": self.signal,
            "description": self.description,
            "args": self.args_model.model_json_schema().get("properties", {}),
        }


class ToolRegistry:
    def __init__(self, tools: list[Tool]) -> None:
        self._tools = {t.name: t for t in tools}

    def names(self) -> list[str]:
        return list(self._tools)

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def describe(self) -> list[dict]:
        return [t.describe() for t in self._tools.values()]

    def call(self, ctx: ToolContext, name: str, args: dict) -> ToolResult:
        """Run a tool. Never raises: every failure comes back as a result."""
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(ok=False, summary=f"Unknown tool '{name}'.", query=args, error="unknown_tool")
        try:
            parsed = tool.args_model(**args)
        except ValidationError as exc:
            return ToolResult(
                ok=False,
                summary=f"Invalid arguments for {name}: {_short(exc)}",
                query=args,
                error="invalid_arguments",
            )
        try:
            result = tool.fn(ctx, parsed)
        except Exception as exc:  # errors are data, not crashes
            return ToolResult(ok=False, summary=f"{name} failed: {exc}", query=args, error=type(exc).__name__)
        if not result.query:
            result.query = args
        return result


def _short(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()[:3])
