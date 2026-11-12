"""A generic root-cause key for fan-out rules: count, report and baseline by cause (#201)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from arborescent_helpers import write_files
from test_baseline_ratchet import write_policy

from arborescent import baseline
from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.cli_report import _print_summary
from arborescent.domain import FAN_OUT_RULES, Rule, Violation
from arborescent.engine import CheckReport, Services
from arborescent.engines import default_engines
from arborescent.facts import ModuleInfo, extract
from arborescent.lazy_imports import lazy_import_violations
from arborescent.testing import FakeFileSystem, FakeToolRunner
from arborescent.typecheckers import all_checkers

CAUSE = "fan|heavy"


def f(subject: str, cause: str = CAUSE, detail: str = "d") -> Violation:
    return Violation("fan", subject, detail, root_cause=cause)


def filesystem() -> tuple[FakeFileSystem, Path]:
    fs = FakeFileSystem()
    fs.mkdir(Path("/repo"))
    return fs, Path("/repo/baseline.json")


def test_root_cause_is_not_part_of_identity() -> None:
    plain, tagged = Violation("fan", "a", "d"), f("a")
    assert plain == tagged and plain.key == tagged.key == "fan|a|d"
    assert plain.cause == "fan|a|d" and tagged.cause == CAUSE
    assert tagged.render() == plain.render()


# --- apply -----------------------------------------------------------------------------------------------------------


def test_a_covered_cause_tolerates_its_recorded_members_even_when_the_detail_changes() -> None:
    result = baseline.apply([f("a", detail="changed"), f("b")], set(), root_causes={CAUSE: {"a", "b"}})
    assert result.ok and result.tolerated == 2


def test_a_covered_cause_that_gained_a_member_counts_as_growth() -> None:
    result = baseline.apply([f("a"), f("c")], set(), root_causes={CAUSE: {"a"}})
    assert [x.subject for x in result.new] == ["c"]


def test_a_member_does_not_cover_another_cause() -> None:
    result = baseline.apply([f("a", cause="fan|other")], set(), root_causes={CAUSE: {"a"}})
    assert [x.subject for x in result.new] == ["a"]


def test_a_finding_with_no_cause_is_never_covered_by_one() -> None:
    result = baseline.apply([Violation("fan", "a", "d")], set(), root_causes={"": {"a"}, CAUSE: {"a"}})
    assert len(result.new) == 1


def test_a_cause_with_fewer_members_is_stale_and_a_fixed_cause_is_stale_whole() -> None:
    partial = baseline.apply([f("a")], set(), root_causes={CAUSE: {"a", "b"}})
    assert partial.stale == [f"{CAUSE} (1 of 2 recorded fan-out member(s) gone: b)"] and not partial.ok
    gone = baseline.apply([], set(), root_causes={CAUSE: {"a", "b"}})
    assert gone.stale == [f"{CAUSE} (all 2 of 2 recorded fan-out member(s) gone: a, b)"]
    assert baseline.apply([], set(), complete=False, root_causes={CAUSE: {"a"}}).stale == []


def test_empty_inputs() -> None:
    assert baseline.apply([], set(), root_causes={}).ok
    assert baseline.apply([], set(), root_causes=None).ok
    assert baseline.apply([f("a")], set(), root_causes={CAUSE: set()}).new[0].subject == "a"


# --- record / prune / growth -----------------------------------------------------------------------------------------


def test_by_root_cause_writes_one_entry_per_cause() -> None:
    fs, path = filesystem()
    baseline.record(fs, path, [f("b"), f("a"), f("x", cause="fan|other"), Violation("plain", "p", "d")], by_root_cause=True)
    assert fs.read_json(path) == {
        "version": 2,
        "violations": ["plain|p|d"],
        "growth": [],
        "root_causes": {CAUSE: ["a", "b"], "fan|other": ["x"]},
    }


def test_without_the_flag_a_new_baseline_stays_per_finding() -> None:
    fs, path = filesystem()
    baseline.record(fs, path, [f("a")])
    assert fs.read_json(path) == {"version": 2, "violations": ["fan|a|d"], "growth": []}


def test_root_causes_are_never_written_when_empty() -> None:
    fs, path = filesystem()
    baseline.record(fs, path, [Violation("plain", "p", "d")], by_root_cause=True)
    assert "root_causes" not in fs.read_json(path)


def test_adopt_by_root_cause_logs_the_members_and_is_once_per_family() -> None:
    fs, path = filesystem()
    baseline.save(fs, path, [Violation("internal-import", "old", "d")])
    found = [Violation("internal-import", "old", "d"), Violation("effect-x", "a", "d", root_cause="effect-x|k"), Violation("effect-x", "b", "d", root_cause="effect-x|k")]
    baseline.record(fs, path, found, adopt="effects", by_root_cause=True)
    doc = fs.read_json(path)
    assert doc["root_causes"] == {"effect-x|k": ["a", "b"]}
    assert doc["violations"] == ["internal-import|old|d"]
    assert doc["growth"] == [{"reason": "adopt effects family", "added": ["effect-x|k|a", "effect-x|k|b"]}]
    with pytest.raises(baseline.BaselineError, match="already contains"):
        baseline.record(fs, path, [*found, Violation("effect-x", "c", "d", root_cause="effect-x|k")], adopt="effects", by_root_cause=True)


def test_a_new_member_of_a_recorded_cause_is_refused_without_a_reason_and_leaves_the_file_alone() -> None:
    fs, path = filesystem()
    baseline.record(fs, path, [f("a")], by_root_cause=True)
    before = fs.read_text(path)
    with pytest.raises(baseline.BaselineError, match="growth refused"):
        baseline.record(fs, path, [f("a"), f("b")])
    assert fs.read_text(path) == before
    # the second attempt, with a reason, succeeds and keeps the grouping although the flag is absent
    baseline.record(fs, path, [f("a"), f("b")], allow_growth=True, reason="why")
    doc = fs.read_json(path)
    assert doc["root_causes"] == {CAUSE: ["a", "b"]} and doc["violations"] == []
    assert doc["growth"][-1] == {"reason": "why", "added": [f"{CAUSE}|b"]}


def test_shrinking_a_cause_needs_no_reason_and_recording_twice_is_stable() -> None:
    fs, path = filesystem()
    baseline.record(fs, path, [f("a"), f("b")], by_root_cause=True)
    baseline.record(fs, path, [f("a")], by_root_cause=True)
    first = fs.read_text(path)
    assert fs.read_json(path)["root_causes"] == {CAUSE: ["a"]}
    baseline.record(fs, path, [f("a")], by_root_cause=True)
    assert fs.read_text(path) == first


def test_findings_already_recorded_by_key_stay_by_key() -> None:
    fs, path = filesystem()
    baseline.record(fs, path, [f("a")])
    baseline.record(fs, path, [f("a")], by_root_cause=True)
    assert fs.read_json(path) == {"version": 2, "violations": ["fan|a|d"], "growth": []}


def test_duplicate_findings_are_recorded_once() -> None:
    fs, path = filesystem()
    assert baseline.record(fs, path, [f("a"), f("a")], by_root_cause=True) == 1


def test_growth_consistency_sees_root_cause_members() -> None:
    old = {"version": 2, "violations": [], "growth": [], "root_causes": {CAUSE: ["a"]}}
    logged = {**old, "root_causes": {CAUSE: ["a", "b"]}, "growth": [{"reason": "r", "approved-by": "me", "added": [f"{CAUSE}|b"]}]}
    assert baseline.growth_consistent(logged, old)
    assert not baseline.growth_consistent({**logged, "growth": []}, old)
    assert baseline.growth_consistent(old, {**old, "root_causes": {CAUSE: ["a", "b"]}})  # shrinking needs no entry
    assert not baseline.growth_consistent({**old, "root_causes": {CAUSE: "oops"}}, old)


def test_prune_drops_gone_members_and_empty_causes() -> None:
    fs, path = filesystem()
    baseline.record(fs, path, [f("a"), f("b"), f("x", cause="fan|other")], by_root_cause=True)
    assert baseline.prune(fs, path, [f("a"), f("x", cause="fan|other")]) == 1
    assert fs.read_json(path)["root_causes"] == {CAUSE: ["a"], "fan|other": ["x"]}
    assert baseline.prune(fs, path, [f("a")]) == 1
    assert fs.read_json(path)["root_causes"] == {CAUSE: ["a"]}
    assert baseline.prune(fs, path, []) == 1
    assert fs.read_json(path)["root_causes"] == {}


def test_load_and_recorded_rules() -> None:
    fs, path = filesystem()
    assert baseline.load_root_causes(fs, path) == {}
    baseline.record(fs, path, [f("a"), Violation("plain", "p", "d")], by_root_cause=True)
    assert baseline.load_root_causes(fs, path) == {CAUSE: frozenset({"a"})}
    assert baseline.load(fs, path) == {"plain|p|d"}
    assert baseline.recorded_rules(fs, path) == {"fan", "plain"}


@pytest.mark.parametrize("bad", [[], "x", {"c": "a"}])
def test_a_malformed_root_causes_section_is_a_clear_error(bad: object) -> None:
    fs, path = filesystem()
    fs.write_json(path, {"version": 2, "violations": [], "growth": [], "root_causes": bad})
    with pytest.raises(ValueError, match="root"):
        baseline.document(fs, path)


def test_an_old_baseline_still_covers_new_findings_that_name_a_cause() -> None:
    fs, path = filesystem()
    fs.write_json(path, {"version": 1, "violations": ["fan|a|d"]})
    result = baseline.apply([f("a"), f("b")], baseline.load(fs, path), root_causes=baseline.load_root_causes(fs, path))
    assert [x.subject for x in result.new] == ["b"]


# --- summaries -------------------------------------------------------------------------------------------------------


def test_root_cause_groups_are_widest_first_and_skip_findings_without_one() -> None:
    items = [f("a"), f("b"), f("c"), f("x", cause="fan|big"), f("y", cause="fan|big"), f("z", cause="fan|big"), f("w", cause="fan|big"), Violation("fan", "p", "d")]
    assert [(c, len(m)) for c, m in baseline.root_cause_groups(items)] == [("fan|big", 4), (CAUSE, 3)]
    assert baseline.cause_count(items) == 3


def test_rule_summary_reports_causes_only_for_a_fan_out() -> None:
    items = [f("a"), f("b"), f("c"), Violation("plain", "p", "d")]
    assert baseline.rule_summary(items, [items[0]]) == "by rule: fan 3 / 1 root cause (1 new), plain 1 (0 new)"
    assert baseline.rule_summary([f("a"), f("b", cause="fan|b")], []) == "by rule: fan 2 (0 new)"


def test_summary_line_counts_violations_and_root_causes(capsys: pytest.CaptureFixture[str]) -> None:
    report = CheckReport(violations=[f("a"), f("b"), f("c"), Violation("plain", "p", "d")], parsed=3)
    report.ratchet = baseline.apply(report.violations, set())
    _print_summary(report)
    assert "(4 violation(s) / 2 root cause(s), 0 baselined, 3 file(s) parsed)" in capsys.readouterr().out


# --- the rules that fan out ------------------------------------------------------------------------------------------


def test_lazy_import_names_the_imported_module_as_its_cause() -> None:
    info = ModuleInfo("app.pkg.sub", "app/pkg/sub.py", False, extract("def g() -> None:\n    import json\n    from . import sibling\n    from ..up import thing\n    from .. import top\n"))
    assert [v.root_cause for v in lazy_import_violations(info)] == [
        "lazy-import|json", "lazy-import|app.pkg", "lazy-import|app.up", "lazy-import|app",
    ]


def test_lazy_importers_of_one_module_share_a_cause() -> None:
    one = lazy_import_violations(ModuleInfo("app.a", "app/a.py", False, extract("def g() -> None:\n    import json\n")))
    two = lazy_import_violations(ModuleInfo("app.b", "app/b.py", False, extract("def g() -> None:\n    import json\n")))
    assert one[0].root_cause == two[0].root_cause and one[0].key != two[0].key


def _calls_with(tree: ast.AST, names: frozenset[str]) -> list[ast.Call]:
    """``Violation(Rule.X, ...)`` calls for the given rule names."""
    out: list[ast.Call] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Violation" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Attribute) and isinstance(first.value, ast.Name) and first.value.id == "Rule" and first.attr in names:
                out.append(node)
    return out


SRC = Path(__file__).resolve().parents[1] / "src" / "arborescent"


def test_every_construct_site_of_a_fan_out_rule_names_a_root_cause() -> None:
    names = frozenset(rule.name for rule in FAN_OUT_RULES)
    seen: set[str] = set()
    missing: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        for call in _calls_with(ast.parse(path.read_text(encoding="utf-8")), names):
            seen.add(str(ast.unparse(call.args[0])))
            if not any(kw.arg == "root_cause" for kw in call.keywords):
                missing.append(f"{path.name}:{call.lineno}")
    assert not missing, f"fan-out rules must pass root_cause=...: {missing}"
    assert seen == {f"Rule.{name}" for name in names}, "a fan-out rule has no construct site left to scan"


def test_nothing_rebuilds_a_violation_field_by_field() -> None:
    """A consumer that copies ``rule``/``subject``/``detail`` into a new Violation drops the root cause (and the hint)."""
    offenders: list[str] = []
    for path in sorted(SRC.glob("*.py")):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "Violation":
                reads = {a.attr for arg in node.args for a in ast.walk(arg) if isinstance(a, ast.Attribute) and a.attr in {"subject", "detail", "key"}}
                if {"subject", "detail"} <= reads and not any(kw.arg == "root_cause" for kw in node.keywords):
                    offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, f"use dataclasses.replace so root_cause survives: {offenders}"


def test_the_fan_out_rules_are_real_rule_ids() -> None:
    assert FAN_OUT_RULES and all(isinstance(rule, Rule) for rule in FAN_OUT_RULES)


# --- through the command line ----------------------------------------------------------------------------------------


def _svc() -> Services:
    return Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), LocalFileSystem(), SystemClock(), default_engines())


def _lazy_repo(repo: Path) -> Path:
    config = write_policy(repo)
    write_files(repo, {"app/adapters.py": "def f() -> None:\n    import os\n", "app/composition.py": ""})
    return config


def test_baseline_by_root_cause_then_a_new_importer_of_the_same_module_fails(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _lazy_repo(repo)
    svc = _svc()
    write_files(repo, {"app/domain.py": "def g() -> None:\n    import os\n"})
    assert main(["--config", str(config), "baseline", "--by-root-cause"], svc) == 0
    assert "under 1 root cause entry" in capsys.readouterr().out
    doc = LocalFileSystem().read_json(repo / "arborescent-baseline.json")
    assert doc["root_causes"] == {"lazy-import|os": ["app.adapters", "app.domain"]}

    assert main(["--config", str(config), "check", "--no-tools"], svc) == 0
    assert "4 violation(s) / 3 root cause(s), 4 baselined" in capsys.readouterr().out

    write_files(repo, {"app/ports.py": "def h() -> None:\n    import os\n"})
    assert main(["--config", str(config), "check", "--no-tools"], svc) == 1
    out = capsys.readouterr().out
    assert [ln for ln in out.splitlines() if "[lazy-import]" in ln] == ["FAIL   app.ports:2: [lazy-import] os#1 (put it at module level, or record why it stays with `# deferred: <reason>` on or above the line)"]

    # a different imported module is a different, uncovered cause
    write_files(repo, {"app/ports.py": "", "app/domain.py": "def g() -> None:\n    import os\n    import re\n"})
    assert main(["--config", str(config), "check", "--no-tools"], svc) == 1

    # fixing every importer of the cause leaves a stale entry, until it is pruned
    write_files(repo, {"app/adapters.py": "", "app/domain.py": ""})
    assert main(["--config", str(config), "check", "--no-tools"], svc) == 1
    assert "STALE  baseline entry no longer occurs" in capsys.readouterr().out
    assert main(["--config", str(config), "baseline", "--prune"], svc) == 0
    assert main(["--config", str(config), "check", "--no-tools"], svc) == 0


def test_check_by_root_cause_lists_the_causes_widest_first(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _lazy_repo(repo)
    write_files(repo, {"app/domain.py": "def g() -> None:\n    import os\n    import re\n", "app/ports.py": "def h() -> None:\n    import os\n"})
    assert main(["--config", str(config), "check", "--no-tools", "--by-root-cause"], _svc()) == 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith(("CAUSE", "FAIL"))]
    assert lines[0].startswith("CAUSE")
    assert "lazy-import|os" in lines[0] and "3 finding(s)" in lines[0] and "app.adapters, app.domain, app.ports" in lines[0]
    assert "lazy-import|re" in lines[1]
    assert not any(ln.startswith("FAIL") and "[lazy-import]" in ln for ln in lines)


def test_check_by_root_cause_still_lists_findings_without_a_cause(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _lazy_repo(repo)
    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    assert main(["--config", str(config), "check", "--no-tools", "--by-root-cause"], _svc()) == 1
    assert "FAIL   app.domain:1: [internal-import]" in capsys.readouterr().out


def test_prune_refuses_by_root_cause(repo: Path) -> None:
    config = _lazy_repo(repo)
    assert main(["--config", str(config), "baseline", "--prune", "--by-root-cause"], _svc()) == 2
