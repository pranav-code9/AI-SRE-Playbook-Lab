"""Write tools. Chapter 4 builds them and keeps them from running.

`rollback_release` always produces a plan: the current and target revision,
the chart and app versions of each, the values that would change, and which
rendered resources would change. A Helm rollback restores the whole earlier
release, not just its values, so the plan shows all three. Whether it executes
is decided by the trust ladder (Chapter 5).
"""

from __future__ import annotations

import json
import subprocess
from typing import Optional, Protocol

from pydantic import Field

from investigator.tools.base import ToolArgs, ToolContext, ToolRegistry, ToolResult


class ActionsBackend(Protocol):
    def current_revision(self, namespace: str, release: str) -> int: ...
    def release_info(self, namespace: str, release: str, revision: int) -> dict: ...  # {"chart", "app_version"}
    def manifest(self, namespace: str, release: str, revision: int) -> str: ...
    def rollback(self, namespace: str, release: str, revision: int) -> str: ...


def manifest_changes(before: str, after: str) -> list[str]:
    """Rendered resources that differ between two manifests, as Kind/name."""
    import yaml

    def index(text):
        docs = {}
        for d in yaml.safe_load_all(text or ""):
            if isinstance(d, dict) and d.get("kind"):
                docs[f"{d['kind']}/{d.get('metadata', {}).get('name', '?')}"] = d
        return docs

    a, b = index(before), index(after)
    changed = [k for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]
    return [k + ("" if k in a and k in b else " (added)" if k in b else " (removed)") for k in changed]


