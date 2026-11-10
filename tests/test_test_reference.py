"""#104: every public function in an I/O-free ring needs a test function that imports and calls it."""

from __future__ import annotations

from pathlib import Path

from arborescent_helpers import RINGS, mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.domain import INFORMATIONAL_RULES, Policy, Rule, Violation
from arborescent.facts import FunctionCalls, extract
from arborescent.practices import (
    TestReferenceConfig,
    function_references,
    missing_test_references,
    run_practices,
)
from arborescent.testing import FakeToolRunner

FS = LocalFileSystem()
# the adapters ring may do I/O; the domain ring is I/O-free by default
OPEN_ADAPTERS = {"ring": {**RINGS, "adapters": {**RINGS["adapters"], "forbid": []}}}



LIB = {
    "app/__init__.py": "",
    "app/domain.py": (
        "def parse(x: int) -> int:\n    return x\n\n"
        "def render(x: int) -> str:\n    return str(x)\n\n"
        "def _helper() -> int:\n    return 1\n\n"
        "def abstract_port() -> None:\n    ...\n\n"
        "class Thing:\n    def method(self) -> int:\n        return 1\n"
    ),
    "app/adapters.py": "import os\n\ndef read() -> str:\n    return os.sep\n",
}


def found(root: Path, tests: dict[str, str], *, files: dict[str, str] | None = None, exempt: list[str] | None = None) -> list[Violation]:
    write_files(root, {**(files or LIB), **tests})
    policy = mini_policy(practices={"test-reference": "tests"}, **OPEN_ADAPTERS)
    return missing_test_references(FS, root, policy, FsScanner().scan(policy, root), TestReferenceConfig("tests", exempt or []))


def names(violations: list[Violation]) -> list[str]:
    return [v.detail.split("'")[1] for v in violations]


def test_a_function_with_no_test_is_reported_by_name(tmp_path: Path) -> None:
    out = found(tmp_path, {"tests/test_a.py": "from app.domain import parse\n\ndef test_parse():\n    assert parse(1) == 1\n"})
    assert names(out) == ["render"]
    assert out[0].rule is Rule.TEST_REFERENCE and out[0].subject == "app.domain" and out[0].line == 4
    assert "[domain]" in out[0].detail


def test_direct_call_module_attribute_and_alias_calls_all_count(tmp_path: Path) -> None:
    tests = {
        "tests/test_a.py": (
            "import app.domain\nimport app.domain as d\nfrom app.domain import render as show\n\n"
            "def test_one():\n    app.domain.parse(1)\n\n"
            "def test_two():\n    assert d.parse(1)\n\n"
            "def test_three():\n    show(1)\n"
        )
    }
    assert found(tmp_path, tests) == []


def test_a_call_through_a_package_reexport_counts(tmp_path: Path) -> None:
    files = {
        "app/__init__.py": "from .impl import parse\n",
        "app/impl.py": "def parse() -> int:\n    return 1\n",
    }
    classify = {"classify": {"app": "domain", "app.impl": "domain"}}
    write_files(tmp_path, {**files, "tests/test_a.py": "from app import parse\n\ndef test_it():\n    parse()\n"})
    policy = mini_policy(practices={"test-reference": "tests"}, **OPEN_ADAPTERS, **classify)
    assert missing_test_references(FS, tmp_path, policy, FsScanner().scan(policy, tmp_path), TestReferenceConfig("tests")) == []
    write_files(tmp_path, {"tests/test_a.py": "def test_it():\n    pass\n"})
    out = missing_test_references(FS, tmp_path, policy, FsScanner().scan(policy, tmp_path), TestReferenceConfig("tests"))
    assert [(v.subject, names([v])[0]) for v in out] == [("app.impl", "parse")]


def test_a_test_that_only_names_or_imports_the_function_does_not_count(tmp_path: Path) -> None:
    tests = {
        "tests/test_a.py": (
            "from app.domain import parse, render\n\n"
            "def test_parse():\n    assert parse is not None\n\n"
            "def test_render():\n    pass\n"
        )
    }
    assert names(found(tmp_path, tests)) == ["parse", "render"]


def test_a_call_in_a_test_helper_counts_but_a_module_level_call_does_not(tmp_path: Path) -> None:
    tests = {
        "tests/test_a.py": (
            "from app.domain import parse, render\n\nparse(1)\n\n"
            "def helper():\n    render(1)\n\n"
            "class TestAll:\n    def test_render(self):\n        render(1)\n"
        )
    }
    assert names(found(tmp_path, tests)) == ["parse"]  # the helper and the Test class method call render


