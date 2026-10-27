"""Each own-check rule, against a tiny clean repository that is then broken one way at a time."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from archlint_helpers import CLASSIFY, mini_policy, ring_with, write_files

from archlint import baseline
from archlint.adapters import FsScanner
from archlint.domain import Violation
from archlint.rules import check_modules


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
    assert rules(found) == {"internal-import", "circular-import"}
    assert "app.adapters" in found[0].detail


def test_same_ring_import_cycle_fails_even_when_peer_imports_are_allowed(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from . import ports\n"})
    found = run(repo, classify={"app": "domain", "app.domain": "domain", "app.ports": "domain"})
    cycles = [violation for violation in found if violation.rule == "circular-import"]
    assert len(cycles) == 1
    assert cycles[0].subject == "app.domain, app.ports"


def test_module_forbid_imports_adds_a_finer_grained_exception(repo: Path) -> None:
    write_files(repo, {"app/adapters.py": "import app.main\n"})
    found = run(repo, module_forbid_imports={"app.adapters": ["app.main"]})
    assert [(v.rule, v.subject) for v in found if v.rule == "internal-import"] == [("internal-import", "app.adapters")]
    assert "forbidden module app.main" in next(v for v in found if v.rule == "internal-import").detail


def test_lazy_import_inside_a_function_counts(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "def f() -> None:\n    import app.adapters\n"})
    assert rules(run(repo)) == {"internal-import", "circular-import"}


def test_type_checking_import_counts(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    from . import adapters\n"})
    assert rules(run(repo)) == {"internal-import", "circular-import"}


def test_absolute_import_of_own_package_is_internal_not_external(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "import app.adapters\n"})
    assert rules(run(repo)) == {"internal-import", "circular-import"}


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


def test_aliased_os_branching_is_detected(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "import os as operating_system\noperating_system.name\n"})
    assert "os-branching" in rules(run(repo, ring=ring_with(domain={"external": ["os"]})))


def test_non_outermost_wildcard_ring_is_a_violation_per_axis(repo: Path) -> None:
    found = run(
        repo,
        ring=ring_with(domain={"external": ["*"], "may_import": ["*"]}),
    )

    assert [(v.rule, v.subject, v.detail) for v in found] == [
        ("wildcard-ring", "domain", "external"),
        ("wildcard-ring", "domain", "may_import"),
    ]


def test_outermost_ring_is_exempt_from_wildcard_ring_rule(repo: Path) -> None:
    found = run(repo, ring=ring_with(composition={"external": ["*"], "may_import": ["*"]}))

    assert not [v for v in found if v.rule == "wildcard-ring"]


def test_adding_explicit_allowances_does_not_shrink_a_retained_wildcard(repo: Path) -> None:
    broad = run(repo, ring=ring_with(ports={"may_import": ["*"], "external": ["*"]}))
    narrowed = run(repo, ring=ring_with(ports={"may_import": ["*", "domain"], "external": ["*", "requests"]}))

    broad_keys = {v.key for v in broad if v.rule == "wildcard-ring"}
    narrowed_keys = {v.key for v in narrowed if v.rule == "wildcard-ring"}
    assert broad_keys == narrowed_keys == {
        "wildcard-ring|ports|external",
        "wildcard-ring|ports|may_import",
    }


def test_removing_wildcard_ring_policy_shrinks_the_baseline(repo: Path) -> None:
    before = run(repo, ring=ring_with(domain={"external": ["*"], "may_import": ["*"]}))
    after = run(repo, ring=ring_with(domain={"external": ["@pure", "os"], "may_import": []}))

    ratchet = baseline.apply(after, {finding.key for finding in before})
    assert ratchet.new == []
    assert ratchet.stale == ["wildcard-ring|domain|external", "wildcard-ring|domain|may_import"]


def test_unclassified_module_is_a_violation(repo: Path) -> None:
    write_files(repo, {"app/newcomer.py": "X = 1\n"})
    assert [(v.rule, v.subject) for v in run(repo)] == [("unclassified", "app.newcomer")]


def test_relative_import_above_the_root_is_reported(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "from ... import x\n"})
    assert "bad-import" in rules(run(repo))


def test_from_dot_import_names_submodules(repo: Path) -> None:
    write_files(repo, {"app/ports.py": "from . import adapters\n"})  # reaches app.adapters, not just the package
    assert rules(run(repo)) == {"internal-import", "circular-import"}


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


def test_entry_script_metadata_is_read_from_a_file_with_crlf_line_endings(repo: Path) -> None:
    """Found by running ArchLint on DeepInterview: the scripts there are CRLF and the PEP 723 regex missed the block."""
    script = b'# /// script\r\n# dependencies = ["psutil"]\r\n# ///\r\nimport sys\r\n'
    (repo / "run.py").write_bytes(script)
    scan = FsScanner().scan(mini_policy(**ENTRY), repo)
    assert scan.entry_deps["run"] == ("psutil",)