class HelmActions:
    """Live backend. Needs write access to the release, so it runs under
    different credentials from the read server."""

    def current_revision(self, namespace, release):
        out = subprocess.run(["helm", "status", release, "-n", namespace, "-o", "json"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300])
        return int(json.loads(out.stdout)["version"])

    def release_info(self, namespace, release, revision):
        out = subprocess.run(["helm", "history", release, "-n", namespace, "-o", "json"],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300])
        h = next((h for h in json.loads(out.stdout) if int(h["revision"]) == revision), None)
        if h is None:
            raise KeyError(f"no revision {revision} of {release}")
        return {"chart": h.get("chart"), "app_version": h.get("app_version")}

    def manifest(self, namespace, release, revision):
        out = subprocess.run(["helm", "get", "manifest", release, "-n", namespace, "--revision", str(revision)],
                             capture_output=True, text=True, timeout=30)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300])
        return out.stdout

    def rollback(self, namespace, release, revision):
        out = subprocess.run(["helm", "rollback", release, str(revision), "-n", namespace, "--wait"],
                             capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            raise RuntimeError(out.stderr.strip()[:300])
        return out.stdout.strip()


class RecordingActions:
    """Scenario backend: knows the current revision and records rollbacks
    instead of performing them."""

    def __init__(self, scenario: dict) -> None:
        self.revisions = {}
        self.entries = {(r["release"], r["revision"]): r for r in scenario.get("changes", [])}
        for r in scenario.get("changes", []):
            self.revisions[r["release"]] = max(self.revisions.get(r["release"], 0), r["revision"])
        self.performed: list[tuple[str, str, int]] = []

    def current_revision(self, namespace, release):
        if release not in self.revisions:
            raise KeyError(f"no release named {release}")
        return self.revisions[release]

    def release_info(self, namespace, release, revision):
        r = self.entries.get((release, revision))
        if r is None:
            raise KeyError(f"no revision {revision} of {release}")
        return {"chart": r.get("chart"), "app_version": r.get("app_version")}

    def manifest(self, namespace, release, revision):
        r = self.entries.get((release, revision))
        if r is None or "manifest" not in r:
            raise KeyError(f"no manifest for revision {revision} of {release}")
        return r["manifest"]

    def rollback(self, namespace, release, revision):
        self.performed.append((namespace, release, revision))
        return f"recorded rollback of {release} to revision {revision}"


class RollbackArgs(ToolArgs):
    release: str
    to_revision: int = Field(ge=1)
    reason: str = Field(min_length=10, description="Why, citing the evidence; kept in the audit trail")
    namespace: Optional[str] = None
    dry_run: bool = True
    approval_id: Optional[str] = Field(None, description="An approval granted for this exact plan")
    agent_confidence: Optional[float] = Field(None, ge=0, le=1, description="The agent's confidence in the diagnosis")
    change_age_minutes: Optional[float] = Field(None, ge=0, description="How long ago the release being undone landed")


class RequestApprovalArgs(ToolArgs):
    release: str
    to_revision: int = Field(ge=1)
    reason: str = Field(min_length=10, description="Why, citing the evidence; shown to the approver")
    namespace: Optional[str] = None
    agent_confidence: Optional[float] = Field(None, ge=0, le=1)
    change_age_minutes: Optional[float] = Field(None, ge=0)


class _RollbackBase:
    def __init__(self, backend: ActionsBackend, read_registry: ToolRegistry, allow_execute: bool = False,
                 gate=None, actor: str = "agent", notify=None) -> None:
        self.backend, self.read, self.allow_execute = backend, read_registry, allow_execute
        self.gate, self.actor, self.notify = gate, actor, notify

    def plan(self, ctx: ToolContext, a) -> tuple[Optional[dict], str, dict]:
        """(plan, summary, query). plan is None when the request is invalid."""
        ns = a.namespace or ctx.namespace
        query = a.model_dump(exclude_none=True)
        current = self.backend.current_revision(ns, a.release)
        if a.to_revision >= current:
            return None, f"{a.release} is at revision {current}; rollback target must be earlier.", query
        diff = self.read.call(ctx, "diff_release", {"release": a.release, "revision_a": current,
                                                    "revision_b": a.to_revision, "namespace": ns})
        try:
            now_info = self.backend.release_info(ns, a.release, current)
            then_info = self.backend.release_info(ns, a.release, a.to_revision)
        except (KeyError, RuntimeError):
            now_info = then_info = None
        try:
            resources = manifest_changes(self.backend.manifest(ns, a.release, current),
                                         self.backend.manifest(ns, a.release, a.to_revision))
        except (KeyError, RuntimeError):
            resources = None  # unknown: the policy treats an unknown blast radius as too large
        plan = {
            "release": a.release, "namespace": ns, "current_revision": current, "target_revision": a.to_revision,
            "chart": {"current": now_info and now_info["chart"], "target": then_info and then_info["chart"]},
            "app_version": {"current": now_info and now_info["app_version"], "target": then_info and then_info["app_version"]},
            "changes": diff.data.get("diffs", []) if diff.ok else None,
            "resources_changed": resources,
            "command": f"helm rollback {a.release} {a.to_revision} -n {ns} --wait",
        }
        parts = [f"Plan: roll {a.release} back from revision {current} to {a.to_revision}."]
        if plan["chart"]["current"] != plan["chart"]["target"]:
            parts.append(f"Chart changes: {plan['chart']['current']} \u2192 {plan['chart']['target']}.")
        else:
            parts.append(f"Chart unchanged ({plan['chart']['current']}).")
        parts.append(diff.summary)
        parts.append("Resources changed: " + (", ".join(resources) if resources else "unknown" if resources is None else "none") + ".")
        return plan, " ".join(parts), query

    def request(self, a, plan: dict):
        from sre_policy import ActionRequest

        return ActionRequest(action=Rollback.name, plan=plan, actor=self.actor, reason=a.reason,
                             approval_id=getattr(a, "approval_id", None), agent_confidence=a.agent_confidence,
                             change_age_minutes=a.change_age_minutes)


def _policy(d) -> dict:
    return {"outcome": d.outcome, "rung": d.rung, "reasons": d.reasons, "checks": d.checks}


class Rollback(_RollbackBase):
    name = "rollback_release"
    description = (
        "Roll a Helm release back to an earlier revision. Always returns the plan: current and "
        "target revision and the values that would change. dry_run=false asks to execute; the trust "
        "ladder decides whether it runs, needs an approval, or is refused."
    )
    args_model = RollbackArgs

    def __call__(self, ctx: ToolContext, a: RollbackArgs) -> ToolResult:
        plan, summary, query = self.plan(ctx, a)
        if plan is None:
            return ToolResult(ok=False, summary=summary, query=query, error="invalid_target")
        if a.dry_run:
            return ToolResult(ok=True, summary=summary + " Dry run: nothing changed.",
                              data={"plan": plan, "executed": False}, query=query)

        if self.gate is None:
            if not self.allow_execute:
                return ToolResult(
                    ok=False,
                    summary=summary + " Not executed: this server has no trust ladder and doesn't allow execution.",
                    data={"plan": plan, "executed": False}, query=query, error="execution_not_allowed",
                )
            output = self.backend.rollback(plan["namespace"], a.release, a.to_revision)
            return ToolResult(ok=True, summary=summary + f" Executed: {output}", data={"plan": plan, "executed": True}, query=query)

        req = self.request(a, plan)
        d = self.gate.evaluate(req)
        data = {"plan": plan, "executed": False, "policy": _policy(d)}
        if d.outcome != "allowed":
            hint = {
                "needs_approval": " Call request_rollback_approval, then retry with the approval_id once it's granted.",
                "suggest_only": f" At this rung a human runs it if they agree: {plan['command']}",
                "shadow": "",
                "denied": "",
            }[d.outcome]
            return ToolResult(ok=False, summary=f"{summary} Not executed ({d.outcome}): {'; '.join(d.reasons)}.{hint}",
                              data=data, query=query, error=d.outcome)
        if self.backend.current_revision(plan["namespace"], a.release) != plan["current_revision"]:
            # Someone deployed between the decision and now: the approved plan no longer describes reality.
            return ToolResult(ok=False, summary=f"{summary} Not executed: the release moved on since this plan was made.",
                              data=data, query=query, error="plan_stale")
        output = self.backend.rollback(plan["namespace"], a.release, a.to_revision)
        execution_id = self.gate.record_execution(req, output)
        data.update(executed=True, execution_id=execution_id)
        return ToolResult(ok=True, summary=f"{summary} Executed ({'; '.join(d.reasons)}): {output}. Execution {execution_id}.",
                          data=data, query=query)


class RequestRollbackApproval(_RollbackBase):
    name = "request_rollback_approval"
    description = (
        "Ask a human approver to approve a rollback plan. Returns an approval id; the approver sees "
        "the exact values that will change. Changes nothing in the system."
    )
    args_model = RequestApprovalArgs
    destructive = False

    def __call__(self, ctx: ToolContext, a: RequestApprovalArgs) -> ToolResult:
        plan, summary, query = self.plan(ctx, a)
        if plan is None:
            return ToolResult(ok=False, summary=summary, query=query, error="invalid_target")
        req = self.request(a, plan)
        d = self.gate.evaluate(req)
        data = {"plan": plan, "policy": _policy(d)}
        if d.outcome != "needs_approval":
            return ToolResult(ok=False, summary=f"{summary} No approval requested ({d.outcome}): {'; '.join(d.reasons)}.",
                              data=data, query=query, error=d.outcome)
        approval = self.gate.request_approval(req)
        if self.notify:
            self.notify(approval)
        data["approval_id"] = approval.id
        return ToolResult(ok=True, summary=f"{summary} Approval {approval.id} requested; expires {approval.expires_at[:16]}Z.",
                          data=data, query=query)
