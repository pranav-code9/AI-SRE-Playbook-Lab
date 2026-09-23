import copy
import json
from pathlib import Path

import pytest

from investigator.tools.base import InMemoryRawStore, ToolContext, parse_ts
from investigator.tools.catalog import build_registry
from investigator.tools.fixture import fixture_backends

ROOT = Path(__file__).resolve().parents[1]
SCENARIO = ROOT.parent / "agent" / "scenarios" / "checkout_retry_storm.json"
RUNBOOKS = ROOT / "runbooks"


@pytest.fixture
def scenario():
    return copy.deepcopy(json.loads(SCENARIO.read_text()))


@pytest.fixture
def registry(scenario):
    return build_registry(fixture_backends(scenario))


@pytest.fixture
def context(scenario):
    i = scenario["incident"]
    store = InMemoryRawStore()
    return lambda: ToolContext(i["namespace"], parse_ts(i["window_start"]), parse_ts(i["window_end"]), raw_store=store)
