"""Approvals in Slack: a message with the plan and two buttons, and an
endpoint for Slack's interactivity callback that checks the request really
came from Slack before recording anyone's decision.

Needs a Slack app with interactivity enabled and its request URL pointing at
/slack/interactions. Approver identities in the policy use `slack:<user id>`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional
from urllib.parse import parse_qs

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

from sre_policy.gate import Approval, Gate

MAX_SKEW_SECONDS = 300


def approval_message(a: Approval) -> dict:
    p = a.plan
    changes = p.get("changes") or []
    change_lines = "\n".join(f"• `{c['key']}`: `{c['before']}` → `{c['after']}`" for c in changes[:10]) or "• (no value changes)"
    return {
        "text": f"Approval needed: {a.action} {p.get('release', '')} {p.get('current_revision', '')} → {p.get('target_revision', '')}",
        "blocks": [
            {"type": "section", "text": {"type": "mrkdwn", "text":
                f"*Approval needed:* `{a.action}` on `{p.get('release')}` in `{p.get('namespace')}`\n"
                f"Revision {p.get('current_revision')} → {p.get('target_revision')}\n*Why:* {a.reason}"}},
            {"type": "section", "text": {"type": "mrkdwn", "text":
                "*What will change*\n" + change_lines + "\n" + _chart_line(p) + "\n" + _resources_line(p)}},
            {"type": "context", "elements": [{"type": "mrkdwn", "text":
                f"Requested by {a.requested_by} · plan `{a.plan_hash}` · expires {a.expires_at[:16]}Z · id `{a.id}`"}]},
            {"type": "actions", "elements": [
                {"type": "button", "style": "primary", "text": {"type": "plain_text", "text": "Approve"},
                 "action_id": "approve", "value": a.id},
                {"type": "button", "style": "danger", "text": {"type": "plain_text", "text": "Deny"},
                 "action_id": "deny", "value": a.id},
            ]},
        ],
    }


def _chart_line(p: dict) -> str:
    chart = p.get("chart") or {}
    if not chart:
        return "\u2022 Chart: not in plan"
    if chart.get("current") == chart.get("target"):
        return f"\u2022 Chart unchanged (`{chart.get('current')}`)"
    return f"\u2022 *Chart changes:* `{chart.get('current')}` \u2192 `{chart.get('target')}`"


def _resources_line(p: dict) -> str:
    r = p.get("resources_changed")
    if r is None:
        return "\u2022 Rendered resources changed: *unknown*"
    return "\u2022 Rendered resources changed: " + (", ".join(f"`{x}`" for x in r) if r else "none")


def post(webhook_url: str, message: dict) -> None:
    r = httpx.post(webhook_url, json=message, timeout=10)
    r.raise_for_status()


def verify_signature(signing_secret: str, timestamp: str, body: bytes, signature: str, now: Optional[float] = None) -> bool:
    """Slack's v0 request signature: HMAC-SHA256 over 'v0:<timestamp>:<body>'."""
    try:
        ts = int(timestamp)
    except (TypeError, ValueError):
        return False
    if abs((now or time.time()) - ts) > MAX_SKEW_SECONDS:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(signing_secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def handle_interaction(gate: Gate, payload: dict) -> str:
    action = (payload.get("actions") or [{}])[0]
    approval_id, choice = action.get("value"), action.get("action_id")
    user = "slack:" + (payload.get("user") or {}).get("id", "unknown")
    if choice not in ("approve", "deny") or not approval_id:
        return "Unrecognised action."
    try:
        a = gate.decide_approval(approval_id, user, approve=(choice == "approve"))
    except (KeyError, ValueError, PermissionError) as exc:
        return f"Not recorded: {exc}"
    return f"{'Approved' if a.status == 'approved' else 'Denied'} by <@{user.split(':', 1)[1]}>. Approval `{a.id}` is {a.status}."


def slack_app(gate: Gate, signing_secret: str) -> Starlette:
    async def interactions(request: Request):
        body = await request.body()
        if not verify_signature(signing_secret, request.headers.get("X-Slack-Request-Timestamp", ""),
                                body, request.headers.get("X-Slack-Signature", "")):
            return PlainTextResponse("invalid signature", status_code=401)
        payload = json.loads(parse_qs(body.decode())["payload"][0])
        return JSONResponse({"response_type": "in_channel", "replace_original": False,
                             "text": handle_interaction(gate, payload)})

    return Starlette(routes=[Route("/slack/interactions", interactions, methods=["POST"])])
