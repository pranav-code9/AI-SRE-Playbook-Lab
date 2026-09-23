import sys

import anyio
from conftest import ROOT, RUNBOOKS, SCENARIO
from mcp import Client, StdioServerParameters

from sre_mcp.actions import RecordingActions, Rollback
from sre_mcp.runbooks import load_runbooks
from sre_mcp.servers import build_actions_server, build_read_server


def run(coro):
    return anyio.run(coro)


def read_server(registry, context):
    return build_read_server(registry, load_runbooks(RUNBOOKS), context)


def test_read_server_exposes_only_read_tools(registry, context):
    async def go():
        async with Client(read_server(registry, context)) as c:
            return (await c.list_tools()).tools

    tools = run(go)
    names = {t.name for t in tools}
    assert len(tools) == 13 and "runbook_checkout_errors" in names
    assert "rollback_release" not in names
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools)
    red = next(t for t in tools if t.name == "get_service_red")
    assert red.input_schema["required"] == ["service"]
    assert red.input_schema.get("additionalProperties") is False


def test_read_tools_return_summary_and_structure(registry, context):
    async def go():
        async with Client(read_server(registry, context)) as c:
            ok = await c.call_tool("diff_release", {"release": "otel-demo", "revision_a": 2, "revision_b": 3})
            bad = await c.call_tool("get_dependencies", {"service": "checkout", "depth": 9})
            book = await c.call_tool("runbook_checkout_errors", {})
            wrong = await c.call_tool("runbook_checkout_errors", {"region": "eu"})
            return ok, bad, book, wrong

    ok, bad, book, wrong = run(go)
    assert not ok.is_error and "'2s' → '40ms'" in ok.content[0].text
    assert ok.structured_content["data"]["diffs"][1]["after"] == "40ms"
    assert bad.is_error and "Invalid arguments" in bad.content[0].text
    assert not book.is_error and "checkout-errors@3" in book.content[0].text
    assert wrong.is_error


def test_rollback_plans_by_default_and_refuses_to_execute(scenario, registry, context):
    backend = RecordingActions(scenario)
    server = build_actions_server([Rollback(backend, registry)], context)
    reason = "Revision 3 cut the payment timeout (E13) and errors began after it (E16)"

    async def go():
        async with Client(server) as c:
            tools = (await c.list_tools()).tools
            plan = await c.call_tool("rollback_release", {"release": "otel-demo", "to_revision": 2, "reason": reason})
            execute = await c.call_tool("rollback_release", {"release": "otel-demo", "to_revision": 2, "reason": reason, "dry_run": False})
            same = await c.call_tool("rollback_release", {"release": "otel-demo", "to_revision": 3, "reason": reason})
            no_reason = await c.call_tool("rollback_release", {"release": "otel-demo", "to_revision": 2, "reason": "x"})
            return tools, plan, execute, same, no_reason

    tools, plan, execute, same, no_reason = run(go)
    [t] = tools
    assert t.annotations.destructive_hint and not t.annotations.read_only_hint
    assert not plan.is_error and "from revision 3 to 2" in plan.content[0].text and "Dry run" in plan.content[0].text
    changes = plan.structured_content["data"]["plan"]["changes"]
    assert {c["key"].split("[")[1].split("]")[0]: c["after"] for c in changes} == {"PAYMENT_MAX_RETRIES": "0", "PAYMENT_TIMEOUT": "2s"}
    assert execute.is_error and "doesn't allow execution" in execute.content[0].text
    assert same.is_error and "must be earlier" in same.content[0].text
    assert no_reason.is_error
    assert backend.performed == []


def test_execution_only_when_the_server_allows_it(scenario, registry, context):
    backend = RecordingActions(scenario)
    server = build_actions_server([Rollback(backend, registry, allow_execute=True)], context)

    async def go():
        async with Client(server) as c:
            return await c.call_tool("rollback_release", {
                "release": "otel-demo", "to_revision": 2, "reason": "lab test of the execution path", "dry_run": False})

    r = run(go)
    assert not r.is_error and r.structured_content["data"]["executed"]
    assert backend.performed == [("otel-demo", "otel-demo", 2)]


def test_stdio_server_end_to_end():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "sre_mcp.cli", "read", "--scenario", str(SCENARIO)],
        env={"PYTHONPATH": f"{ROOT / 'src'}:{ROOT.parent / 'agent' / 'src'}:{ROOT.parent / 'policies' / 'src'}"},
        cwd=str(ROOT),
    )

    async def go():
        async with Client(params) as c:
            tools = (await c.list_tools()).tools
            r = await c.call_tool("list_changes", {})
            return tools, r

    tools, r = run(go)
    assert len(tools) == 13
    assert "otel-demo revision 3" in r.content[0].text
