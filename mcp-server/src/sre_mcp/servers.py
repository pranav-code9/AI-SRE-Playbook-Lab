"""Two MCP servers, split on purpose.

`sre-read` exposes the agent's twelve read tools and the runbooks. It runs with
read-only credentials. `sre-actions` exposes write tools and runs with separate,
narrower credentials. A client that only connects to `sre-read` can't change
anything, whatever the model asks for, because the tools don't exist there.
"""

from __future__ import annotations

from typing import Callable

import mcp.types as types
from mcp.server.lowlevel import Server
from pydantic import ValidationError

from investigator.tools.base import ToolContext, ToolRegistry, ToolResult
from sre_mcp.runbooks import Runbook, run_runbook

ContextFactory = Callable[[], ToolContext]

READ_ONLY = types.ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
DESTRUCTIVE = types.ToolAnnotations(read_only_hint=False, destructive_hint=True, idempotent_hint=False, open_world_hint=False)

READ_INSTRUCTIONS = (
    "Read-only SRE tools for the lab's Kubernetes namespace. Every tool is bounded to a recent "
    "time window and returns a short summary plus structured data. Runbook tools run a team "
    "runbook's diagnosis steps and list the decisions and actions it leaves to humans. "
    "Nothing on this server can change the system."
)
ACTIONS_INSTRUCTIONS = (
    "Write tools for the lab. Every action returns its plan first; dry_run defaults to true. "
    "Whether an action executes is decided by the trust ladder policy, not by the caller."
)


def _result(r: ToolResult) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=r.summary)],
        structured_content={"ok": r.ok, "data": r.data, "query": r.query, "raw_ref": r.raw_ref, "error": r.error},
        is_error=not r.ok,
    )


def _error(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=True)


def _schema(model) -> dict:
    schema = model.model_json_schema()
    schema.setdefault("properties", {})
    schema["type"] = "object"
    return schema


def runbook_schema(book: Runbook) -> dict:
    return {
        "type": "object",
        "properties": {k: {"type": "string", "default": v} for k, v in book.params.items()},
        "additionalProperties": False,
    }


def build_read_server(registry: ToolRegistry, runbooks: list[Runbook], context: ContextFactory) -> Server:
    books = {b.tool_name: b for b in runbooks}

    async def list_tools(ctx, params):
        tools = [
            types.Tool(
                name=spec["name"],
                description=f"[{spec['signal']}] {spec['description']}",
                input_schema=_schema(registry.get(spec["name"]).args_model),
                annotations=READ_ONLY,
            )
            for spec in registry.describe()
        ]
        tools += [
            types.Tool(
                name=b.tool_name,
                description=f"Run runbook {b.ref} ({b.title}): its diagnosis steps, plus the decisions and actions it leaves to humans.",
                input_schema=runbook_schema(b),
                annotations=READ_ONLY,
            )
            for b in books.values()
        ]
        return types.ListToolsResult(tools=tools)

    async def call_tool(ctx, params):
        args = params.arguments or {}
        if params.name in books:
            book = books[params.name]
            unknown = set(args) - set(book.params)
            if unknown:
                return _error(f"Unknown runbook parameters: {', '.join(sorted(unknown))}")
            return _result(run_runbook(book, registry, context(), args))
        return _result(registry.call(context(), params.name, args))

    return Server("sre-read", version="0.1.0", instructions=READ_INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool)


APPROVAL_REQUEST = types.ToolAnnotations(read_only_hint=False, destructive_hint=False, idempotent_hint=False, open_world_hint=False)


def build_actions_server(actions: list, context: ContextFactory) -> Server:
    by_name = {a.name: a for a in actions}

    async def list_tools(ctx, params):
        return types.ListToolsResult(tools=[
            types.Tool(name=a.name, description=a.description, input_schema=_schema(a.args_model),
                       annotations=DESTRUCTIVE if getattr(a, "destructive", True) else APPROVAL_REQUEST)
            for a in actions
        ])

    async def call_tool(ctx, params):
        action = by_name.get(params.name)
        if action is None:
            return _error(f"Unknown action '{params.name}'.")
        try:
            parsed = action.args_model(**(params.arguments or {}))
        except ValidationError as exc:
            return _error(f"Invalid arguments for {params.name}: {exc.errors()[0]['msg']}")
        try:
            return _result(action(context(), parsed))
        except Exception as exc:  # errors as data, on this server too
            return _error(f"{params.name} failed: {exc}")

    return Server("sre-actions", version="0.1.0", instructions=ACTIONS_INSTRUCTIONS,
                  on_list_tools=list_tools, on_call_tool=call_tool)
