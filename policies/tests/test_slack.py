import hashlib
import hmac
import json
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from starlette.testclient import TestClient

from sre_policy import ActionRequest, Gate, load_policy
from sre_policy.slack import approval_message, slack_app, verify_signature
from test_gate import PLAN, POLICY

SECRET = "test-signing-secret"


def signed(body: bytes, ts: int):
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {"X-Slack-Request-Timestamp": str(ts), "X-Slack-Signature": sig,
            "Content-Type": "application/x-www-form-urlencoded"}


def setup(tmp_path):
    g = Gate(load_policy(POLICY), tmp_path, clock=lambda: datetime.now(tz=timezone.utc))
    a = g.request_approval(ActionRequest(action="rollback_release", plan=PLAN, actor="agent",
                                         reason="retry storm after revision 3"))
    return g, a


def test_message_shows_the_plan(tmp_path):
    _, a = setup(tmp_path)
    m = approval_message(a)
    text = json.dumps(m, ensure_ascii=False)
    assert "otel-demo" in text and "`40ms` → `2s`" in text and a.id in text
    assert "Chart unchanged" in text and "`Deployment/checkout`" in text
    assert [b["action_id"] for b in m["blocks"][-1]["elements"]] == ["approve", "deny"]


def test_signature_checks():
    body, now = b"payload=x", int(time.time())
    headers = signed(body, now)
    assert verify_signature(SECRET, headers["X-Slack-Request-Timestamp"], body, headers["X-Slack-Signature"])
    assert not verify_signature(SECRET, headers["X-Slack-Request-Timestamp"], b"payload=y", headers["X-Slack-Signature"])
    old = signed(body, now - 600)
    assert not verify_signature(SECRET, old["X-Slack-Request-Timestamp"], body, old["X-Slack-Signature"])


def test_button_click_approves_for_a_listed_approver_only(tmp_path):
    g, a = setup(tmp_path)
    client = TestClient(slack_app(g, SECRET))

    def click(user):
        payload = {"user": {"id": user}, "actions": [{"action_id": "approve", "value": a.id}]}
        body = urlencode({"payload": json.dumps(payload)}).encode()
        return client.post("/slack/interactions", content=body, headers=signed(body, int(time.time())))

    r = click("UINTRUDER")
    assert r.status_code == 200 and "Not recorded" in r.json()["text"]
    r = click("U000EXAMPLE")
    assert "Approved" in r.json()["text"]
    assert g.get_approval(a.id).status == "approved"


def test_unsigned_requests_are_rejected(tmp_path):
    g, _ = setup(tmp_path)
    r = TestClient(slack_app(g, SECRET)).post("/slack/interactions", content=b"payload={}",
                                              headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 401
