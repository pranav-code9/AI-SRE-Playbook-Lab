"""Human side of the trust ladder.

    sre-policy check                         validate the policy file
    sre-policy pending                       approval requests waiting
    sre-policy approve <id> [--as cli:name]  approve a plan (deny works the same)
    sre-policy audit [--verify]              readable audit trail; check the hash chain
    sre-policy stop <action> --reason ...    kill switch for one action (resume undoes it)
    sre-policy outcome <execution> good|bad|reverted --reason ...
    sre-policy feedback <action> agree|disagree --reason ...   review a suggestion
    sre-policy readiness <action> [--eval-pass-rate 0.97]
    sre-policy reset-demotion <action> --reason ...
    sre-policy serve-slack --port 8787       Slack interactivity endpoint
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from datetime import datetime
from pathlib import Path

from sre_policy.gate import Gate
from sre_policy.model import load_policy
from sre_policy.readiness import readiness

DEFAULT_POLICY = Path(__file__).resolve().parents[2] / "trust-ladder.yaml"
DEFAULT_STATE = Path(os.environ.get("SRE_POLICY_STATE", Path.cwd() / ".sre-policy"))


def _parse(argv):
    p = argparse.ArgumentParser(prog="sre-policy", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--policy", default=str(DEFAULT_POLICY))
    p.add_argument("--state-dir", default=str(DEFAULT_STATE))
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--as", dest="identity", default=f"cli:{getpass.getuser()}", help="who is acting")
    common.add_argument("--now", help="ISO time to use as 'now' (replaying a recorded incident); default: real time")
    sub = p.add_subparsers(dest="cmd", required=True)
    _add = sub.add_parser
    sub.add_parser = lambda name, **kw: _add(name, parents=[common], **kw)
    sub.add_parser("check")
    sub.add_parser("pending")
    for name in ("approve", "deny"):
        s = sub.add_parser(name)
        s.add_argument("approval_id")
        s.add_argument("--note", default="")
    s = sub.add_parser("audit")
    s.add_argument("--verify", action="store_true")
    for name in ("stop", "resume", "reset-demotion"):
        s = sub.add_parser(name)
        s.add_argument("action")
        s.add_argument("--reason", required=True)
    s = sub.add_parser("outcome")
    s.add_argument("execution_id")
    s.add_argument("result", choices=["good", "bad", "reverted", "inconclusive"])
    s.add_argument("--reason", required=True)
    s = sub.add_parser("feedback")
    s.add_argument("action")
    s.add_argument("verdict", choices=["agree", "disagree"])
    s.add_argument("--reason", required=True)
    s = sub.add_parser("readiness")
    s.add_argument("action")
    s.add_argument("--eval-pass-rate", type=float)
    s = sub.add_parser("serve-slack")
    s.add_argument("--port", type=int, default=8787)
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = _parse(argv if argv is not None else sys.argv[1:])
    policy = load_policy(a.policy)
    clock = None
    if a.now:
        fixed = datetime.fromisoformat(a.now.replace("Z", "+00:00"))
        clock = lambda: fixed  # noqa: E731
    gate = Gate(policy, a.state_dir, clock=clock)

    if a.cmd == "check":
        problems = policy.problems()
        for name, ap in policy.actions.items():
            print(f"{name}: rung {ap.rung.label} (effective {gate.effective_rung(name).label})")
        for prob in problems:
            print(f"  - {prob}")
        return 1 if problems else 0
    if a.cmd == "pending":
        for ap in gate.pending():
            print(f"{ap.id}  {ap.action}  plan {ap.plan_hash}  by {ap.requested_by}  expires {ap.expires_at[:16]}Z  {ap.reason}")
        return 0
    if a.cmd in ("approve", "deny"):
        try:
            ap = gate.decide_approval(a.approval_id, a.identity, approve=(a.cmd == "approve"), note=a.note)
        except (KeyError, ValueError, PermissionError) as exc:
            print(f"Not recorded: {exc}", file=sys.stderr)
            return 1
        print(f"{ap.id} {ap.status} by {ap.decided_by}")
        return 0
    if a.cmd == "audit":
        if a.verify:
            problems = gate.audit.verify()
            print("audit chain intact" if not problems else "\n".join(problems))
            return 1 if problems else 0
        print(gate.audit.render())
        return 0
    if a.cmd == "stop":
        gate.stop(a.action, a.identity, a.reason)
    elif a.cmd == "resume":
        gate.resume(a.action, a.identity, a.reason)
    elif a.cmd == "reset-demotion":
        gate.reset_demotion(a.action, a.identity, a.reason)
    elif a.cmd == "outcome":
        demoted = gate.record_outcome(a.execution_id, a.result, a.identity, a.reason)
        if demoted is not None:
            print(f"demoted to {demoted.label}")
    elif a.cmd == "feedback":
        gate.audit.append("suggestion_reviewed", a.identity, a.action, {"verdict": a.verdict, "reason": a.reason}, gate.now())
    elif a.cmd == "readiness":
        for target, rows in readiness(gate, a.action, a.eval_pass_rate).items():
            ready = rows and all(r.ok for r in rows)
            print(f"to {target}: {'READY' if ready else 'not yet'}")
            for r in rows:
                shown = "n/a" if r.value is None else (f"{r.value:.2f}" if isinstance(r.value, float) else r.value)
                print(f"  {'ok ' if r.ok else '-- '} {r.name}: {shown} (needs {r.required})")
    elif a.cmd == "serve-slack":
        import uvicorn

        from sre_policy.slack import slack_app

        uvicorn.run(slack_app(gate, os.environ["SLACK_SIGNING_SECRET"]), host="127.0.0.1", port=a.port)
    print("ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
