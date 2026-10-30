"""Mutation probes select hosts and targets using effective annotated rings."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.config import policy_from_mapping
from arborescent.domain import Rule
from arborescent.engine import Services
from arborescent.selftest import plan, selftest
from arborescent.testing import FakeToolRunner


@pytest.mark.parametrize("annotated", [False, True])
def test_probe_hosts_and_forbidden_targets_follow_effective_rings(tmp_path: Path, annotated: bool) -> None:
    # Annotations reverse the classify assignments; both rings must still get probes.
    write_files(tmp_path, {
        "app/first.py": '__arbor__ = {"ring": "ports"}\n' if annotated else "",
        "app/second.py": '__arbor__ = {"ring": "domain"}\n' if annotated else "",
    })
    policy = mini_policy(classify={"app.first": "domain", "app.second": "ports"})
    scan = FsScanner().scan(policy, tmp_path)
    mutations, _ = plan(policy, scan)
    domain_file = "app/second.py" if annotated else "app/first.py"
    ports_file = "app/first.py" if annotated else "app/second.py"
    forbidden_target = "app.first" if annotated else "app.second"
    assert [(m.file, m.append) for m in mutations if m.rule == Rule.INTERNAL_IMPORT] == [
        (domain_file, f"import {forbidden_target}"),
        (domain_file, f"def _arborescent_probe() -> None:\n    import {forbidden_target}"),
    ]
    assert [m.file for m in mutations if m.rule == Rule.EXTERNAL_IMPORT] == [
        domain_file, domain_file, ports_file, ports_file,
    ]


def test_entry_probe_host_can_be_classified_only_by_annotation(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "app/first.py": "",
        "app/second.py": '__arbor__ = {"ring": "domain"}\n',
        "run.py": "# /// script\n# dependencies = []\n# ///\n",
    })
    policy = mini_policy(sources=["app", "run.py"], entry_scripts=["run"], classify={"run": "composition"})
    scan = FsScanner().scan(policy, tmp_path)
    mutations, _ = plan(policy, scan)
    assert [m.file for m in mutations if m.rule == Rule.UNDECLARED_ENTRY_DEPENDENCY] == ["app/second.py"]


def test_selftest_uses_full_scan_annotations_for_mutation_planning(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "app/first.py": '__arbor__ = {"ring": "ports"}\n',
        "app/second.py": '__arbor__ = {"ring": "domain"}\n',
    })
    policy = mini_policy(classify={"app.first": "domain", "app.second": "ports"})
    services = Services(FsScanner(tmp_path / "cache.json"), FakeToolRunner(), [], LocalFileSystem(), SystemClock(), [])
    report = selftest(policy, tmp_path, services)
    assert report.survived == []
    assert any("domain: forbidden top-level internal import of app.first" in label for label in report.killed)


def test_selftest_covers_cross_scope_boundary(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/alpha/core.py": "VALUE = 1\n",
        "libs/beta/src/beta/api.py": "VALUE = 2\n",
    })
    policy = policy_from_mapping({
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain"], "classify": {"alpha.core": "domain"},
                "public": ["alpha.core"],
            },
            "beta": {
                "sources": ["libs/beta/src/beta"], "source_roots": ["libs/beta/src"],
                "rings": ["domain"], "classify": {"beta.api": "domain"},
                "public": ["beta.api"],
            },
        },
    })
    services = Services(FsScanner(tmp_path / "cache.json"), FakeToolRunner(), [], LocalFileSystem(), SystemClock(), [])

    report = selftest(policy, tmp_path, services)

    assert report.survived == []
    assert any(line.startswith("[scope-import]") for line in report.killed)


def test_selftest_covers_scope_effect_leak(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/alpha/core.py": "VALUE = 1\n",
        "libs/beta/src/beta/api.py": "import time\nVALUE = time.time()\n",
    })
    policy = policy_from_mapping({
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain", "adapters"], "classify": {"alpha.core": "domain"},
                "depends_on": ["beta"],
            },
            "beta": {
                "sources": ["libs/beta/src/beta"], "source_roots": ["libs/beta/src"],
                "rings": ["domain", "adapters"], "classify": {"beta.api": "adapters"},
                "public": ["beta.api"],
            },
        },
    })
    services = Services(FsScanner(tmp_path / "cache.json"), FakeToolRunner(), [], LocalFileSystem(), SystemClock(), [])

    report = selftest(policy, tmp_path, services)

    assert report.survived == []
    assert any(line.startswith("[scope-effect-leak]") for line in report.killed)


def test_circular_import_probe_uses_a_scope_that_enforces_cycles(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/alpha/a.py": "VALUE = 1\n",
        "libs/beta/src/beta/b.py": "VALUE = 2\n",
    })
    policy = policy_from_mapping({
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain"], "classify": {"alpha.a": "domain"},
                "forbid_circular_imports": False,
            },
            "beta": {
                "sources": ["libs/beta/src/beta"], "source_roots": ["libs/beta/src"],
                "rings": ["domain"], "classify": {"beta.b": "domain"},
                "forbid_circular_imports": True,
            },
        },
    })
    scan = FsScanner().scan(policy, tmp_path)

    mutations, _ = plan(policy, scan)

    circular = [mutation for mutation in mutations if mutation.rule == Rule.CIRCULAR_IMPORT]
    assert len(circular) == 1
    assert circular[0].file == "libs/beta/src/beta/b.py"
