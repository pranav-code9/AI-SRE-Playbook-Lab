"""Detect automations fighting over the same thing.

An action executed through the trust ladder is recorded in the audit log.
Any other change to the same release close to it (a deploy, another
controller's sync, a second automation's rollback) is a sign two actors are
working on the same system at once. That's worth a human's attention even
when each action was reasonable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable


@dataclass
class Overlap:
    release: str
    our_execution: str
    our_at: str
    other_change: str
    other_at: str
    minutes_apart: float


def _ours(change: dict, execution: dict) -> bool:
    """Helm records our rollback as a new revision described 'Rollback to N'."""
    target = execution["data"].get("plan", {}).get("target_revision")
    return f"Rollback to {target}" in (change.get("description") or "")


def find_overlaps(audit_entries: Iterable[dict], changes: list[dict], window_minutes: int = 30) -> list[Overlap]:
    window = timedelta(minutes=window_minutes)
    executions = [e for e in audit_entries if e["event"] == "executed"]
    found = []
    for ex in executions:
        release = ex["data"].get("plan", {}).get("release")
        at = datetime.fromisoformat(ex["at"])
        for c in changes:
            if c["release"] != release or _ours(c, ex):
                continue
            when = datetime.fromisoformat(c["updated"].replace("Z", "+00:00"))
            if abs(when - at) <= window:
                found.append(Overlap(release, ex["data"]["execution_id"], ex["at"],
                                     f"revision {c['revision']} ({c.get('description', '')})", c["updated"],
                                     round((when - at).total_seconds() / 60, 1)))
    for i, a in enumerate(executions):
        for b in executions[i + 1:]:
            ra, rb = a["data"].get("plan", {}).get("release"), b["data"].get("plan", {}).get("release")
            gap = datetime.fromisoformat(b["at"]) - datetime.fromisoformat(a["at"])
            if ra == rb and a["actor"] != b["actor"] and abs(gap) <= window:
                found.append(Overlap(ra, a["data"]["execution_id"], a["at"],
                                     f"execution {b['data']['execution_id']} by {b['actor']}", b["at"],
                                     round(gap.total_seconds() / 60, 1)))
    return found
