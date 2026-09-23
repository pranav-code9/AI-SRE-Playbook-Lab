import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
AGENT = ROOT.parent / "agent"


def load(name):
    return json.loads((ROOT / "scenarios" / f"{name}.json").read_text())


def script(path):
    data = json.loads(Path(path).read_text())
    data.pop("_about", None)
    return data


@pytest.fixture
def baseline_scenario():
    return load("baseline")
