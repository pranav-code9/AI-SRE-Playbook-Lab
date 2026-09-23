"""Checks on the agent itself.

    sre-obs overlaps --audit .sre-policy/audit.jsonl --live [--namespace otel-demo]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from sre_obs.overlap import find_overlaps


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sre-obs", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("overlaps")
    o.add_argument("--audit", required=True)
    src = o.add_mutually_exclusive_group(required=True)
    src.add_argument("--scenario")
    src.add_argument("--live", action="store_true")
    o.add_argument("--namespace", default="otel-demo")
    o.add_argument("--window-minutes", type=int, default=30)
    a = p.parse_args(argv if argv is not None else sys.argv[1:])

    entries = [json.loads(line) for line in Path(a.audit).read_text().splitlines() if line.strip()]
    executed = [e for e in entries if e["event"] == "executed"]
    if not executed:
        print("no executions in the audit log")
        return 0
    times = [datetime.fromisoformat(e["at"]) for e in executed]
    start, end = min(times) - timedelta(minutes=a.window_minutes), max(times) + timedelta(minutes=a.window_minutes)
    if a.scenario:
        from investigator.tools.fixture import fixture_backends, load_scenario

        changes = fixture_backends(load_scenario(a.scenario)).changes
    else:
        from investigator.tools.live import HelmChanges

        changes = HelmChanges()
    found = find_overlaps(entries, changes.list(a.namespace, start, end), a.window_minutes)
    for f in found:
        print(f"{f.release}: our execution {f.our_execution} at {f.our_at[:19]} and {f.other_change} "
              f"at {f.other_at[:19]} ({f.minutes_apart:+} min)")
    print(f"{len(found)} overlap(s)")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
