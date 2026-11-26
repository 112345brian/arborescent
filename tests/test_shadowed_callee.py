"""``shadowed-callee`` (#215): a local assigned from a call to the same-named module-level name (a "make it public" rename).

``_rows`` renamed to ``rows`` turns ``rows = rows(...)`` into a local that is called before it is bound.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

from arborescent.definition_facts import shadowed_callees
from arborescent.domain import Rule, Violation
from arborescent.engine import run_own
from arborescent.selftest import plan, selftest

ON = {"shadowed-callee": True}


def names(source: str) -> list[tuple[str, str]]:
    return [(function, name) for _, function, name in shadowed_callees(source)]


@pytest.mark.parametrize(
    "source",
    [
        "from a import rows\ndef f():\n    rows = rows(1)\n",
        "import rows\ndef f():\n    rows = rows()\n",
        "from .a import _x as rows\ndef f():\n    rows = rows()\n",
        "def rows():\n    pass\ndef f():\n    rows = rows()\n",
        "class rows:\n    pass\ndef f():\n    rows = rows()\n",
        "from a import rows\ndef f():\n    rows: list = rows()\n",
        "from a import rows\nasync def f():\n    rows = await rows()\n",
        "from a import rows\ndef f():\n    rows = sorted(rows())\n",
        "from a import rows\ndef f():\n    rows, total = rows()\n",
        "from a import rows\ndef f():\n    if (rows := rows()):\n        pass\n",
        "from a import rows\ndef f():\n    for x in y:\n        rows = rows(x)\n",
        "try:\n    from a import rows\nexcept ImportError:\n    rows = None\ndef f():\n    rows = rows()\n",
        "from a import rows\ndef f():\n    rows = rows()\n    return rows\n",
    ],
)
def test_each_shape_is_found(source: str) -> None:
    assert names(source) == [("f", "rows")]


def test_methods_and_nested_functions_are_named_by_their_path() -> None:
    source = "from a import store\nclass K:\n    def m(self):\n        store = store()\ndef o():\n    def i():\n        store = store()\n"
    assert names(source) == [("K.m", "store"), ("o.i", "store")]


@pytest.mark.parametrize(
    "source",
    [
        "from a import rows\nrows = rows()\n",  # module level rebinding works
        "from a import rows\nclass K:\n    rows = rows()\n",  # a class body falls back to the global
        "from a import rows\ndef f(rows):\n    rows = rows()\n",  # the parameter is called
        "from a import rows\ndef f():\n    global rows\n    rows = rows()\n",
        "from a import rows\ndef o():\n    rows = 1\n    def i():\n        nonlocal rows\n        rows = rows()\n",
        "def f():\n    from a import rows\n    rows = rows()\n",
        "from a import rows\ndef f():\n    from a import rows\n    rows = rows()\n",
        "from a import rows\ndef f(x):\n    rows = x\n    rows = rows()\n",
        "from a import rows\ndef f():\n    rows = other.rows()\n",
        "from a import rows\ndef f():\n    rows = [r for r in other()]\n",
        "from a import rows\ndef f():\n    rows = [rows() for rows in other()]\n",
        "from a import rows\ndef f():\n    rows = lambda: rows()\n",
        "from a import rows\ndef f():\n    rows = (rows() for _ in x)\n",
        "from a import rows\ndef f():\n    result = rows()\n",
        "def f():\n    rows = rows()\n",  # no module-level name: not this rule
        "",
        "def f(:\n",
        "x\x00 = 1\n",
    ],
)
def test_legitimate_or_unknown_shapes_are_not_reported(source: str) -> None:
    assert names(source) == []


def test_a_byte_order_mark_a_long_ladder_and_many_functions_do_not_break_it() -> None:
    assert names("\ufefffrom a import r\ndef f():\n    r = r()\n") == [("f", "r")]
    ladder = "def g(x):\n    if x:\n        pass\n" + "    elif x:\n        pass\n" * 2000  # as tests/test_deep_sources.py
    many = "".join(f"def f{n}():\n    r = r()\n" for n in range(1500))
    assert len(names("from a import r\n" + ladder + many)) == 1500


def found(root: Path, files: dict[str, str], practices: dict[str, object] | None = None, only: frozenset[str] | None = None) -> list[Violation]:
    write_files(root, {**MINI_FILES, **files})
    policy = mini_policy(practices=ON if practices is None else practices)
    return [v for v in run_own(policy, root, services(), only, practices=False)[0] if v.rule == Rule.SHADOWED_CALLEE]


BROKEN = {"app/adapters.py": "from app.domain import VALUE\n\n\ndef f() -> None:\n    VALUE = VALUE()\n"}


def test_the_finding_names_the_function_and_has_a_stable_key(repo: Path) -> None:
    (violation,) = found(repo, BROKEN)
    assert (violation.subject, violation.line) == ("app.adapters", 5)
    assert "[f]" in violation.detail and "'VALUE'" in violation.detail and "5" not in violation.key


def test_it_is_off_by_default_and_still_runs_on_a_changed_subset(repo: Path) -> None:
    assert found(repo, BROKEN, {}) == []
    assert len(found(repo, BROKEN, only=frozenset({"app/adapters.py"}))) == 1


def test_two_assignments_in_one_function_are_one_finding(repo: Path) -> None:
    source = "from app.domain import VALUE\n\n\ndef f(x) -> None:\n    if x:\n        VALUE = VALUE()\n    else:\n        VALUE = VALUE()\n"
    assert len(found(repo, {"app/adapters.py": source})) == 1


def test_selftest_probes_it_only_when_enabled(repo: Path) -> None:
    write_files(repo, MINI_FILES)
    policy = mini_policy(practices=ON)
    _, scan = run_own(policy, repo, services(), practices=False)
    assert [m.rule for m in plan(policy, scan)[0]].count(Rule.SHADOWED_CALLEE) == 1
    assert Rule.SHADOWED_CALLEE not in {m.rule for m in plan(mini_policy(), scan)[0]}
    report = selftest(policy, repo, services())
    assert any("[shadowed-callee]" in line for line in report.killed), report.survived
