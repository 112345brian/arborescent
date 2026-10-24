"""Each own-check rule, against a tiny clean repository that is then broken one way at a time."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archlint.adapters import FsScanner
from archlint.domain import Violation
from archlint.rules import check_modules
from conftest import CLASSIFY, ring_with, mini_policy, write_files


def run(root: Path, **overrides: Any) -> list[Violation]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps)


def rules(found: list[Violation]) -> set[str]:
    return {v.rule for v in found}


def test_clean_repo_has_no_violations(repo: Path) -> None:
    assert run(repo) == []


def test_inner_ring_importing_outer_ring_fails(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    found = run(repo)
    assert rules(found) == {"internal-import"}
    assert "app.adapters" in found[0].detail


def test_lazy_import_inside_a_function_counts(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "def f() -> None:\n    import app.adapters\n"})
    assert rules(run(repo)) == {"internal-import"}


def test_type_checking_import_counts(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import adapters\n"})
    assert rules(run(repo)) == {"internal-import"}


def test_absolute_import_of_own_package_is_internal_not_external(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import app.adapters\n"})
    assert rules(run(repo)) == {"internal-import"}


def test_external_allow_list_is_per_ring(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import os\n", "app/adapters.py": "import psutil\n"})
    assert {(v.subject, v.rule) for v in run(repo)} == {("app.domain", "external-import")}


def test_lazy_external_import_counts(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "def f() -> None:\n    import requests\n"})
    assert rules(run(repo)) == {"external-import"}


def test_os_branching_outside_permitted_rings(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import sys\nsys.platform\n", "app/adapters.py": "import os\nos.name\n"})
    found = run(repo, ring=ring_with(ports={"may_import": ["domain"], "external": ["sys"]}))
    assert {(v.subject, v.rule) for v in found} == {("app.ports", "os-branching")}


def test_from_sys_import_platform_is_os_branching(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from sys import platform\n"})
    assert "os-branching" in rules(run(repo, ring=ring_with(domain={"external": ["sys"]})))


def test_unclassified_module_is_a_violation(repo: Path) -> None:
    write_files(repo, {"app/newcomer.py": "X = 1\n"})
    assert [(v.rule, v.subject) for v in run(repo)] == [("unclassified", "app.newcomer")]


def test_relative_import_above_the_root_is_reported(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "from ... import x\n"})
    assert "bad-import" in rules(run(repo))


def test_from_dot_import_names_submodules(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "from . import adapters\n"})  # reaches app.adapters, not just the package
    assert rules(run(repo)) == {"internal-import"}


ENTRY = {
    "sources": ["app", "run.py"],
    "entry_scripts": ["run"],
    "classify": {**CLASSIFY, "run": "composition"},
}


def test_entry_script_must_declare_every_third_party_package(repo: Path) -> None:
    write_files(
        repo,
        {
            "run.py": '# /// script\n# dependencies = ["requests"]\n# ///\nfrom app import main\n',
            "app/adapters.py": "import psutil\nimport requests\n",
        },
    )
    found = run(repo, **ENTRY, ring=ring_with(adapters={"may_import": ["domain", "ports"], "external": ["psutil", "requests"], "os_branching": True}))
    assert [(v.rule, v.subject) for v in found] == [("undeclared-entry-dependency", "run")]
    assert "psutil" in found[0].detail


def test_distribution_mapping_is_used_for_entry_scripts(repo: Path) -> None:
    write_files(
        repo,
        {"run.py": '# /// script\n# dependencies = ["lifecycle-runner"]\n# ///\n', "app/adapters.py": "import lifecycle_runner\n"},
    )
    found = run(
        repo, **ENTRY, dependencies={"lifecycle_runner": "lifecycle-runner"},
        ring=ring_with(adapters={"may_import": ["domain", "ports"], "external": ["lifecycle_runner"], "os_branching": True}),
    )  # fmt: skip
    assert found == []


def test_entry_script_without_metadata_block_fails(repo: Path) -> None:
    write_files(repo, {"run.py": "import sys\n"})
    assert [v.rule for v in run(repo, **ENTRY)] == ["undeclared-entry-dependency"]
