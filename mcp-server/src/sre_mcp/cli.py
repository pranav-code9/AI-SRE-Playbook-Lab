"""Run an MCP server over stdio (for IDE assistants and local agents) or
Streamable HTTP.

    sre-mcp read --scenario ../agent/scenarios/checkout_retry_storm.json
    sre-mcp read --live
    sre-mcp actions --scenario ../agent/scenarios/checkout_retry_storm.json
    sre-mcp actions --live                   # plans only; execution refused
    sre-mcp actions --live --policy ../policies/trust-ladder.yaml   # the trust ladder decides
    sre-mcp check                            # validate every runbook

Chapter 5, the human and agent sides of the trust ladder:
    sre-mcp propose <agent run>/state.json --scenario ... --policy ...   # route the agent's proposals
    sre-mcp execute <approval id> --scenario ... --policy ...            # run an approved plan
    sre-mcp verify <execution id> --live --policy ...                    # did it work? demotes if not
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio

from investigator.tools.base import InMemoryRawStore, ToolContext, parse_ts
from investigator.tools.catalog import build_registry
from sre_mcp.actions import HelmActions, RecordingActions, RequestRollbackApproval, Rollback
from sre_mcp.runbooks import load_runbooks, validate_runbook
from sre_mcp.servers import build_actions_server, build_read_server

DEFAULT_RUNBOOKS = Path(__file__).resolve().parents[2] / "runbooks"


def _parse(argv):
    p = argparse.ArgumentParser(prog="sre-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("server", choices=["read", "actions", "check", "propose", "execute", "verify"])
    p.add_argument("target", nargs="?", help="propose: agent state.json; execute: approval id; verify: execution id")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--scenario", help="serve a recorded scenario (offline)")
    src.add_argument("--live", action="store_true", help="serve the running lab")
    p.add_argument("--namespace", default="otel-demo")
    p.add_argument("--window-minutes", type=int, default=60, help="live: how far back tools may look")
    p.add_argument("--prometheus-url", default="http://localhost:9090")
    p.add_argument("--jaeger-url", default="http://localhost:8080/jaeger/ui")
    p.add_argument("--runbooks", default=str(DEFAULT_RUNBOOKS))
    p.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--allow-execute", action="store_true",
                   help="actions without a policy: let dry_run=false execute. Lab only")
    p.add_argument("--policy", help="trust ladder policy file (Chapter 5)")
    p.add_argument("--state-dir", default=".sre-policy", help="trust ladder state: approvals, audit log, demotions")
    p.add_argument("--actor", default="agent", help="identity recorded for actions requested through this server")
    p.add_argument("--slack-webhook", help="post approval requests to this Slack incoming webhook")
    return p.parse_args(argv)


def _backends(a):
    if a.scenario:
        from investigator.tools.fixture import fixture_backends, load_scenario

        scenario = load_scenario(a.scenario)
        i = scenario["incident"]

        def context():
            return ToolContext(i["namespace"], parse_ts(i["window_start"]), parse_ts(i["window_end"]), raw_store=store)

        return fixture_backends(scenario), context, RecordingActions(scenario)

    from investigator.tools.live import live_backends

    def context():
        end = datetime.now(tz=timezone.utc)
        return ToolContext(a.namespace, end - timedelta(minutes=a.window_minutes), end, raw_store=store)

    return live_backends(a.prometheus_url, a.jaeger_url), context, HelmActions()


store = InMemoryRawStore()


def _gate(a):
    from sre_policy import Gate, load_policy

    policy = load_policy(a.policy)
    if a.scenario:
        # Replaying a recorded incident: the policy's clock is the incident's.
        from investigator.tools.fixture import load_scenario

        end = parse_ts(load_scenario(a.scenario)["incident"]["window_end"])
        return Gate(policy, a.state_dir, clock=lambda: end)
    return Gate(policy, a.state_dir)


def _now(a):
    if a.scenario:
        from investigator.tools.fixture import load_scenario

        return parse_ts(load_scenario(a.scenario)["incident"]["window_end"])
    return datetime.now(tz=timezone.utc)


def _notifier(a):
    from sre_policy.slack import approval_message, post

    def notify(approval):
        message = approval_message(approval)
        if a.slack_webhook:
            post(a.slack_webhook, message)
        else:
            print(f"[approval {approval.id}] {message['text']}", file=sys.stderr)

    return notify


def main(argv=None) -> int:
    a = _parse(argv if argv is not None else sys.argv[1:])
    runbooks_dir = Path(a.runbooks)

    if a.server == "check":
        from investigator.tools.fixture import fixture_backends

        registry = build_registry(fixture_backends({}))
        failed = False
        for book in load_runbooks(runbooks_dir):
            problems = validate_runbook(book, registry, {Rollback.name}, runbooks_dir)
            print(f"{book.ref}: {'ok' if not problems else 'PROBLEMS'}")
            for prob in problems:
                print(f"  - {prob}")
            failed = failed or bool(problems)
        return 1 if failed else 0

    if not (a.scenario or a.live):
        print("choose --scenario or --live", file=sys.stderr)
        return 2
    if a.policy and a.allow_execute:
        print("--allow-execute can't be combined with --policy; the policy decides", file=sys.stderr)
        return 2
    backends, context, actions_backend = _backends(a)
    registry = build_registry(backends)
    gate = _gate(a) if a.policy else None
    notify = _notifier(a) if a.policy else None

    if a.server in ("propose", "execute", "verify"):
        if not (gate and a.target):
            print(f"{a.server} needs a target and --policy", file=sys.stderr)
            return 2
        from sre_mcp import flows

        if a.server == "propose":
            return flows.propose(Path(a.target), gate, actions_backend, registry, context, notify, now=_now(a))
        if a.server == "execute":
            return flows.execute(a.target, gate, actions_backend, registry, context, actor=a.actor)
        return flows.verify(a.target, gate, backends)

    if a.server == "read":
        server = build_read_server(registry, load_runbooks(runbooks_dir), context)
    else:
        kwargs = dict(allow_execute=a.allow_execute, gate=gate, actor=a.actor, notify=notify)
        actions = [Rollback(actions_backend, registry, **kwargs)]
        if gate:
            actions.append(RequestRollbackApproval(actions_backend, registry, **kwargs))
        server = build_actions_server(actions, context)

    if a.transport == "http":
        import uvicorn

        uvicorn.run(server.streamable_http_app(), host="127.0.0.1", port=a.port)
        return 0

    from mcp.server.stdio import stdio_server

    async def serve():
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(serve)
    return 0


if __name__ == "__main__":
    sys.exit(main())
