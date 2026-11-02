"""#75: a shrink-only import cap needs its size readable off the check, in modules rather than baseline entries."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import write_files
from test_baseline_ratchet import write_policy

from arborescent import baseline
from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.cli import main
from arborescent.domain import Rule, Violation
from arborescent.engine import Services
from arborescent.engines import default_engines
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers


def internal(subject: str, target: str) -> Violation:
    return Violation(Rule.INTERNAL_IMPORT, subject, f"[x] imports {target}", 1, target)


def test_the_summary_counts_entries_importers_and_targets() -> None:
    baselined = [
        internal("tracker.a", "jobsearch"), internal("tracker.a", "jobsearch.desktop_open"),
        internal("tracker.b", "jobsearch.desktop_open"), internal("tracker.b", "jobsearch.desktop_open"),
    ]
    assert baseline.internal_import_summary(baselined) == (
        "baselined internal-import: 3 entries, 2 importing modules, 2 distinct targets"
    )


def test_one_entry_and_one_module_read_naturally() -> None:
    assert baseline.internal_import_summary([internal("a", "b")]) == (
        "baselined internal-import: 1 entry, 1 importing module, 1 distinct target"
    )


@pytest.mark.parametrize("violations", [[], [Violation("external-import", "a", "d", 1)], [Violation(Rule.INTERNAL_IMPORT, "a", "d")]])
def test_nothing_to_summarise_prints_no_line(violations: list[Violation]) -> None:
    assert baseline.internal_import_summary(violations) is None


def test_the_target_is_not_part_of_the_baseline_key_or_equality() -> None:
    assert internal("a", "b").key == Violation(Rule.INTERNAL_IMPORT, "a", "[x] imports b", 1).key
    assert internal("a", "b") == Violation(Rule.INTERNAL_IMPORT, "a", "[x] imports b", 1)


def _svc(fs: LocalFileSystem) -> Services:
    return Services(FsScanner(), FakeToolRunner(), list(all_checkers().values()), fs, SystemClock(), default_engines())


def test_check_prints_the_summary_for_baselined_import_violations_only(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_policy(repo)
    write_files(repo, {"app/domain.py": "from . import adapters, ports\n"})
    svc = _svc(LocalFileSystem())
    assert main(["--config", str(config), "baseline", "--allow-growth", "--reason", "cap"], svc) == 0
    _ = capsys.readouterr()

    assert main(["--config", str(config), "check", "--no-tools"], svc) == 0
    out = capsys.readouterr().out
    assert "baselined internal-import: 2 entries, 1 importing module, 2 distinct targets" in out

    assert main(["--config", str(config), "check", "--no-tools", "--no-baseline"], svc) == 1
    assert "baselined internal-import" not in capsys.readouterr().out


def test_the_baseline_command_prints_the_summary_too(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = write_policy(repo)
    write_files(repo, {"app/domain.py": "from . import adapters\n"})
    assert main(["--config", str(config), "baseline", "--allow-growth", "--reason", "cap"], _svc(LocalFileSystem())) == 0
    assert "baselined internal-import: 1 entry, 1 importing module, 1 distinct target" in capsys.readouterr().out
