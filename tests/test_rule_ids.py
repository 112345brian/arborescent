"""Rule ids: one vocabulary, stable strings (baseline files store them), every rule tested or explained."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import CLASSIFY, mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.config import load_policy
from arborescent.domain import Rule, Violation
from arborescent.engine import Services, write_generated
from arborescent.rules import check_modules
from arborescent.selftest import NO_MUTATION, plan
from arborescent.testing import FakeToolRunner

ROOT = Path(__file__).resolve().parents[1]
ENTRY: dict[str, Any] = {"sources": ["app", "run.py"], "entry_scripts": ["run"], "classify": {**CLASSIFY, "run": "composition"}}


def test_rule_id_strings_never_change() -> None:
    """Baseline keys are ``rule|subject|detail`` in users' files: renaming an id orphans every recorded entry."""
    assert sorted(Rule) == sorted(
        [
            "unclassified",
            "noise-dir-code",
            "vendor-folder",
            "wildcard-ring",
            "suppression",
            "discard",
            "effect-clock",
            "effect-env",
            "effect-identity",
            "effect-randomness",
            "effect-sleep",
            "effect-threads",
            "effect-process",
            "effect-dynamic-import",
            "effect-patch",
            "internal-import",
            "circular-import",
            "external-import",
            "os-branching",
            "bad-import",
            "undeclared-entry-dependency",
            "gitignore-shadow",
            "sibling-versions",
            "secrets-guard",
            "docs-sync",
            "crlf",
            "undeclared-imports",
            "invalid-arbor-annotation",
            "scope-import",
            "scope-effect-leak",
        ]
    )


def test_a_rule_id_is_a_plain_string_in_keys_and_output() -> None:
    v = Violation(Rule.INTERNAL_IMPORT, "app.domain", "detail", 3)
    assert v.key == "internal-import|app.domain|detail"
    assert v.render() == "app.domain:3: [internal-import] detail"
    assert Rule("crlf") is Rule.CRLF


def test_every_rule_has_a_selftest_mutation_or_a_documented_reason() -> None:
    policy, root = load_policy(LocalFileSystem(), ROOT / "pyproject.toml")
    scan = FsScanner().scan(policy, root)
    mutated = {m.rule for m in plan(policy, scan)[0]}
    # the arborescent repo has no entry scripts; that rule's mutation is covered by the next test
    assert set(NO_MUTATION) | mutated | {Rule.UNDECLARED_ENTRY_DEPENDENCY} >= set(Rule)
    assert not set(NO_MUTATION) & mutated, "a rule with a mutation needs no excuse"
    assert all(reason.strip() for reason in NO_MUTATION.values())


def test_entry_dependency_rule_has_a_mutation(repo: Path) -> None:
    write_files(repo, {"run.py": "# /// script\n# dependencies = []\n# ///\n"})
    policy = mini_policy(**ENTRY)
    scan = FsScanner().scan(policy, repo)
    assert Rule.UNDECLARED_ENTRY_DEPENDENCY in {m.rule for m in plan(policy, scan)[0]}


def test_circular_import_rule_has_a_selftest_mutation(repo: Path) -> None:
    policy = mini_policy()
    scan = FsScanner().scan(policy, repo)
    assert Rule.CIRCULAR_IMPORT in {m.rule for m in plan(policy, scan)[0]}


def _table_ids(text: str, heading: str) -> set[str]:
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    return set(re.findall(r"^\| `([a-z-]+)` \|", section, re.MULTILINE))


def test_configuration_doc_lists_exactly_the_rules_that_exist() -> None:
    text = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    documented = _table_ids(text, "## Rules Arborescent checks itself") | _table_ids(text, "## Practices:")
    assert documented == {r.value for r in Rule}


def test_check_modules_orders_violations_by_module_then_import_then_os_then_entry(repo: Path) -> None:
    write_files(
        repo,
        {
            "run.py": "# /// script\n# dependencies = []\n# ///\n",
            "app/ports.py": "import requests\nfrom . import adapters\nimport os\n\nX = os.name\n",
            "app/adapters.py": "from ... import nothing\nimport psutil\nimport sys\nfrom . import main\n",
            "app/zzz.py": "",
        },
    )
    policy = mini_policy(**ENTRY)
    scan = FsScanner().scan(policy, repo)
    found = check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)
    assert [(v.rule, v.subject) for v in found] == [
        ("bad-import", "app.adapters"),
        ("external-import", "app.adapters"),
        ("internal-import", "app.adapters"),
        ("external-import", "app.ports"),
        ("internal-import", "app.ports"),
        ("external-import", "app.ports"),
        ("os-branching", "app.ports"),
        ("unclassified", "app.zzz"),
        ("circular-import", "app.adapters, app.main"),
        ("undeclared-entry-dependency", "run"),
        ("undeclared-entry-dependency", "run"),
    ]
    assert [v.detail for v in found[-2:]] == [
        "does not declare 'psutil' (imported by project code)",
        "does not declare 'requests' (imported by project code)",
    ]


def test_check_modules_edge_inputs(repo: Path) -> None:
    policy = mini_policy(**ENTRY)
    assert check_modules(policy, {}, [], {}, {}) == []  # nothing scanned
    assert [v.subject for v in check_modules(policy, {}, ["run"], {}, {})] == ["run"]  # entry script without a block
    assert check_modules(policy, {}, [], {"run": ()}, {}) == []  # declared, not scanned, nothing imported


def test_write_generated_writes_every_file_and_reports_them(repo: Path) -> None:
    policy = mini_policy()
    services = Services(FsScanner(), FakeToolRunner(), [], LocalFileSystem(), SystemClock(), [])
    written = write_generated(policy, repo, services)
    assert written == sorted(written) and written
    for rel in written:
        assert b"\r\n" not in (repo / rel).read_bytes()
    (repo / written[0]).write_text("stale", encoding="utf-8")
    assert write_generated(policy, repo, services) == []  # existing presets are left untouched
    assert (repo / written[0]).read_text(encoding="utf-8") == "stale"


@pytest.mark.parametrize("ring_change", [{}, {"domain": {"external": ["sys"]}}])
def test_write_generated_makes_missing_folders(repo: Path, ring_change: dict[str, Any]) -> None:
    policy = mini_policy(ring=ring_with(**ring_change))
    written = write_generated(policy, repo, Services(FsScanner(), FakeToolRunner(), [], LocalFileSystem(), SystemClock(), []))
    assert all((repo / rel).is_file() for rel in written)
