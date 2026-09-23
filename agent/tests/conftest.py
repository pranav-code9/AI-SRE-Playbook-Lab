import copy
import json
from pathlib import Path

import pytest

from investigator.tools.base import ToolContext, parse_ts
from investigator.tools.catalog import build_registry
from investigator.tools.fixture import fixture_backends, load_scenario

SCENARIOS = Path(__file__).resolve().parents[1] / "scenarios"


@pytest.fixture
def scenario():
    return copy.deepcopy(load_scenario(SCENARIOS / "checkout_retry_storm.json"))


@pytest.fixture
def script():
    data = json.loads((SCENARIOS / "checkout_retry_storm.script.json").read_text())
    data.pop("_about")
    return data


@pytest.fixture
def registry(scenario):
    return build_registry(fixture_backends(scenario))


@pytest.fixture
def ctx(scenario):
    i = scenario["incident"]
    return ToolContext(i["namespace"], parse_ts(i["window_start"]), parse_ts(i["window_end"]))
