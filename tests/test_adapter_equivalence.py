"""#113: modules declared equivalent (two adapters of one port) need one test function that calls into every member."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import RINGS, mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.domain import INFORMATIONAL_RULES, Policy, Rule, Violation
from arborescent.equivalence import parse_groups
from arborescent.practices import run_practices
from arborescent.testing import FakeToolRunner

FS = LocalFileSystem()
OPEN_ADAPTERS = {"ring": {**RINGS, "adapters": {**RINGS["adapters"], "forbid": []}}}
LIB = {
    "app/__init__.py": "",
    "app/facts_a.py": "def lookup(include_pending: bool = False) -> list[str]:\n    return []\n",
    "app/facts_b.py": "def lookup(include_pending: bool = True) -> list[str]:\n    return []\n",
    "app/other.py": "def other() -> int:\n    return 1\n",
}
BOTH = "from app import facts_a, facts_b\n\ndef test_parity():\n    assert facts_a.lookup() == facts_b.lookup()\n"


def run(root: Path, tests: dict[str, str], groups: object, *, report: bool = False, files: dict[str, str] | None = None) -> list[Violation]:
    write_files(root, {**(files or LIB), **tests})
    practices: dict[str, object] = {"adapter-equivalence": groups}
    if report:
        practices["adapter-equivalence-report"] = True
    return _practices(mini_policy(practices=practices, **OPEN_ADAPTERS), root)


def _practices(policy: Policy, root: Path) -> list[Violation]:
    found = run_practices(policy, root, FakeToolRunner(), FS, FsScanner().scan(policy, root))
    return [v for v in found if v.rule in (Rule.ADAPTER_EQUIVALENCE, Rule.ADAPTER_EQUIVALENCE_REPORT)]


GROUP = [["app.facts_a", "app.facts_b"]]


def test_one_test_function_calling_every_member_satisfies_the_group(tmp_path: Path) -> None:
    assert run(tmp_path, {"tests/test_a.py": BOTH}, GROUP) == []


def test_a_group_with_no_such_test_fails_and_names_the_members(tmp_path: Path) -> None:
    split = "from app import facts_a, facts_b\n\ndef test_a():\n    facts_a.lookup()\n\ndef test_b():\n    facts_b.lookup()\n"
    out = run(tmp_path, {"tests/test_a.py": split}, GROUP)
    assert [(v.rule, v.subject) for v in out] == [(Rule.ADAPTER_EQUIVALENCE, "app.facts_a, app.facts_b")]
    assert "no single test function" in out[0].detail


def test_report_mode_demotes_only_the_missing_test_finding_to_information(tmp_path: Path) -> None:
    out = run(tmp_path, {"tests/test_a.py": "def test_x():\n    pass\n"}, GROUP, report=True)
    assert [v.rule for v in out] == [Rule.ADAPTER_EQUIVALENCE_REPORT]
    assert Rule.ADAPTER_EQUIVALENCE_REPORT in INFORMATIONAL_RULES and Rule.ADAPTER_EQUIVALENCE not in INFORMATIONAL_RULES


def test_a_helper_a_module_level_call_and_a_bare_import_do_not_count(tmp_path: Path) -> None:
    tests = {
        "tests/test_a.py": (
            "from app import facts_a, facts_b\n\nfacts_a.lookup()\nfacts_b.lookup()\n\n"
            "def helper():\n    facts_a.lookup()\n    facts_b.lookup()\n\n"
            "def test_mentions():\n    assert facts_a is not facts_b\n"
        )
    }
    assert len(run(tmp_path, tests, GROUP)) == 1


def test_test_class_methods_count_and_the_two_calls_may_use_aliases(tmp_path: Path) -> None:
    tests = {"tests/test_a.py": "import app.facts_a as a\nfrom app.facts_b import lookup as b\n\nclass TestParity:\n    def test_same(self):\n        a.lookup(); b()\n"}
    assert run(tmp_path, tests, GROUP) == []


def test_three_member_group_needs_all_three_in_one_function(tmp_path: Path) -> None:
    group = [["app.facts_a", "app.facts_b", "app.other"]]
    assert len(run(tmp_path, {"tests/test_a.py": BOTH}, group)) == 1
    three = "from app import facts_a, facts_b, other\n\ndef test_all():\n    facts_a.lookup(); facts_b.lookup(); other.other()\n"
    assert run(tmp_path, {"tests/test_a.py": three}, group) == []


def test_missing_tests_folder_reports_the_group_instead_of_crashing(tmp_path: Path) -> None:
    assert [v.rule for v in run(tmp_path, {}, GROUP)] == [Rule.ADAPTER_EQUIVALENCE]


@pytest.mark.parametrize("report", [False, True])
def test_an_unknown_module_is_an_error_even_in_report_mode_and_suggests_the_suffix_match(tmp_path: Path, report: bool) -> None:
    out = run(tmp_path, {"tests/test_a.py": BOTH}, [["facts_a", "app.facts_b"]], report=report)
    assert [v.rule for v in out] == [Rule.ADAPTER_EQUIVALENCE]
    assert "'facts_a'" in out[0].detail and "app.facts_a" in out[0].detail


def test_an_unknown_module_without_a_suffix_match_is_still_an_error(tmp_path: Path) -> None:
    out = run(tmp_path, {"tests/test_a.py": BOTH}, [["app.nope", "app.facts_b"]])
    assert len(out) == 1 and "'app.nope'" in out[0].detail and "not a scanned module" in out[0].detail


def test_an_empty_group_and_a_one_member_group_are_errors(tmp_path: Path) -> None:
    out = run(tmp_path, {"tests/test_a.py": BOTH}, [[], ["app.facts_a"], ["app.other", "app.other"]])
    assert [v.rule for v in out] == [Rule.ADAPTER_EQUIVALENCE] * 3
    assert all("at least two" in v.detail for v in out)


def test_a_module_in_two_groups_is_an_error(tmp_path: Path) -> None:
    groups = [["app.facts_a", "app.facts_b"], ["app.facts_b", "app.other"]]
    out = run(tmp_path, {"tests/test_a.py": BOTH}, groups)
    assert any("app.facts_b" in v.detail and "more than one group" in v.detail for v in out)


@pytest.mark.parametrize("value", ["app.facts_a", [["app.facts_a", 3]], ["app.facts_a", "app.facts_b"], {"a": 1}, [None]])
def test_a_malformed_value_is_an_error(tmp_path: Path, value: object) -> None:
    out = run(tmp_path, {"tests/test_a.py": BOTH}, value)
    assert [v.rule for v in out] == [Rule.ADAPTER_EQUIVALENCE] and "list of groups" in out[0].detail


def test_off_when_unset_or_false(tmp_path: Path) -> None:
    write_files(tmp_path, {**LIB, "tests/test_a.py": "def test_x():\n    pass\n"})
    for practices in ({}, {"adapter-equivalence": False}, {"adapter-equivalence-report": True}):
        assert _practices(mini_policy(practices=practices, **OPEN_ADAPTERS), tmp_path) == []


def test_parse_groups_returns_clean_groups_and_all_the_problems() -> None:
    groups, problems = parse_groups([["a", "b"], ["a", "c"], ["x"]])
    assert groups == [("a", "b"), ("a", "c"), ("x",)] and problems == []
    assert parse_groups("a")[1] and parse_groups([["a", 1]])[1]


def test_the_tests_folder_defaults_to_tests_and_can_be_set(tmp_path: Path) -> None:
    write_files(tmp_path, {**LIB, "checks/test_a.py": BOTH})
    base = {"adapter-equivalence": GROUP}
    assert len(_practices(mini_policy(practices=base, **OPEN_ADAPTERS), tmp_path)) == 1
    assert _practices(mini_policy(practices={**base, "adapter-equivalence-tests": "checks"}, **OPEN_ADAPTERS), tmp_path) == []
