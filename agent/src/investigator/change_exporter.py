"""A small service that reads Helm history so the agent doesn't have to.

Helm stores release history in Kubernetes Secrets, and RBAC can't limit read
access to Helm's Secrets alone: a role that can read them can read every
Secret in the namespace, including the application's passwords. So the agent
never gets that permission. This exporter runs under its own service account
(lab/k8s/rbac.yaml), reads Helm history and rollout history, redacts secret
values, and serves only what the change tools need:

    GET /changes?namespace=&start=&end=           releases and rollouts in a window
    GET /values?namespace=&release=&revision=     one revision's values, flattened and redacted

    sre-change-exporter --port 8099
    investigate --live ... --changes-url http://localhost:8099
"""

from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from investigator.safety import config_value, untrusted
from investigator.tools.base import parse_ts


def _flatten(value, prefix=""):
    from investigator.tools.catalog import _flatten as flatten
    return flatten(value, prefix)


def redacted_values(values: dict) -> dict:
    return {k: config_value(k, v) for k, v in _flatten(values).items()}


def handler_for(changes):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            try:
                if url.path == "/changes":
                    body = [{**c, "description": untrusted(c.get("description", ""), 120)}
                            for c in changes.list(q["namespace"], parse_ts(q["start"]), parse_ts(q["end"]))]
                elif url.path == "/values":
                    body = redacted_values(changes.values(q["namespace"], q["release"], int(q["revision"])))
                else:
                    self.send_error(404)
                    return
            except KeyError as exc:
                self.send_error(404, str(exc))
                return
            except Exception as exc:  # report, don't crash the server
                self.send_error(500, type(exc).__name__)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    return Handler


class HttpChanges:
    """ChangesBackend client for the exporter: the agent's live change source."""

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")

    def _get(self, path, params):
        import httpx

        r = httpx.get(f"{self.base}{path}", params=params, timeout=15)
        if r.status_code == 404:
            raise KeyError(r.text[:200])
        r.raise_for_status()
        return r.json()

    def list(self, namespace, start, end):
        from investigator.tools.base import fmt_ts
        return self._get("/changes", {"namespace": namespace, "start": fmt_ts(start), "end": fmt_ts(end)})

    def values(self, namespace, release, revision):
        return self._get("/values", {"namespace": namespace, "release": release, "revision": revision})


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="sre-change-exporter", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8099)
    a = p.parse_args(argv if argv is not None else sys.argv[1:])
    from investigator.tools.live import HelmChanges

    ThreadingHTTPServer((a.host, a.port), handler_for(HelmChanges())).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
