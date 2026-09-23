import json
import re
from pathlib import Path

import yaml

from sre_obs.overlap import find_overlaps

ROOT = Path(__file__).resolve().parents[1]


def executed(at, actor="cli:pranav", execution="ex1", release="otel-demo", target=2):
    return {"event": "executed", "at": at, "actor": actor,
            "data": {"execution_id": execution, "plan": {"release": release, "target_revision": target}}}


def change(rev, at, description="Upgrade complete", release="otel-demo"):
    return {"release": release, "revision": rev, "updated": at, "description": description}


def test_our_own_rollback_is_not_an_overlap():
    audit = [executed("2026-09-22T10:31:00+00:00")]
    changes = [change(4, "2026-09-22T10:31:20Z", "Rollback to 2")]
    assert find_overlaps(audit, changes) == []


def test_another_deploy_right_after_our_rollback_is_flagged():
    audit = [executed("2026-09-22T10:31:00+00:00")]
    changes = [change(4, "2026-09-22T10:31:20Z", "Rollback to 2"), change(5, "2026-09-22T10:40:00Z", "Upgrade complete")]
    [o] = find_overlaps(audit, changes)
    assert o.other_change == "revision 5 (Upgrade complete)" and o.minutes_apart == 9.0


def test_changes_outside_the_window_or_to_other_releases_are_ignored():
    audit = [executed("2026-09-22T10:31:00+00:00")]
    changes = [change(5, "2026-09-22T11:30:00Z"), change(9, "2026-09-22T10:35:00Z", release="ad-banner")]
    assert find_overlaps(audit, changes) == []


def test_two_actors_executing_on_one_release():
    audit = [executed("2026-09-22T10:31:00+00:00"), executed("2026-09-22T10:36:00+00:00", actor="agent", execution="ex2")]
    [o] = find_overlaps(audit, [])
    assert "execution ex2 by agent" in o.other_change


METRICS = {
    "sre_agent_investigations_total", "sre_agent_investigation_duration_seconds_bucket", "sre_agent_tool_calls_total",
    "sre_agent_guard_trips_total", "sre_agent_cost_usd_total", "gen_ai_client_token_usage_sum",
}
LABELS = {"gen_ai_tool_name", "gen_ai_token_type"}
RECORDED = re.compile(r"\bsre_agent:[a-z_0-9:]+")
NAMES = re.compile(r"\b(sre_agent_[a-z_]+|gen_ai_[a-z_]+)\b")


def test_rules_and_dashboard_use_the_same_metric_names():
    rules = yaml.safe_load((ROOT / "prometheus" / "sre-agent-rules.yaml").read_text())
    recorded = {r["record"] for g in rules["groups"] for r in g["rules"] if "record" in r}
    exprs = [r["expr"] for g in rules["groups"] for r in g["rules"]]
    dashboard = json.loads((ROOT / "grafana" / "sre-agent-dashboard.json").read_text())
    exprs += [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    for e in exprs:
        assert set(NAMES.findall(e)) <= METRICS | LABELS, e
        assert set(RECORDED.findall(e)) <= recorded, e


def test_alerts_link_to_runbook_sections():
    rules = yaml.safe_load((ROOT / "prometheus" / "sre-agent-rules.yaml").read_text())
    runbook = (ROOT / "runbooks" / "agent-misbehaving.md").read_text().lower()
    for g in rules["groups"]:
        for r in g["rules"]:
            link = r.get("annotations", {}).get("runbook")
            if link:
                anchor = link.split("#")[1].replace("-", " ")
                assert f"## {anchor}" in runbook, link
