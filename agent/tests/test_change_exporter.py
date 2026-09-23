import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer

from investigator.change_exporter import HttpChanges, handler_for
from investigator.tools.fixture import fixture_backends


def test_exporter_serves_changes_and_redacted_values(scenario):
    scenario["changes"][1]["values"]["components"]["checkout"]["envOverrides"].append({"name": "DB_PASSWORD", "value": "hunter2"})
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(fixture_backends(scenario).changes))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = HttpChanges(f"http://127.0.0.1:{server.server_port}")
        start = datetime(2026, 9, 22, 9, 50, tzinfo=timezone.utc)
        end = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)
        assert [c["revision"] for c in client.list("otel-demo", start, end)] == [3]
        values = client.values("otel-demo", "otel-demo", 3)
        assert values["components.checkout.envOverrides[PAYMENT_TIMEOUT].value"] == "40ms"
        assert values["components.checkout.envOverrides[DB_PASSWORD].value"].startswith("[redacted:")
        assert "hunter2" not in str(values)
    finally:
        server.shutdown()
