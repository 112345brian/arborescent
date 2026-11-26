"""``unresolved-patch-target`` (#215): a test patches ``module.name`` and the module no longer has ``name``.

It shares patch-target extraction and the tests-folder reading with ``unreachable-patch`` (#226).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, services, write_files

from arborescent.call_facts import optional_patch_lines
from arborescent.definition_facts import module_attributes
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.engine import run_own
from arborescent.selftest import plan, selftest

REPO = "import os\nfrom json import dumps\n\nLIMIT: int = 1\n\n\ndef load() -> int:\n    return 1\n\n\nclass Store:\n    def get(self) -> None:\n        pass\n"
CLASSIFY = {"app*": "composition"}


def policy_for(tests: object = "tests", **practices: object):  # type: ignore[no-untyped-def]
    base = {} if tests is None else {"unresolved-patch-target": tests}
    return mini_policy(classify=CLASSIFY, practices={**base, **practices})


def run(root: Path, files: dict[str, str], policy: Any = None) -> list[Violation]:
    write_files(root, {"app/__init__.py": "", "app/repo.py": REPO, **files})
    return run_own(policy or policy_for(), root, services(), practices=False)[0]


def check(root: Path, files: dict[str, str], policy: Any = None) -> list[Violation]:
    return [v for v in run(root, files, policy) if v.rule == Rule.UNRESOLVED_PATCH_TARGET]


def patching(target: str) -> dict[str, str]:
    return {"tests/test_x.py": f"def test_x(monkeypatch):\n    monkeypatch.setattr({target!r}, None)\n"}


@pytest.mark.parametrize(
    "body",
    [
        "def test_x(monkeypatch):\n    monkeypatch.setattr('app.repo.gone', None)\n",
        "from unittest import mock\n\n\ndef test_x():\n    with mock.patch('app.repo.gone'):\n        pass\n",
        "from unittest.mock import patch\n\n\n@patch('app.repo.gone')\ndef test_x(fake):\n    pass\n",
        "from app import repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'gone', None)\n",
        "def test_x(mocker):\n    mocker.patch('app.repo.gone')\n",
    ],
)
def test_each_patch_form_with_a_missing_name_is_reported(repo: Path, body: str) -> None:
    (violation,) = check(repo, {"tests/test_x.py": body})
    assert violation.subject == "tests/test_x.py" and "app.repo.gone" in violation.detail and violation.line > 0


@pytest.mark.parametrize("target", ["app.gone.load", "app.repo.Gone.get", "app.repo.gone.deeper"])
def test_a_moved_module_class_or_attribute_chain_is_reported(repo: Path, target: str) -> None:
    assert len(check(repo, patching(target))) == 1


@pytest.mark.parametrize(
    "target",
    [
        "app.repo.load", "app.repo.LIMIT", "app.repo.os", "app.repo.dumps", "app.repo.Store", "app.repo.Store.get",
        "app.repo.Store.anything",  # members of a class are not resolved (inheritance)
        "app.repo.os.path.exists",  # nor attributes of something the module imports
        "app.repo", "app.repo.__name__", "app.repo.open", "app.repo.print",  # a submodule, a dunder, builtins mock creates
        "os.getcwd", "nowhere.thing",  # not first-party
    ],
)
def test_resolvable_or_unknown_targets_are_not_reported(repo: Path, target: str) -> None:
    assert check(repo, patching(target)) == []


@pytest.mark.parametrize(
    "module",
    [
        "from os import *\n",
        "def __getattr__(name):\n    return name\n",
        "__getattr__ = make()\n",
        "globals().update(table())\n",
        "def f():\n    globals()['gone'] = 1\n",
        "import sys\nsetattr(sys.modules[__name__], 'gone', 1)\n",
        "exec(code())\n",
        "def f():\n    global gone\n    gone = 1\n",
        "if True:\n    gone = 1\n",
        "try:\n    import gone\nexcept ImportError:\n    gone = None\n",
        "for gone in range(1):\n    pass\n",
        "with open('x') as gone:\n    pass\n",
        "x, (gone, *rest) = y\n",
        "if (gone := 1):\n    pass\n",
        "gone: int = 1\n",
    ],
)
def test_a_module_that_may_define_the_name_is_not_reported(repo: Path, module: str) -> None:
    assert check(repo, {"app/other.py": module, **patching("app.other.gone")}) == []


@pytest.mark.parametrize(
    "module",
    [
        "", "gone: int\n", "def f():\n    gone = 1\n", "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import gone\n",
        "def f(args):\n    return vars(args)\n",  # another object's namespace
        "def f(code, names):\n    exec(code, names)\n",
    ],
)
def test_a_module_that_cannot_define_the_name_is_reported(repo: Path, module: str) -> None:
    assert len(check(repo, {"app/other.py": module, **patching("app.other.gone")})) == 1


def test_module_attributes_of_unparsable_sources_are_unknown() -> None:
    assert module_attributes("x\x00 = 1\n") is None
    assert module_attributes("﻿a = 1\n") == frozenset({"a"})


@pytest.mark.parametrize(
    "body",
    [
        "def test_x(monkeypatch):\n    monkeypatch.setattr('app.repo.gone', None, raising=False)\n",
        "def test_x(monkeypatch):\n    monkeypatch.setattr('app.repo.gone', None, False)\n",
        "from app import repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'gone', None, False)\n",
        "def test_x(mocker, flag):\n    mocker.patch('app.repo.gone', create=True)\n",
        "from app import repo\nfrom unittest.mock import patch\n\n\ndef test_x():\n    patch.object(repo, 'gone', create=True)\n",
    ],
)
def test_a_patch_that_is_allowed_to_create_the_name_is_not_reported(repo: Path, body: str) -> None:
    assert check(repo, {"tests/test_x.py": body}) == []


def test_a_strict_patch_beside_a_creating_one_is_still_reported(repo: Path) -> None:
    body = "def test_x(mocker):\n    mocker.patch('app.repo.gone', create=True)\n    mocker.patch('app.repo.gone')\n"
    (violation,) = check(repo, {"tests/test_x.py": body})
    assert violation.line == 3


def test_explicit_strictness_is_not_optional() -> None:
    source = "patch('a.b', create=False)\nm.setattr('a.b', 1, raising=True)\nm.setattr(o, 'b', 1)\n"
    assert optional_patch_lines(source) == frozenset()
    assert optional_patch_lines("broken(:") == frozenset()


def test_a_submodule_that_is_only_on_disk_is_not_reported(repo: Path) -> None:
    policy = mini_policy(classify=CLASSIFY, practices={"unresolved-patch-target": "tests"}, exclude=["app/vendored"])
    assert check(repo, {"app/vendored/__init__.py": "", **patching("app.vendored.thing")}, policy) == []


def test_every_test_to_retarget_is_listed_and_the_key_ignores_lines(repo: Path) -> None:
    found = check(repo, {**patching("app.repo.gone"), "tests/test_y.py": "\n" + patching("app.repo.gone")["tests/test_x.py"]})
    assert sorted((v.subject, v.line) for v in found) == [("tests/test_x.py", 2), ("tests/test_y.py", 3)]
    assert len({v.detail for v in found}) == 1


@pytest.mark.parametrize("exempt", ["app.repo.gone", "app.repo"])
def test_exempt_names_a_target_or_a_module(repo: Path, exempt: str) -> None:
    assert check(repo, patching("app.repo.gone"), policy_for(**{"unresolved-patch-target-exempt": [exempt]})) == []


def test_it_is_off_unless_configured_and_skipped_under_changed(repo: Path) -> None:
    assert check(repo, patching("app.repo.gone"), policy_for(None)) == []
    changed = run_own(policy_for(), repo, services(), frozenset({"app/repo.py"}), practices=False)[0]
    assert not [v for v in changed if v.rule == Rule.UNRESOLVED_PATCH_TARGET]


@pytest.mark.parametrize("value", [True, 3, ["tests"], "/"])
def test_the_tests_folder_must_be_a_folder_name(repo: Path, value: object) -> None:
    with pytest.raises(PolicyError, match="unresolved-patch-target"):
        run(repo, {}, policy_for(value))


def test_a_folder_with_no_python_files_is_reported(repo: Path) -> None:
    (violation,) = check(repo, {}, policy_for("nothing"))
    assert violation.subject == "nothing"


CALLER = "from app.repo import load\n\n\ndef go() -> int:\n    return load()\n"


def test_each_practice_reads_only_its_own_tests_folder(repo: Path) -> None:
    files = {
        "app/caller.py": CALLER,
        "unit/test_a.py": "import app.caller\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr('app.repo.load', None)\n",
        "other/test_b.py": "def test_x(monkeypatch):\n    monkeypatch.setattr('app.repo.gone', None)\n",
    }
    found = run(repo, files, policy_for("other", **{"unreachable-patch": "unit"}))
    assert [v.subject for v in found if v.rule == Rule.UNRESOLVED_PATCH_TARGET] == ["other/test_b.py"]
    (unreachable,) = [v for v in found if v.rule == Rule.UNREACHABLE_PATCH]
    assert "unit/test_a.py" in unreachable.render() and "other/" not in unreachable.render()


def test_an_unreachable_patch_in_the_other_folder_is_not_seen(repo: Path) -> None:
    files = {
        "app/caller.py": CALLER,
        "unit/test_ok.py": "def test_ok():\n    pass\n",
        "other/test_b.py": "import app.caller\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr('app.repo.load', None)\n",
    }
    found = run(repo, files, policy_for("other", **{"unreachable-patch": "unit"}))
    assert not [v for v in found if v.rule == Rule.UNREACHABLE_PATCH]


def test_selftest_probes_it_by_adding_a_test_file(repo: Path) -> None:
    write_files(repo, {"app/__init__.py": "", "app/repo.py": REPO, "tests/test_ok.py": "def test_ok():\n    pass\n"})
    policy = policy_for()
    _, scan = run_own(policy, repo, services(), practices=False)
    assert Rule.UNRESOLVED_PATCH_TARGET in {m.rule for m in plan(policy, scan)[0]}
    assert Rule.UNRESOLVED_PATCH_TARGET not in {m.rule for m in plan(policy_for(None), scan)[0]}
    report = selftest(policy, repo, services())
    assert any("[unresolved-patch-target]" in line for line in report.killed), report.survived
