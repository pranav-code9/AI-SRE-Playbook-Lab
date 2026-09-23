"""Shadow mode: the agent investigates every alert, humans investigate as
usual, and nobody acts on what the agent says. Afterwards, the human's
conclusion is recorded in the same ground-truth format as Chapter 6's
scenarios, and the agent's investigation is graded against it.

Each shadowed incident is also a candidate for the evaluation library.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Optional


class ShadowLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)

    def _append(self, entry: dict) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def entries(self) -> list[dict]:
        return [json.loads(line) for line in self.path.read_text().splitlines() if line.strip()]

    def record_agent(self, state: dict, reported_at: str) -> None:
        """What the agent concluded, and when its report was ready."""
        self._append({"kind": "agent", "alert_id": state["incident"]["alert_id"],
                      "alert_started_at": state["incident"]["alert_started_at"],
                      "reported_at": reported_at, "state": state})

    def record_human(self, alert_id: str, ground_truth: dict, diagnosed_at: str, notes: str = "") -> None:
        """What the responders concluded, as ground truth, and when they knew."""
        self._append({"kind": "human", "alert_id": alert_id, "ground_truth": ground_truth,
                      "diagnosed_at": diagnosed_at, "notes": notes})

    def pairs(self) -> list[tuple[dict, dict]]:
        agents = {e["alert_id"]: e for e in self.entries() if e["kind"] == "agent"}
        humans = {e["alert_id"]: e for e in self.entries() if e["kind"] == "human"}
        return [(agents[k], humans[k]) for k in agents if k in humans]


def _minutes(a: str, b: str) -> float:
    fa = datetime.fromisoformat(a.replace("Z", "+00:00"))
    fb = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return (fb - fa).total_seconds() / 60


def shadow_report(log: ShadowLog) -> dict:
    from sre_evals.grade import grade

    rows = []
    for agent, human in log.pairs():
        scenario = {"name": agent["alert_id"], "ground_truth": human["ground_truth"]}
        g = grade(scenario, agent["state"])
        rows.append({
            "alert_id": agent["alert_id"],
            "agreed": g.passed,
            "confidently_wrong": g.confidently_wrong,
            "outcome": g.outcome,
            "agent_minutes": round(_minutes(agent["alert_started_at"], agent["reported_at"]), 1),
            "human_minutes": round(_minutes(agent["alert_started_at"], human["diagnosed_at"]), 1),
            "failures": g.failures,
        })
    n = len(rows)
    faster = [r for r in rows if r["agreed"] and r["agent_minutes"] < r["human_minutes"]]
    return {
        "incidents": n,
        "agreement": (sum(r["agreed"] for r in rows) / n) if n else None,
        "confidently_wrong": sum(r["confidently_wrong"] for r in rows),
        "escalation_rate": (sum(r["outcome"] == "escalated" for r in rows) / n) if n else None,
        "median_agent_minutes": median(r["agent_minutes"] for r in rows) if rows else None,
        "median_human_minutes": median(r["human_minutes"] for r in rows) if rows else None,
        "agreed_and_faster": len(faster),
        "rows": rows,
    }


def render_shadow_report(r: dict, title: Optional[str] = "Shadow mode") -> str:
    if not r["incidents"]:
        return f"# {title}\n\nNo incidents with both an agent and a human conclusion yet.\n"
    lines = [
        f"# {title}", "",
        f"**Incidents:** {r['incidents']}  ",
        f"**Agent agreed with responders:** {r['agreement']:.0%}  ",
        f"**Confidently wrong:** {r['confidently_wrong']}  ",
        f"**Escalated:** {r['escalation_rate']:.0%}  ",
        f"**Median minutes to a conclusion:** agent {r['median_agent_minutes']}, responders {r['median_human_minutes']}  ",
        f"**Agreed and faster:** {r['agreed_and_faster']} of {r['incidents']}",
        "", "| Alert | Agreed | Outcome | Agent min | Responders min | Differences |", "|---|---|---|---|---|---|",
    ]
    for row in r["rows"]:
        lines.append(f"| {row['alert_id']} | {'yes' if row['agreed'] else 'no'} | {row['outcome']} | "
                     f"{row['agent_minutes']} | {row['human_minutes']} | {'; '.join(row['failures']) or '—'} |")
    return "\n".join(lines) + "\n"
