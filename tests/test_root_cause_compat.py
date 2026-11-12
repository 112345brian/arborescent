"""What every Violation and baseline consumer relies on today (#201): these pin the behaviour that adding an optional
``root_cause`` must not change. A baseline written before the field existed has to keep working byte for byte."""

from __future__ import annotations

from pathlib import Path

import pytest

from arborescent import baseline
from arborescent.cli_report import _print_summary
from arborescent.domain import Violation
from arborescent.engine import CheckReport
from arborescent.testing import FakeFileSystem


def v(rule: str, subject: str, detail: str = "detail") -> Violation:
    return Violation(rule, subject, detail)


def filesystem() -> tuple[FakeFileSystem, Path]:
    fs = FakeFileSystem()
    fs.mkdir(Path("/repo"))
    return fs, Path("/repo/baseline.json")


def test_violation_positional_signature_and_identity_are_unchanged() -> None:
    item = Violation("r", "s", "d", 3, "tgt", " hint")
    assert (item.rule, item.subject, item.detail, item.line, item.target, item.hint) == ("r", "s", "d", 3, "tgt", " hint")
    assert item.key == "r|s|d"
    assert Violation("r", "s", "d", 3) == Violation("r", "s", "d", 3, "other", "other hint")
    assert hash(Violation("r", "s", "d", 3)) == hash(Violation("r", "s", "d", 3, "other"))


def test_render_is_unchanged() -> None:
    assert Violation("r", "s", "d", 4, hint=" (h)").render() == "s:4: [r] d (h)"


def test_old_baseline_document_has_exactly_the_old_shape() -> None:
    fs, path = filesystem()
    fs.write_json(path, {"version": 1, "violations": ["a|b|c"]})
    assert baseline.document(fs, path) == {"version": 2, "violations": ["a|b|c"], "growth": []}
    assert baseline.load(fs, path) == {"a|b|c"}


def test_missing_baseline_document_is_empty() -> None:
    fs, path = filesystem()
    assert baseline.document(fs, path) == {"version": 2, "violations": [], "growth": []}
    assert baseline.load(fs, path) == set()


def test_prune_does_not_add_keys_to_an_old_baseline() -> None:
    fs, path = filesystem()
    fs.write_json(path, {"version": 2, "violations": ["r|a|d", "r|b|d"], "growth": [{"reason": "x"}]})
    assert baseline.prune(fs, path, [v("r", "a", "d")]) == 1
    assert fs.read_json(path) == {"version": 2, "violations": ["r|a|d"], "growth": [{"reason": "x"}]}


def test_apply_counts_new_stale_and_tolerated() -> None:
    result = baseline.apply([v("r", "a"), v("r", "b")], {"r|a|detail", "r|gone|detail"})
    assert [x.key for x in result.new] == ["r|b|detail"]
    assert result.stale == ["r|gone|detail"]
    assert result.tolerated == 1
    assert not result.ok
    partial = baseline.apply([v("r", "a")], {"r|a|detail", "r|gone|detail"}, complete=False)
    assert partial.stale == [] and partial.ok


def test_record_logs_plain_growth_keys_and_refuses_unexplained_growth() -> None:
    fs, path = filesystem()
    baseline.save(fs, path, [v("r", "old")])
    with pytest.raises(baseline.BaselineError, match="growth refused"):
        baseline.record(fs, path, [v("r", "old"), v("r", "new")])
    baseline.record(fs, path, [v("r", "old"), v("r", "new")], allow_growth=True, reason="why")
    assert fs.read_json(path) == {
        "version": 2,
        "violations": ["r|new|detail", "r|old|detail"],
        "growth": [{"reason": "why", "added": ["r|new|detail"]}],
    }


def test_growth_consistent_for_plain_keys() -> None:
    old = {"version": 2, "violations": ["r|a|d"], "growth": []}
    grown = {"version": 2, "violations": ["r|a|d", "r|b|d"], "growth": [{"reason": "x", "approved-by": "me", "added": ["r|b|d"]}]}
    assert baseline.growth_consistent(grown, old)
    assert not baseline.growth_consistent({**grown, "growth": []}, old)


def test_rule_summary_is_unchanged_without_fan_out() -> None:
    items = [v("a", "x"), v("a", "y"), v("b", "z")]
    assert baseline.rule_summary(items, [items[1]]) == "by rule: a 2 (1 new), b 1 (0 new)"
    assert baseline.rule_summary([], []) is None


def test_summary_line_is_unchanged_without_fan_out(capsys: pytest.CaptureFixture[str]) -> None:
    report = CheckReport(violations=[v("a", "x"), v("a", "y")], parsed=7)
    report.ratchet = baseline.apply(report.violations, {"a|x|detail"})
    _print_summary(report)
    out = capsys.readouterr().out
    assert "(2 violation(s), 1 baselined, 7 file(s) parsed)" in out
    assert "root cause" not in out
