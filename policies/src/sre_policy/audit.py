"""Append-only, hash-chained audit log.

Each entry records the hash of the one before it, so editing or deleting any
past entry breaks the chain and `verify()` says where. It's tamper-evident,
not tamper-proof: ship the file somewhere append-only for real use.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

GENESIS = "0" * 64


def _hash(entry: dict) -> str:
    body = {k: v for k, v in entry.items() if k != "hash"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def entries(self) -> Iterator[dict]:
        with self.path.open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)

    def _last(self) -> Optional[dict]:
        last = None
        for last in self.entries():
            pass
        return last

    def append(self, event: str, actor: str, action: str, data: dict, at: Optional[datetime] = None) -> dict:
        last = self._last()
        entry = {
            "seq": (last["seq"] + 1) if last else 1,
            "at": (at or datetime.now(tz=timezone.utc)).isoformat(),
            "event": event,
            "actor": actor,
            "action": action,
            "data": data,
            "prev_hash": last["hash"] if last else GENESIS,
        }
        entry["hash"] = _hash(entry)
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
        return entry

    def verify(self) -> list[str]:
        problems, prev, expected_seq = [], GENESIS, 1
        for e in self.entries():
            if e.get("seq") != expected_seq:
                problems.append(f"entry {e.get('seq')}: expected sequence {expected_seq}")
            if e.get("prev_hash") != prev:
                problems.append(f"entry {e.get('seq')}: chain broken (previous entry changed or missing)")
            if _hash(e) != e.get("hash"):
                problems.append(f"entry {e.get('seq')}: contents changed after it was written")
            prev, expected_seq = e.get("hash"), expected_seq + 1
        return problems

    def render(self) -> str:
        lines = []
        for e in self.entries():
            d = e["data"]
            if e["event"] == "decision":
                detail = f"{d.get('summary', '')}: {d['outcome']} ({'; '.join(d.get('reasons', []))})"
            elif e["event"] == "outcome":
                detail = f"{d['execution_id']}: {d['outcome']} ({d.get('reason', '')})"
            elif e["event"] == "executed":
                detail = f"{d.get('summary', '')} (execution {d['execution_id']})"
            else:
                detail = d.get("reason") or d.get("summary") or ""
            lines.append(f"{e['seq']:>4} {e['at'][:19]}Z {e['event']:<18} {e['action']:<18} {e['actor']:<22} {detail}")
        return "\n".join(lines)
