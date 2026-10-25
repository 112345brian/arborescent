"""Stdlib detection follows the policy's target Python, not the interpreter running archlint."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest
from archlint_helpers import mini_policy, write_files

from archlint.adapters import FsScanner, LocalFileSystem
from archlint.domain import Policy, PolicyError
from archlint.practices import undeclared_imports
from archlint.rules import check_modules
from archlint.stdlib import SUPPORTED_PYTHONS, stdlib_for


def test_supported_versions_are_listed_oldest_first() -> None:
    assert SUPPORTED_PYTHONS[0] == "3.11"
    assert list(SUPPORTED_PYTHONS) == sorted(SUPPORTED_PYTHONS, key=lambda v: int(v.split(".")[1]))


def test_table_matches_the_running_interpreter_when_it_is_supported() -> None:
    """Drift guard: a new Python minor means the table needs a refresh (docs/stdlib-table.md)."""
    running = f"{sys.version_info.major}.{sys.version_info.minor}"
    if running not in SUPPORTED_PYTHONS:
        pytest.skip(f"Python {running} is not in the table")
    assert stdlib_for(running) == frozenset(sys.stdlib_module_names)


@pytest.mark.parametrize(
    ("module", "versions"),
    [
        ("distutils", {"3.11"}),
        ("imp", {"3.11"}),
        ("asyncore", {"3.11"}),
        ("cgi", {"3.11", "3.12"}),
        ("telnetlib", {"3.11", "3.12"}),
        ("annotationlib", {"3.14"}),
        ("compression", {"3.14"}),
        ("_interpreters", {"3.13", "3.14"}),
        ("json", {"3.11", "3.12", "3.13", "3.14"}),
        ("requests", set()),
    ],
)
def test_version_dependent_membership(module: str, versions: set[str]) -> None:
    assert {v for v in SUPPORTED_PYTHONS if module in stdlib_for(v)} == versions


@pytest.mark.parametrize("bad", ["3", "3.99", "abc", "", "3.10", "python3.11"])
def test_unsupported_python_is_a_policy_error_listing_the_supported_versions(bad: str) -> None:
    with pytest.raises(PolicyError, match=r"3\.11.*3\.14"):
        _ = mini_policy(python=bad)


def test_a_toml_float_is_read_as_its_version_string() -> None:
    assert mini_policy(python=3.12).python == "3.12"
    with pytest.raises(PolicyError):  # TOML `3.10` is the float 3.1
        _ = mini_policy(python=3.10)


def test_directly_built_policies_are_validated_too() -> None:
    p = mini_policy()
    with pytest.raises(PolicyError):
        _ = Policy(rings=p.rings, classify=p.classify, python="3.99")


def test_default_target_is_3_11_whatever_the_host_runs() -> None:
    assert mini_policy().python == "3.11"
    assert "distutils" in mini_policy().stdlib


def _found(root: Path, python: str, **overrides: Any) -> set[tuple[str, str]]:
    policy = mini_policy(python=python, preset="standard", **overrides)
    scan = FsScanner().scan(policy, root)
    return {(v.rule, v.subject) for v in check_modules(policy, scan.modules, scan.known, scan.entry_deps)}


def test_a_module_removed_from_the_stdlib_is_third_party_for_a_newer_target(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import distutils\n"})
    assert _found(repo, "3.11") == set()  # plain stdlib is free under `standard`
    assert _found(repo, "3.12") == {("external-import", "app.domain")}


def test_a_module_added_to_the_stdlib_is_third_party_for_an_older_target(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import annotationlib\n"})
    assert _found(repo, "3.11") == {("external-import", "app.domain")}
    assert _found(repo, "3.14") == set()


def test_undeclared_imports_follow_the_target_python(repo: Path) -> None:
    write_files(
        repo,
        {
            "app/adapters.py": "import imp\n",
            "pyproject.toml": '[project]\nname = "app"\nversion = "1"\ndependencies = []\n',
        },
    )
    results: dict[str, list[str]] = {}
    for python in ("3.11", "3.12"):
        policy = mini_policy(python=python)
        scan = FsScanner().scan(policy, repo)
        results[python] = [v.subject for v in undeclared_imports(LocalFileSystem(), repo, policy, scan.modules, scan.known, "pyproject.toml")]
    assert results == {"3.11": [], "3.12": ["imp"]}
