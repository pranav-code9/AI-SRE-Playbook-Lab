# MCP servers (Chapter 4)

Two Model Context Protocol servers that expose the investigation tools, the
team's runbooks, and a gated rollback to any MCP client: an IDE assistant, a
chat client, or another agent.

| Server | Tools | Credentials |
|---|---|---|
| `sre-read` | The agent's twelve read tools, plus one tool per runbook | Read-only |
| `sre-actions` | `rollback_release` (plans by default; execution refused) | Separate, write access to the release |

The split is the point. A client connected only to `sre-read` can't change
anything, whatever a model asks for, because no write tool exists there.

## Install

```bash
pip install -e ../agent -e ../policies
pip install -e ".[dev]"
pytest
```

## Run

Offline, against the recorded case study:

```bash
sre-mcp read --scenario ../agent/scenarios/checkout_retry_storm.json
sre-mcp actions --scenario ../agent/scenarios/checkout_retry_storm.json
```

Against the lab (same port-forwards and checks as `agent/README.md`):

```bash
sre-mcp read --live --window-minutes 60
sre-mcp actions --live
```

Both use stdio by default. Add `--transport http --port 8765` to serve
Streamable HTTP at `http://127.0.0.1:8765/mcp` instead.

### Connecting a client

Most MCP clients take a JSON configuration along these lines; check your
client's documentation for where it goes:

```json
{
  "mcpServers": {
    "sre-read": {
      "command": "sre-mcp",
      "args": ["read", "--scenario", "/path/to/agent/scenarios/checkout_retry_storm.json"]
    }
  }
}
```

Connect `sre-actions` separately, and only where you want write tools offered.

## Runbooks

`runbooks/` holds each runbook twice: the page people read (`.md`) and its
encoded form (`.yaml`), versioned together. Each step is a `diagnosis` (a read
tool call), an `action` (a write tool, never run from a runbook), or a
`judgment` (stays with a human).

```bash
sre-mcp check      # fails if a runbook names a missing tool or stale arguments
```

The same check runs in `tests/test_runbooks.py`, so a tool change that breaks a
runbook fails CI.

## Execution

`rollback_release` always returns a plan: current and target revision, the
exact values that would change, and the command. What happens with
`dry_run=false` depends on how the server was started:

- **With `--policy`** (Chapter 5): the trust ladder decides. At the `approve`
  rung the server also offers `request_rollback_approval`, and a rollback runs
  only with an approval of that exact plan. See `../policies/README.md`.
- **Without a policy:** refused, unless the lab-only `--allow-execute` switch
  is on. Never use that switch outside the lab.

`sre-mcp propose`, `execute` and `verify` route the agent's structured action
proposals through the same policy.
