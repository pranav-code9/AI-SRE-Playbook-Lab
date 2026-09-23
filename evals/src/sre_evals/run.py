"""Run the agent over a set of scenarios, several times each, and summarise.

A single run of a model-driven agent is an anecdote. The runner repeats each
scenario (`trials`), grades every run, and reports pass rates with a
confidence interval, so a change of a few points can be told apart from noise.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from statistics import mean, median
from typing import Callable, Optional

from sre_evals.grade import Grade, grade

LLMFactory = Callable[[str], Optional[object]]   # scenario name -> StructuredLLM, or None to skip


def dump_state(final: dict) -> dict:
    def conv(v):
        if hasattr(v, "model_dump"):
            return v.model_dump()
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return {k: conv(v) for k, v in final.items()}


def run_one(scenario: dict, llm, structural_rules: bool = True) -> tuple[dict, float]:
    from investigator.graph import Deps, build_graph, initial_state
    from investigator.state import IncidentContext
    from investigator.tools.catalog import build_registry
    from investigator.tools.fixture import fixture_backends

    deps = Deps(build_registry(fixture_backends(scenario)), llm, structural_rules=structural_rules)
    started = time.perf_counter()
    final = build_graph(deps).invoke(initial_state(IncidentContext(**scenario["incident"])), {"recursion_limit": 100})
    return dump_state(final), time.perf_counter() - started


def run_suite(paths: list[Path], llm_for: LLMFactory, trials: int = 1, structural_rules: bool = True,
              progress: Callable[[str], None] = lambda _: None) -> dict:
    grades: list[Grade] = []
    skipped: list[str] = []
    for path in paths:
        scenario = json.loads(path.read_text())
        name = scenario.get("name", path.stem)
        for trial in range(trials):
            llm = llm_for(name)
            if llm is None:
                skipped.append(name)
                break
            try:
                state, seconds = run_one(scenario, llm, structural_rules)
                g = grade(scenario, state, seconds)
            except Exception as exc:  # a crash is a failed run, not a failed suite
                g = Grade(scenario=name, passed=False, outcome="error", outcome_ok=False, root_cause_ok=False,
                          mechanism_ok=None, actions_ok=False, grounded=False, key_evidence=0.0,
                          confidently_wrong=False, iterations=0, tool_calls=0, cost_usd=0.0, seconds=0.0,
                          failures=[f"run failed: {type(exc).__name__}: {exc}"])
            grades.append(g)
            progress(f"{name} trial {trial + 1}/{trials}: {'pass' if g.passed else 'FAIL'}")
    return summarise(grades, trials, skipped)


def wilson(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a pass rate. Honest about small samples."""
    if n == 0:
        return (0.0, 0.0)
    p = passes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarise(grades: list[Grade], trials: int, skipped: list[str]) -> dict:
    by: dict[str, list[Grade]] = {}
    for g in grades:
        by.setdefault(g.scenario, []).append(g)
    scenarios = {}
    for name, gs in by.items():
        passes = sum(g.passed for g in gs)
        scenarios[name] = {
            "runs": len(gs),
            "passes": passes,
            "pass_rate": passes / len(gs),
            "all_trials_pass": passes == len(gs),
            "confidently_wrong": sum(g.confidently_wrong for g in gs),
            "median_tool_calls": median(g.tool_calls for g in gs),
            "mean_iterations": mean(g.iterations for g in gs),
            "mean_cost_usd": mean(g.cost_usd for g in gs),
            "mean_seconds": mean(g.seconds for g in gs),
            "failures": sorted({f for g in gs for f in g.failures}),
        }
    n, passes = len(grades), sum(g.passed for g in grades)
    lo, hi = wilson(passes, n)
    return {
        "trials": trials,
        "runs": n,
        "pass_rate": passes / n if n else 0.0,
        "pass_rate_interval": [lo, hi],
        "scenarios_all_pass": (sum(s["all_trials_pass"] for s in scenarios.values()) / len(scenarios)) if scenarios else 0.0,
        "confidently_wrong": sum(g.confidently_wrong for g in grades),
        "total_cost_usd": sum(g.cost_usd for g in grades),
        "skipped": sorted(set(skipped)),
        "scenarios": scenarios,
        "runs_detail": [g.as_dict() for g in grades],
    }


def eligible_rung(summary: dict, thresholds: dict) -> str:
    """Highest trust-ladder rung whose evaluation bar this result clears."""
    best = "observe"
    for rung in ("suggest", "approve", "autonomous"):
        t = thresholds.get(rung, {})
        ok = (
            summary["pass_rate"] >= t.get("min_pass_rate", 0)
            and summary["pass_rate_interval"][0] >= t.get("min_pass_rate_lower_bound", 0)
            and summary["confidently_wrong"] <= t.get("max_confidently_wrong", math.inf)
            and (not t.get("require_every_scenario_every_trial") or summary["scenarios_all_pass"] == 1.0)
        )
        if not ok:
            break
        best = rung
    return best


def compare(current: dict, baseline: dict, tolerance: float = 0.05) -> list[str]:
    """Regressions against a stored baseline. Empty means no regression."""
    problems = []
    if current["pass_rate"] < baseline["pass_rate"] - tolerance:
        problems.append(f"pass rate fell from {baseline['pass_rate']:.0%} to {current['pass_rate']:.0%}")
    if current["confidently_wrong"] > baseline["confidently_wrong"]:
        problems.append(f"confidently wrong runs rose from {baseline['confidently_wrong']} to {current['confidently_wrong']}")
    for name, b in baseline["scenarios"].items():
        cur = current["scenarios"].get(name)
        if cur is None:
            if name not in current.get("skipped", []):
                problems.append(f"{name}: missing from this run")
            continue
        if b["all_trials_pass"] and not cur["all_trials_pass"]:
            problems.append(f"{name}: passed every trial in the baseline, now {cur['passes']}/{cur['runs']}")
    return problems


def report(summary: dict, title: str, thresholds: Optional[dict] = None) -> str:
    lo, hi = summary["pass_rate_interval"]
    lines = [
        f"# {title}", "",
        f"**Pass rate:** {summary['pass_rate']:.0%} of {summary['runs']} runs "
        f"(95% interval {lo:.0%}–{hi:.0%}), {summary['trials']} trial(s) per scenario.  ",
        f"**Confidently wrong:** {summary['confidently_wrong']} run(s).  ",
        f"**Scenarios passing every trial:** {summary['scenarios_all_pass']:.0%}.  ",
        f"**Cost:** ${summary['total_cost_usd']:.2f}.",
    ]
    if thresholds:
        lines.append(f"**Evaluation bar cleared for rung:** {eligible_rung(summary, thresholds)}.")
    if summary["skipped"]:
        lines.append(f"**Skipped (no model for them):** {', '.join(summary['skipped'])}.")
    lines += ["", "| Scenario | Passed | Confidently wrong | Tool calls (median) | Iterations (mean) | Failures |",
              "|---|---|---|---|---|---|"]
    for name, s in summary["scenarios"].items():
        fails = "; ".join(s["failures"]) or "—"
        lines.append(f"| {name} | {s['passes']}/{s['runs']} | {s['confidently_wrong']} | {s['median_tool_calls']:.0f} | "
                     f"{s['mean_iterations']:.1f} | {fails} |")
    return "\n".join(lines) + "\n"
