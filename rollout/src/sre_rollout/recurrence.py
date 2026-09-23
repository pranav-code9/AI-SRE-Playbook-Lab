"""The case study, a month later: the same misconfiguration comes back in a
new release, and this time the rollback's effect is in the data too, so the
trust ladder's verification has something to measure.
"""

from __future__ import annotations

import copy
import json
import re
from datetime import datetime, timedelta

TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _shift(value, delta: timedelta):
    if isinstance(value, str) and TS.match(value):
        dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ") + delta
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if isinstance(value, list):
        return [_shift(v, delta) for v in value]
    if isinstance(value, dict):
        return {k: _shift(v, delta) for k, v in value.items()}
    return value


def recurrence_scenario(base: dict, days: int = 30, revision_offset: int = 4, recovery_minutes: int = 20,
                        incident_tail_minutes: int = 3) -> dict:
    s = _shift(copy.deepcopy(base), timedelta(days=days))
    for c in s["changes"]:
        c["revision"] += revision_offset
    gt = s.get("ground_truth", {})
    if "change" in gt:
        gt["change"]["revision"] += revision_offset
    end = datetime.strptime(s["incident"]["window_end"], "%Y-%m-%dT%H:%M:%SZ")
    # The incident continues until the rollback (a few minutes after the window
    # ends, once it's approved), then the service recovers. Request rate is
    # extended too, and not only the metric under test: verification refuses to
    # call a window a success when there was too little traffic to judge, and a
    # rollback that merely stopped the traffic would otherwise score as a fix.
    recovery = {
        ("checkout", "error_rate"): 0.004,
        ("checkout", "request_rate"): 5.0,   # users keep shopping throughout
        ("payment", "request_rate"): 5.0,
    }
    for (service, metric), healthy in recovery.items():
        series = s["metrics"][service][metric]
        during = series[-1][1]
        for m in range(1, recovery_minutes + 1):
            value = during if m <= incident_tail_minutes else healthy
            series.append([(end + timedelta(minutes=m)).strftime("%Y-%m-%dT%H:%M:%SZ"), value])
    s["name"] = "checkout-retry-storm-recurrence"
    s["description"] = f"The retry storm again, {days} days later, in revision {3 + revision_offset}."
    return s


def recurrence_script(base_script: dict, revision_offset: int = 4) -> dict:
    """The Chapter 3 script, adjusted for the new revision numbers."""
    text = json.dumps(base_script)
    text = text.replace('\\"revision_a\\": 2, \\"revision_b\\": 3', f'\\"revision_a\\": {2 + revision_offset}, \\"revision_b\\": {3 + revision_offset}')
    text = text.replace('\\"to_revision\\": 2', f'\\"to_revision\\": {2 + revision_offset}')
    for word in ("revision", "Revision"):
        text = text.replace(f"{word} 3", f"{word} {3 + revision_offset}").replace(f"{word} 2", f"{word} {2 + revision_offset}")
    text = text.replace("2026-09-22", "2026-10-22")
    return json.loads(text)
