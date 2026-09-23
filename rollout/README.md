# Rolling out to on-call (Chapter 8)

| Path | What it holds |
|---|---|
| `rollout-checklist.md` | The 90-day plan: phases, exit criteria, measures, reasons to pause |
| `postmortem-template.md` | The blameless template the agent's draft fills in |
| `src/sre_rollout/shadow.py` | Shadow mode: record the agent's and responders' conclusions, grade one against the other |
| `src/sre_rollout/postmortem.py` | Draft a blameless postmortem from the investigation and the audit log |
| `src/sre_rollout/impact.py` | Measures beyond MTTR: agreement, speed, approval latency, overrides, outcomes |
| `scenarios/` | The case study a month later (revision 7), with recovery data after the rollback |
| `examples/` | Output of the offline demo: postmortem draft, shadow report, impact report |

## Install and run

```bash
pip install -e ../agent -e ../evals -e ../policies -e ../mcp-server
pip install -e ".[dev]"
pytest                                   # includes an end-to-end test of the whole flow
python -m sre_rollout.demo examples/     # regenerate the examples
```

## Commands

```bash
sre-rollout shadow record-agent runs/<ts>/state.json --reported-at <ISO>
sre-rollout shadow record-human <alert id> --ground-truth gt.json --diagnosed-at <ISO>
sre-rollout shadow report
sre-rollout postmortem runs/<ts>/state.json --audit .sre-policy/audit.jsonl --title "..." --out postmortem.md
sre-rollout impact --audit .sre-policy/audit.jsonl
sre-rollout recurrence                   # regenerate scenarios/
```

The ground-truth file uses the Chapter 6 format, so a shadowed incident can go
straight into the evaluation library.
