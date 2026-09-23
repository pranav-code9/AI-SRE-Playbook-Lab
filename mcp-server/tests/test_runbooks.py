from conftest import RUNBOOKS

from sre_mcp.actions import Rollback
from sre_mcp.runbooks import Runbook, Step, load_runbooks, run_runbook, validate_runbook


def test_every_runbook_matches_the_tools_it_names(registry):
    books = load_runbooks(RUNBOOKS)
    assert books, "no runbooks found"
    for book in books:
        assert validate_runbook(book, registry, {Rollback.name}, RUNBOOKS) == [], book.ref


def _book(*steps):
    return Runbook(name="t", version=1, title="t", source="checkout-errors.md", params={"service": "checkout"}, steps=list(steps))


def test_validation_catches_drift(registry):
    book = _book(
        Step(id="a", kind="diagnosis", title="a", tool="get_service_rde", args={"service": "{service}"}),
        Step(id="b", kind="diagnosis", title="b", tool="get_dependencies", args={"service": "x", "depth": 9}),
        Step(id="c", kind="action", title="c", tool="restart_everything", requires="IC"),
        Step(id="d", kind="action", title="d", tool="rollback_release"),
        Step(id="e", kind="judgment", title="e", tool="get_service_red"),
    )
    problems = validate_runbook(book, registry, {Rollback.name}, RUNBOOKS)
    assert len(problems) == 5
    assert "unknown read tool 'get_service_rde'" in problems[0]
    assert "arguments don't fit get_dependencies" in problems[1]
    assert "unknown action tool 'restart_everything'" in problems[2]
    assert "must say what approval" in problems[3]
    assert "judgment steps can't name a tool" in problems[4]


def test_running_the_runbook_does_diagnosis_only(registry, context):
    [book] = load_runbooks(RUNBOOKS)
    r = run_runbook(book, registry, context(), {})
    assert r.ok
    assert "[recent-deploys] Recent deploys: 1 change(s)" in r.summary
    assert "otel-demo revision 3" in r.summary
    assert [f["step"] for f in r.data["findings"]] == ["symptom", "dependencies", "payment", "recent-deploys", "failed-order"]
    assert [a["step"] for a in r.data["actions"]] == ["rollback", "maintenance-banner"]
    assert "Actions available, not run" in r.summary
    assert r.data["runbook"] == "checkout-errors@3"


def test_runbook_parameters_override_defaults(registry, context):
    [book] = load_runbooks(RUNBOOKS)
    r = run_runbook(book, registry, context(), {"service": "payment"})
    assert r.data["findings"][0]["args"] == {"service": "payment"}