def test_stub_private_class_and_other_ring_are_exempt(tmp_path: Path) -> None:
    out = found(tmp_path, {"tests/test_a.py": "def test_x():\n    pass\n"})
    assert names(out) == ["parse", "render"]  # not _helper, abstract_port, Thing or adapters.read


def test_the_exempt_list_takes_function_or_module_globs(tmp_path: Path) -> None:
    tests = {"tests/test_a.py": "def test_x():\n    pass\n"}
    assert names(found(tmp_path, tests, exempt=["app.domain.render", "nothing.*"])) == ["parse"]
    assert found(tmp_path, tests, exempt=["app.domain"]) == []


def test_output_is_ordered_by_module_then_line(tmp_path: Path) -> None:
    files = {**LIB, "app/__init__.py": "def zed():\n    return 1\n\ndef alpha():\n    return 2\n"}
    out = found(tmp_path, {"tests/test_a.py": "def test_x():\n    pass\n"}, files=files)
    assert [(v.subject, v.line) for v in out] == [("app", 1), ("app", 4), ("app.domain", 1), ("app.domain", 4)]


def test_it_is_information_only_and_off_by_default(tmp_path: Path) -> None:
    assert Rule.TEST_REFERENCE in INFORMATIONAL_RULES
    write_files(tmp_path, {**LIB, "tests/test_a.py": "def test_x():\n    pass\n"})
    off = mini_policy(**OPEN_ADAPTERS)
    assert [v for v in _practice_run(off, tmp_path) if v.rule is Rule.TEST_REFERENCE] == []
    on = mini_policy(practices={"test-reference": "tests"}, **OPEN_ADAPTERS)
    assert {v.rule for v in _practice_run(on, tmp_path)} == {Rule.TEST_REFERENCE}


def _practice_run(policy: Policy, root: Path) -> list[Violation]:
    return run_practices(policy, root, FakeToolRunner(), FS, FsScanner().scan(policy, root))


def test_missing_tests_folder_and_unparsable_test_files_do_not_crash(tmp_path: Path) -> None:
    write_files(tmp_path, LIB)
    policy = mini_policy(**OPEN_ADAPTERS)
    scan = FsScanner().scan(policy, tmp_path)
    assert names(missing_test_references(FS, tmp_path, policy, scan, TestReferenceConfig("tests"))) == ["parse", "render"]
    write_files(tmp_path, {"tests/test_bad.py": "def (:\n", "tests/test_ok.py": "from app.domain import parse\n\ndef test_p():\n    parse(1)\n"})
    assert names(missing_test_references(FS, tmp_path, policy, scan, TestReferenceConfig("tests"))) == ["render"]


def test_function_references_are_reusable_per_test_function(tmp_path: Path) -> None:
    """The machinery #113 extends: which project functions each test function calls."""
    write_files(tmp_path, {**LIB, "tests/test_a.py": (
        "from app import domain, adapters\n\n"
        "def test_both():\n    domain.parse(1)\n    adapters.read()\n\n"
        "def test_one():\n    domain.render(1)\n"
    )})
    policy = mini_policy(**OPEN_ADAPTERS)
    refs = function_references(FS, tmp_path, "tests", FsScanner().scan(policy, tmp_path))
    by_name = {r.name: r.references for r in refs if r.is_test}
    assert by_name == {
        "test_both": frozenset({("app.domain", "parse"), ("app.adapters", "read")}),
        "test_one": frozenset({("app.domain", "render")}),
    }
    assert [r.path for r in refs] == ["tests/test_a.py", "tests/test_a.py"]
    helper = function_references(FS, tmp_path, "tests", FsScanner().scan(policy, tmp_path))
    assert all(r.is_test for r in helper)


def test_extract_records_resolved_calls_per_function() -> None:
    facts = extract(
        "import os\nfrom app.domain import parse as p\n\n"
        "def test_a():\n    p(1)\n    os.getcwd()\n    local.thing()\n\n"
        "class TestB:\n    def test_m(self):\n        p(2)\n\n"
        "def plain():\n    len([])\n"
    )
    assert facts.function_calls == (
        FunctionCalls("test_a", 4, ("app.domain.parse", "os.getcwd")),
        FunctionCalls("TestB.test_m", 10, ("app.domain.parse",)),
    )
