"""#99: developer tooling is a role of its own, not an exclusion."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files
from test_coverage import GitFiles, make_services

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
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


def rows(root: Path, **overrides: object) -> list[tuple[str, str, int]]:
    policy = mini_policy(**overrides)
    found, _ = run_own(policy, root, make_services(GitFiles(available=False)), practices=False)
    return [(v.rule, v.subject, v.line) for v in found]


def test_one_import_statement_of_tooling_is_one_violation(repo: Path) -> None:
    tool_repo(repo)
    write_files(repo, {"app/domain.py": "from devtools import scan\n"})

    assert [r for r in rows(repo, dev_tools=["devtools"]) if r[0] == "dev-tool-import"] == [("dev-tool-import", "app.domain", 1)]


@pytest.mark.parametrize("entry", ["./devtools", "devtools/", ".//devtools", "devtools\\"])
def test_dev_tools_entries_are_normalised(repo: Path, entry: str) -> None:
    assert not {f for f in findings(tool_repo(repo), dev_tools=[entry]) if f[1].startswith("devtools")}
    assert mini_policy(dev_tools=[entry]).dev_tools == ("devtools",)


@pytest.mark.parametrize("entry", [".", "..", "../x", "a/../b", "/abs/tools", "C:/tools"])
def test_dev_tools_entries_outside_the_repository_are_refused(entry: str) -> None:
    with pytest.raises(PolicyError, match="dev_tools"):
        mini_policy(dev_tools=[entry])


def test_misplaced_role_is_ignored_when_annotations_are_disabled(repo: Path) -> None:
    write_files(repo, {"app/domain.py": '__arbor__ = {"role": "dev_tool"}\n'})

    assert ("dev-tool-misplaced", "app.domain") in findings(repo)
    assert not {f for f in findings(repo, disabled_tools=["arbor-annotations"]) if f[0] == "dev-tool-misplaced"}


def test_tooling_is_still_checked_for_unresolvable_imports_and_forbidden_names(repo: Path) -> None:
    tool_repo(repo, "from ... import nowhere\n")

    found = findings(repo, dev_tools=["devtools"], forbidden_modules=["devtools.scan"])

    assert ("bad-import", "devtools.scan") in found
    assert ("forbidden-module", "devtools.scan") in found


def test_tooling_import_cycles_are_still_reported(repo: Path) -> None:
    write_files(repo, {"devtools/__init__.py": "", "devtools/a.py": "import devtools.b\n", "devtools/b.py": "import devtools.a\n"})

    assert any(rule == "circular-import" for rule, _ in findings(repo, dev_tools=["devtools"]))


def test_a_ring_annotation_on_tooling_is_not_a_mismatch(repo: Path) -> None:
    tool_repo(repo, '__arbor__ = {"ring": "domain", "role": "dev_tool"}\n')

    assert not {f for f in findings(repo, dev_tools=["devtools"]) if f[0] == "arbor-ring-mismatch"}


def test_raw_process_use_in_tooling_is_reported_by_default(repo: Path) -> None:
    tool_repo(repo, "import subprocess\nsubprocess.run(['git', 'status'])\n")

    assert ("effect-process", "devtools.scan") in findings(repo, dev_tools=["devtools"])


def test_dev_tools_forbid_can_allow_or_widen_what_tooling_may_use(repo: Path) -> None:
    tool_repo(repo, "import subprocess, time\nsubprocess.run(['x'])\ntime.time()\n")

    allowed = findings(repo, dev_tools=["devtools"], dev_tools_forbid=[])
    widened = findings(repo, dev_tools=["devtools"], dev_tools_forbid=["clock"])

    assert not {f for f in allowed if f[0].startswith("effect-")}
    assert {f for f in widened if f[0].startswith("effect-")} == {("effect-clock", "devtools.scan")}


def test_dev_tools_forbid_rejects_unknown_categories() -> None:
    with pytest.raises(PolicyError, match="dev_tools_forbid"):
        mini_policy(dev_tools=["devtools"], dev_tools_forbid=["nope"])


def test_dev_tools_may_import_limits_which_product_modules_tooling_reaches(repo: Path) -> None:
    write_files(repo, {"app/domain.py": "", "app/ports.py": "", "devtools/__init__.py": "", "devtools/scan.py": "from app import domain\nfrom app import ports\n"})

    found = rows(repo, dev_tools=["devtools"], dev_tools_may_import=["app.domain"])

    assert [r for r in found if r[0] == "dev-tool-product-import"] == [("dev-tool-product-import", "devtools.scan", 2)]
    assert not [r for r in rows(repo, dev_tools=["devtools"]) if r[0] == "dev-tool-product-import"]


def test_dev_tools_forbid_uses_the_tool_modules_own_scope_effect_defaults(tmp_path: Path) -> None:
    """A dev tool's `dev_tools_forbid` check must read the scope that owns it, not whichever scope happens to be
    first in the (scoped) root policy: `beta` is first here and stays "injection", so if the check read the root
    policy instead of `alpha`'s own "count", the seam below would never count as a use."""
    write_files(tmp_path, {
        "libs/beta/src/beta/api.py": "VALUE = 1\n",
        "libs/alpha/src/alpha/core.py": "VALUE = 1\n",
        "libs/alpha/src/devtools/scan.py": (
            "import time\nfrom typing import Callable\n"
            "def now(clock: Callable[[], float] = time.time) -> float:\n    return clock()\n"
        ),
    })
    raw = {
        "dev_tools": ["libs/alpha/src/devtools"],
        "dev_tools_forbid": ["clock"],
        "scopes": {
            "beta": {
                "sources": ["libs/beta/src/beta"], "source_roots": ["libs/beta/src"],
                "rings": ["domain"], "classify": {"beta.api": "domain"},
            },
            "alpha": {
                "sources": ["libs/alpha/src/alpha", "libs/alpha/src/devtools"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain"], "classify": {"alpha.core": "domain"},
                "effect_defaults": "count",
            },
        },
    }
    policy = policy_from_mapping(raw)
    assert policy.effect_defaults == "injection"  # the root policy mirrors beta's (first scope's) default, not alpha's

    found, _ = run_own(policy, tmp_path, make_services(GitFiles(available=False)), practices=False)

    assert ("effect-clock", "devtools.scan") in {(v.rule, v.subject) for v in found}
