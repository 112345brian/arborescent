"""#99: developer tooling is a role of its own, not an exclusion."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files
from test_coverage import GitFiles, make_services

from arborescent.adapters import FsScanner
from arborescent.domain import PolicyError
from arborescent.engine import run_own
from arborescent.facts import extract


def findings(root: Path, **overrides: object) -> set[tuple[str, str]]:
    policy = mini_policy(**overrides)
    found, _ = run_own(policy, root, make_services(GitFiles(available=False)), practices=False)
    return {(v.rule, v.subject) for v in found}


def tool_repo(repo: Path, tool: str = "import os\nfrom app import domain\nos.getenv('X')\n") -> Path:
    write_files(repo, {"devtools/__init__.py": "", "devtools/scan.py": tool})
    return repo


def test_tooling_needs_no_ring_may_import_product_code_and_is_not_ring_checked(repo: Path) -> None:
    assert not {f for f in findings(tool_repo(repo), dev_tools=["devtools"]) if f[1].startswith("devtools")}


def test_without_dev_tools_the_same_folder_is_unclassified(repo: Path) -> None:
    assert ("unclassified", "devtools.scan") in findings(tool_repo(repo))


def test_product_code_importing_tooling_fails(repo: Path) -> None:
    tool_repo(repo)
    write_files(repo, {"app/domain.py": "import devtools.scan\n"})

    found = findings(repo, dev_tools=["devtools"])

    assert ("dev-tool-import", "app.domain") in found


def test_from_import_and_lazy_import_of_tooling_also_fail(repo: Path) -> None:
    tool_repo(repo)
    write_files(repo, {"app/domain.py": "def f():\n    from devtools import scan\n    return scan\n"})

    assert ("dev-tool-import", "app.domain") in findings(repo, dev_tools=["devtools"])


def test_tooling_importing_tooling_is_fine(repo: Path) -> None:
    tool_repo(repo)
    write_files(repo, {"devtools/other.py": "from devtools import scan\n"})

    assert not {f for f in findings(repo, dev_tools=["devtools"]) if f[0] == "dev-tool-import"}


def test_annotation_cannot_grant_the_exemption_outside_dev_tools(repo: Path) -> None:
    write_files(repo, {"app/domain.py": '__arbor__ = {"role": "dev_tool"}\n'})

    assert ("dev-tool-misplaced", "app.domain") in findings(repo, dev_tools=["devtools"])


def test_annotated_module_below_a_dev_tools_path_is_accepted(repo: Path) -> None:
    tool_repo(repo, '__arbor__ = {"role": "dev_tool"}\n')

    found = findings(repo, dev_tools=["devtools"])

    assert not {f for f in found if f[1].startswith("devtools")}


@pytest.mark.parametrize("value", ["tool", 3])
def test_role_must_be_dev_tool(value: object) -> None:
    facts = extract(f'__arbor__ = {{"role": {value!r}}}\n')
    assert facts.arbor_error is not None and "role" in facts.arbor_error


def test_role_and_ring_can_be_combined_and_ring_is_still_required_without_role() -> None:
    assert extract('__arbor__ = {"ring": "domain", "role": "dev_tool"}\n').arbor_ring == "domain"
    assert extract('__arbor__ = {}\n').arbor_error == '__arbor__ dictionary is missing "ring"'


def test_dev_tools_must_be_a_list_of_strings() -> None:
    with pytest.raises(PolicyError, match="dev_tools"):
        mini_policy(dev_tools="devtools")


def test_dev_tools_paths_match_whole_segments() -> None:
    policy = mini_policy(dev_tools=["devtools", "scripts/run.py"])
    assert policy.is_dev_tool_path("devtools/a/b.py")
    assert policy.is_dev_tool_path("scripts/run.py")
    assert not policy.is_dev_tool_path("devtools_extra/a.py")
    assert not policy.is_dev_tool_path("scripts/other.py")


def test_scan_reports_tool_modules_and_keeps_them_in_the_suppression_inventory(repo: Path) -> None:
    tool_repo(repo, "import os  # noqa: F401\n")
    scan = FsScanner().scan(mini_policy(dev_tools=["devtools"]), repo)

    assert scan.dev_tool_modules == frozenset({"devtools", "devtools.scan"})
    assert scan.modules["devtools.scan"].facts.suppressions
