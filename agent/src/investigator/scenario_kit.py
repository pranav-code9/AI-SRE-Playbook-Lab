"""Helpers for building recorded scenarios that look like the real thing.

Traces follow OpenTelemetry's shape: every call between services is a CLIENT
span in the caller with a SERVER span beneath it in the callee, and operation
names follow the demo's gRPC naming. Releases carry the chart version, the
app version and a rendered manifest, as `helm history` and `helm get manifest`
would return them.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import yaml

# Operation names follow the OpenTelemetry Demo's gRPC services. VERIFY
# against the pinned demo version: some services have moved to HTTP.
OPERATIONS = {
    "checkout": "oteldemo.CheckoutService/PlaceOrder",
    "cart": "oteldemo.CartService/GetCart",
    "currency": "oteldemo.CurrencyService/Convert",
    "payment": "oteldemo.PaymentService/Charge",
    "shipping": "oteldemo.ShippingService/ShipOrder",
    "email": "oteldemo.EmailService/SendOrderConfirmation",
}


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TraceBuilder:
    def __init__(self, trace_id: str, start: datetime) -> None:
        self.trace_id, self.start, self.spans, self._n = trace_id, start, [], 0

    def _id(self) -> str:
        self._n += 1
        return f"s{self._n}"

    def span(self, parent, service, op, offset_ms, dur, error=False, kind="internal", peer=None) -> str:
        sid = self._id()
        sp = {"span_id": sid, "parent_id": parent, "service": service, "operation": op,
              "start": ts(self.start + timedelta(milliseconds=offset_ms)), "duration_ms": dur,
              "error": error, "kind": kind}
        if peer:
            sp["peer"] = peer
        self.spans.append(sp)
        return sid

    def call(self, parent, caller, callee, offset_ms, dur, error=False, server_error=None) -> str:
        """One RPC: a CLIENT span in the caller and a SERVER span in the callee."""
        op = OPERATIONS[callee]
        client = self.span(parent, caller, op, offset_ms, dur, error, "client", peer=callee)
        self.span(client, callee, op, offset_ms + 1, max(1, dur - 2), error if server_error is None else server_error, "server")
        return client

    def done(self) -> dict:
        return {"trace_id": self.trace_id, "spans": self.spans}


def checkout_trace(trace_id: str, start: datetime, total_ms: float, error: bool) -> tuple[TraceBuilder, str]:
    """frontend -> checkout, returning the builder and checkout's server span id."""
    t = TraceBuilder(trace_id, start)
    root = t.span(None, "frontend", "POST /api/checkout", 0, total_ms + 20, error, "server")
    client = t.span(root, "frontend", OPERATIONS["checkout"], 5, total_ms + 5, error, "client", peer="checkout")
    server = t.span(client, "checkout", OPERATIONS["checkout"], 6, total_ms, error, "server")
    return t, server


def healthy_checkout(trace_id: str, start: datetime) -> dict:
    t, co = checkout_trace(trace_id, start, 190, False)
    t.call(co, "checkout", "cart", 10, 6)
    t.call(co, "checkout", "currency", 20, 2)
    t.call(co, "checkout", "payment", 30, 34)
    t.call(co, "checkout", "shipping", 70, 9)
    t.call(co, "checkout", "email", 90, 55)
    return t.done()


def retrying_checkout(trace_id: str, start: datetime, attempts: int, timeout_ms: float) -> dict:
    total = attempts * (timeout_ms + 5) + 60
    t, co = checkout_trace(trace_id, start, total, True)
    t.call(co, "checkout", "cart", 10, 7)
    t.call(co, "checkout", "currency", 20, 3)
    for i in range(attempts):
        # The client gives up at its deadline; the cancelled server span errors too.
        t.call(co, "checkout", "payment", 30 + (timeout_ms + 5) * i, timeout_ms, error=True)
    return t.done()


def failing_payment_checkout(trace_id: str, start: datetime) -> dict:
    t, co = checkout_trace(trace_id, start, 95, True)
    t.call(co, "checkout", "cart", 10, 6)
    t.call(co, "checkout", "currency", 20, 2)
    t.call(co, "checkout", "payment", 30, 28, error=True)
    return t.done()


def manifest(values: dict, chart: str, app_version: str) -> str:
    """A small rendered manifest: one Deployment per component with its env."""
    docs = []
    for name, comp in sorted(values.get("components", {}).items()):
        env = [{"name": e["name"], "value": e["value"]} for e in comp.get("envOverrides", [])]
        image = comp.get("imageOverride", {})
        docs.append({
            "apiVersion": "apps/v1", "kind": "Deployment",
            "metadata": {"name": name, "labels": {"helm.sh/chart": chart, "app.kubernetes.io/version": app_version}},
            "spec": {"template": {"spec": {"containers": [{
                "name": name,
                "image": f"{image.get('repository', 'ghcr.io/open-telemetry/demo')}:{image.get('tag', app_version)}",
                "env": env,
            }]}}},
        })
    docs.append({"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "flagd-config"},
                 "data": {"demo.flagd.json": "{}"}})
    return "---\n" + "\n---\n".join(yaml.safe_dump(d, sort_keys=True) for d in docs)
