"""``unreachable-patch`` (#226): a test patches ``X.name`` while a module it reaches calls ``name`` through ``from X import name``.

The caller bound the function object at import time, so the patch of ``X`` never reaches it. Joined statically from the
scanned modules and the files under the configured tests folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

from arborescent import scanner
from arborescent.adapters import FsScanner
from arborescent.domain import PolicyError, Rule, Violation
from arborescent.engine import run_own
from arborescent.facts import extract
from arborescent.scan_cache_codec import facts_from_json, facts_to_json
from arborescent.selftest import plan, selftest

REPO = "def load() -> int:\n    return 1\n\n\ndef use() -> int:\n    return load()\n"
CALLER = "from app.repo import load\n\n\ndef backfill() -> int:\n    return load()\n"
TEST = "from app import caller, repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'load', lambda: 2)\n    caller.backfill()\n"
CLASSIFY = {"app*": "composition"}


def policy_for(tests: object = "tests", **overrides: Any):  # type: ignore[no-untyped-def]
    practices = {} if tests is None else {"unreachable-patch": tests}
    extra = overrides.pop("practices", {})
    return mini_policy(classify=CLASSIFY, practices={**practices, **extra}, **overrides)


def check(root: Path, files: dict[str, str], tests: object = "tests", **overrides: Any) -> list[Violation]:
    write_files(root, {"app/__init__.py": "", "app/repo.py": REPO, "app/caller.py": CALLER, **files})
    found = run_own(policy_for(tests, **overrides), root, services(), practices=False)[0]
    return [v for v in found if v.rule == Rule.UNREACHABLE_PATCH]


def one(found: list[Violation]) -> tuple[str, int]:
    assert len(found) == 1, [v.render() for v in found]
    assert "app.repo.load" in found[0].detail
    return found[0].subject, found[0].line


def test_the_patch_that_cannot_reach_the_caller_is_reported(repo: Path) -> None:
    found = check(repo, {"tests/test_x.py": TEST})
    assert one(found) == ("app.caller", 5)
    assert "tests/test_x.py:5" in found[0].render()
    assert "tests" not in found[0].detail  # the baseline key survives renaming the test


@pytest.mark.parametrize(
    "body",
    [
        "from app import repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'load', None)\n",
        "import app.repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(app.repo, 'load', None)\n",
        "import app.repo as r\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(r, 'load', None, raising=False)\n",
        "def test_x(monkeypatch):\n    monkeypatch.setattr('app.repo.load', None)\n",
        "from unittest import mock\n\n\ndef test_x():\n    with mock.patch('app.repo.load'):\n        pass\n",
        "from unittest.mock import patch\n\n\n@patch('app.repo.load')\ndef test_x(fake):\n    pass\n",
        "from unittest.mock import patch\nfrom app import repo\n\n\ndef test_x():\n    with patch.object(repo, 'load'):\n        pass\n",
        "import mock\nfrom app import repo\n\n\ndef test_x():\n    mock.patch.object(repo, attribute='load').start()\n",
        "def test_x(mocker):\n    mocker.patch(target='app.repo.load')\n",
        "from app import repo\n\n\ndef test_x(mocker):\n    mocker.patch.object(repo, 'load')\n",
        "from app import repo\n\n\ndef test_x(monkeypatch):\n    with monkeypatch.context() as m:\n        m.setattr(repo, 'load', None)\n",
    ],
)
def test_each_patch_form_counts(repo: Path, body: str) -> None:
    assert one(check(repo, {"tests/test_x.py": "import app.caller\n" + body})) == ("app.caller", 5)


@pytest.mark.parametrize(
    "caller",
    [
        "from app.repo import load as fetch\n\n\ndef backfill() -> int:\n    return fetch()\n",
        "from .repo import load\n\n\ndef backfill() -> int:\n    return load()\n",
        "try:\n    from app.repo import load\nexcept ImportError:\n    load = None\n\n\nclass C:\n    def f(self) -> int:\n        return load()\n",
        "from app.repo import load\n\nbackfill = lambda: load()\n",
    ],
)
def test_aliased_relative_and_method_callers_count(repo: Path, caller: str) -> None:
    files = {"app/caller.py": caller, "tests/test_x.py": TEST}
    found = check(repo, files)
    if caller.startswith("try"):  # the module also assigns `load`, so the binding is not certainly the import
        assert found == []
    else:
        assert [v.subject for v in found] == ["app.caller"]


@pytest.mark.parametrize(
    "caller",
    [
        "from app import repo\n\n\ndef backfill() -> int:\n    return repo.load()\n",  # the fix
        "import app.repo\n\n\ndef backfill() -> int:\n    return app.repo.load()\n",
        "def backfill() -> int:\n    from app.repo import load\n\n    return load()\n",  # bound at call time: the patch reaches it
        "from app.repo import load\n\nCACHED = load()\n",  # runs at import, before any patch
        "from app.repo import load\n\n\ndef backfill(f=load()) -> int:\n    return f\n",
        "from app.repo import load\n\n\ndef backfill(load=None) -> int:\n    return load()\n",  # shadowed
        "from app.repo import load\n\nWRAPPED = load\n",  # not a call
        "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from app.repo import load\n\n\ndef backfill() -> int:\n    return load()\n",
    ],
)
def test_callers_the_patch_does_reach_or_that_are_not_certain_are_not_reported(repo: Path, caller: str) -> None:
    assert check(repo, {"app/caller.py": caller, "tests/test_x.py": TEST}) == []


@pytest.mark.parametrize(
    "body",
    [
        "from app import caller\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(caller, 'load', None)\n",  # patches the caller
        # patches both ends
        "from app import caller, repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'load', None)\n    monkeypatch.setattr(caller, 'load', None)\n",
        # never reaches the caller: only the origin's own behaviour is under test
        "from app import repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'load', None)\n    repo.use()\n",
        "import app.caller\nfrom app import repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'other', None)\n",
        "import app.caller\nimport time\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(time, 'sleep', None)\n",
        "import app.caller\n\n\ndef test_x(monkeypatch, repo):\n    monkeypatch.setattr(repo, 'load', None)\n    monkeypatch.setenv('A', 'b')\n    monkeypatch.setattr()\n    monkeypatch.setattr('load', 1)\n",
        "from app import caller, repo\n\n\ndef test_x(thing, name):\n    thing.setattr(repo, 'load', None)\n    mock = thing\n    mock.patch.object(repo, name)\n",
        "",
    ],
)
def test_patches_that_are_fine_or_unknown_are_not_reported(repo: Path, body: str) -> None:
    assert check(repo, {"tests/test_x.py": body}) == []


def test_the_test_may_reach_the_caller_indirectly_through_production_conftest_or_a_helper(repo: Path) -> None:
    patch = "from app import repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'load', None)\n"
    through = {"app/cli.py": "def main() -> None:\n    from app import caller\n", "tests/test_x.py": "import app.cli\n" + patch}
    assert one(check(repo, through)) == ("app.caller", 5)
    write_files(repo, {"tests/test_x.py": patch, "tests/unit/test_y.py": patch, "tests/conftest.py": "import app.caller\n"})
    assert one(check(repo, {})) == ("app.caller", 5)
    (repo / "tests" / "conftest.py").unlink()
    helper = {"tests/my_helpers.py": "from app import caller\n", "tests/test_x.py": "from my_helpers import caller\n" + patch}
    assert one(check(repo, helper)) == ("app.caller", 5)


def test_a_helper_that_shares_a_production_name_is_not_followed_for_a_project_import(repo: Path) -> None:
    patch = "from app import repo\n\n\ndef test_x(monkeypatch):\n    monkeypatch.setattr(repo, 'load', None)\n"
    assert check(repo, {"tests/repo.py": "from app import caller\n", "tests/test_x.py": patch}) == []
    helper = {"tests/helpers.py": "from app import caller\n", "tests/test_x.py": "from . import helpers\n" + patch}
    assert one(check(repo, helper)) == ("app.caller", 5)


def test_it_is_off_unless_configured_and_exempt_silences_a_binding(repo: Path) -> None:
    files = {"tests/test_x.py": TEST}
    assert check(repo, files, tests=None) == []
    assert check(repo, files, practices={"unreachable-patch-exempt": ["app.caller.load"]}) == []
    assert check(repo, files, practices={"unreachable-patch-exempt": ["app.caller"]}) == []
    assert check(repo, files, practices={"unreachable-patch-exempt": ["app.repo"]}) != []


@pytest.mark.parametrize("value", [True, 3, ["tests"], "/", ""])
def test_the_tests_folder_must_be_a_folder_name(repo: Path, value: object) -> None:
    if value == "":  # an empty string is an unset practice, as for the other practices
        assert check(repo, {"tests/test_x.py": TEST}, tests=value) == []
    else:
        with pytest.raises(PolicyError, match="unreachable-patch"):
            _ = check(repo, {}, tests=value)


def test_a_tests_folder_with_no_python_files_is_reported_not_silently_clean(repo: Path) -> None:
    found = check(repo, {}, tests="test")
    assert [(v.subject, "no Python files" in v.detail) for v in found] == [("test", True)]


def test_an_excluded_test_file_that_does_not_parse_is_skipped(repo: Path) -> None:
    files = {"tests/broken.py": "def broken(:\n", "tests/test_x.py": TEST}
    assert one(check(repo, files, exclude=["tests"])) == ("app.caller", 5)


def test_a_changed_run_skips_the_whole_repository_check(repo: Path) -> None:
    write_files(repo, {"app/repo.py": REPO, "app/caller.py": CALLER, "tests/test_x.py": TEST})
    found = run_own(policy_for(), repo, services(), frozenset({"app/caller.py"}), practices=False)[0]
    assert [v for v in found if v.rule == Rule.UNREACHABLE_PATCH] == []


@pytest.mark.parametrize("layout", ["outside sources", "excluded", "inside sources"])
def test_every_file_is_parsed_once_and_a_warm_run_parses_nothing(
    repo: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch, layout: str,
) -> None:
    write_files(repo, {"app/repo.py": REPO, "app/caller.py": CALLER, "tests/test_x.py": TEST})
    overrides: dict[str, Any] = {
        "outside sources": {},
        "excluded": {"exclude": ["tests"]},  # also parsed for `mixed-purity-excluded`
        "inside sources": {"sources": ["app", "tests"]},
    }[layout]
    seen: list[str] = []
    real = scanner.extract
    monkeypatch.setattr(scanner, "extract", lambda src, name="": (seen.append(name), real(src, name))[1])
    policy = policy_for(**overrides)
    cached = services(cache=tmp_path_factory.mktemp("cache"))
    found = run_own(policy, repo, cached, practices=False)[0]
    assert [v.subject for v in found if v.rule == Rule.UNREACHABLE_PATCH] == ["app.caller"]
    assert len(seen) == len(set(seen)) and "tests/test_x.py" in seen
    seen.clear()
    assert [v.subject for v in run_own(policy, repo, cached, practices=False)[0] if v.rule == Rule.UNREACHABLE_PATCH] == ["app.caller"]
    assert seen == []


def test_the_facts_survive_the_cache() -> None:
    facts = extract(CALLER + "\n\ndef t(monkeypatch):\n    monkeypatch.setattr('a.b.c', 1)\n")
    assert facts.from_calls == ((5, "app.repo.load", "load"),)
    assert facts.patches == ((9, "a.b.c"),)
    again = facts_from_json(facts_to_json(facts))
    assert (again.from_calls, again.patches) == (facts.from_calls, facts.patches)


def test_selftest_probes_the_rule_by_adding_a_test_file(repo: Path) -> None:
    write_files(repo, {**MINI_FILES, "app/repo.py": REPO, "app/caller.py": CALLER, "tests/test_ok.py": "def test_ok():\n    pass\n"})
    off = policy_for(None)
    assert Rule.UNREACHABLE_PATCH not in {m.rule for m in plan(off, FsScanner().scan(off, repo))[0]}
    policy = policy_for()
    report = selftest(policy, repo, services())
    assert any(entry.startswith("[unreachable-patch]") for entry in report.killed)
    assert not any(entry.startswith("[unreachable-patch]") for entry in report.survived)


def test_selftest_picks_a_binding_no_real_test_already_patches_or_says_why_not(repo: Path) -> None:
    write_files(repo, {**MINI_FILES, "app/repo.py": REPO, "app/caller.py": CALLER, "tests/test_x.py": TEST})
    policy = policy_for()
    mutations, skipped = plan(policy, FsScanner().scan(policy, repo))
    assert Rule.UNREACHABLE_PATCH not in {m.rule for m in mutations}
    assert any(entry.startswith("unreachable-patch:") for entry in skipped)
