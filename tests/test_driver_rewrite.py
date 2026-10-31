"""#67: the import-rewriting step of the driver. A second port (``ImportRewriter``), separate from the migration library,
applies the package's module-name map to the NEW repository after the library ran, never to the monorepo.

All tests here use the in-memory fakes through the ports.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from arborescent_helpers import services
from test_driver_plan import CLEAN, SITE, build
from test_migration_arguments import CATALOG
from test_migration_export import CLI_CATALOG, _repo

from arborescent.cli import main
from arborescent.driver_plan import READY, plan_supply
from arborescent.driver_request import MigrationOutcome, RewriteFinding
from arborescent.testing import FakeImportRewriter, FakeMigrationLibrary, FakeToolRunner

RENAMED = CLI_CATALOG.replace("[[packages.paths]]", '[[packages.module_renames]]\nfrom = "app"\nto = "core"\n[[packages.paths]]', 1)


def run(tmp_path: Path, library: FakeMigrationLibrary, rewriter: FakeImportRewriter, *extra: str, catalog: str = RENAMED) -> int:
    _repo(tmp_path, catalog)
    argv = ["--config", str(tmp_path / "arborescent.toml"), "packages", "migrate", "core", "--target", str(tmp_path / "out"), *extra]
    return main(argv, services(FakeToolRunner()), library=library, rewriter=rewriter)


# -- the plan carries the per-module map ------------------------------------------------------------------------------

def test_the_plan_carries_one_rename_per_module_whose_name_changes_longest_first() -> None:
    plan = plan_supply(build(), "core", FakeMigrationLibrary(), SITE)
    assert plan.status == READY
    assert plan.rewrite is not None
    assert [(r.source, r.target) for r in plan.rewrite.renames] == [
        ("app.core.internal", "core.internal"), ("app.core.api", "core.api"),
    ]
    assert plan.rewrite.source_paths == tuple(r.source for r in plan.request.path_renames)  # type: ignore[union-attr]


def test_a_package_whose_module_names_do_not_change_has_no_rewrite_step() -> None:
    assert plan_supply(build(CATALOG.replace('[[packages.module_renames]]\nfrom = "app.core"\nto = "core"\n', "")), "core", FakeMigrationLibrary(), SITE).rewrite is None


# -- the command ---------------------------------------------------------------------------------------------------------

def test_the_dry_run_prints_every_rename_and_the_exact_commands_and_applies_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library, rewriter = FakeMigrationLibrary(), FakeImportRewriter()
    assert run(tmp_path, library, rewriter) == 0
    out = capsys.readouterr().out
    assert library.supplied == [] and rewriter.applied == []
    assert "import rewrite (on the new repository only, after the library ran):" in out
    assert "app.domain -> core.domain" in out and "app -> core" in out
    assert "fake-rewrite app.domain core.domain" in out  # the rewriter's own rendering of each command
    assert "NOT APPLIED  module renames" not in out  # the rewriter takes them over


def test_yes_runs_the_library_first_and_then_rewrites_only_the_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log: list[str] = []
    library, rewriter = FakeMigrationLibrary(log=log), FakeImportRewriter(log=log)
    assert run(tmp_path, library, rewriter, "--yes") == 0
    assert log == ["supply", "rewrite"]
    request = rewriter.applied[0]
    assert request.root == tmp_path / "out"  # the new repository, never the monorepo
    assert request.root != tmp_path
    assert "uncommitted" in capsys.readouterr().out


def test_a_library_failure_means_no_rewrite(tmp_path: Path) -> None:
    library, rewriter = FakeMigrationLibrary(outcome=MigrationOutcome(False, "boom")), FakeImportRewriter()
    assert run(tmp_path, library, rewriter, "--yes") == 1
    assert rewriter.applied == []


def test_a_rewrite_failure_exits_one_and_says_what_state_the_target_is_in(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rewriter = FakeImportRewriter(outcome=MigrationOutcome(False, "rename failed"))
    assert run(tmp_path, FakeMigrationLibrary(), rewriter, "--yes") == 1
    out = capsys.readouterr().out
    assert "rename failed" in out and "history was written" in out


def test_unsupported_import_forms_refuse_before_anything_is_created(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    finding = RewriteFinding("app/main.py", 3, "from app import domain")
    library, rewriter = FakeMigrationLibrary(), FakeImportRewriter(findings=(finding,))
    assert run(tmp_path, library, rewriter, "--yes") == 1
    out = capsys.readouterr().out
    assert "[unsupported-import-form] app/main.py:3: from app import domain" in out
    assert library.supplied == [] and rewriter.applied == []
    assert not (tmp_path / "out").exists()


def test_the_dry_run_reports_the_same_refusal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    rewriter = FakeImportRewriter(findings=(RewriteFinding("app/main.py", 3, "from app import domain"),))
    assert run(tmp_path, FakeMigrationLibrary(), rewriter) == 1
    assert "[unsupported-import-form]" in capsys.readouterr().out


def test_a_missing_rewriter_stops_before_the_library_runs_and_prints_a_setup_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library, rewriter = FakeMigrationLibrary(), FakeImportRewriter(installed=False)
    assert run(tmp_path, library, rewriter, "--yes") == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("SETUP")]
    assert len(lines) == 1 and "fake-rewrite" in lines[0] and "fake-rewriter>=1" in lines[0]
    assert library.supplied == [] and rewriter.applied == []


def test_a_dry_run_with_a_missing_rewriter_still_shows_the_plan_and_the_setup_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, FakeMigrationLibrary(), FakeImportRewriter(installed=False)) == 0
    out = capsys.readouterr().out
    assert "SETUP" in out and "app.domain -> core.domain" in out


def test_without_module_renames_the_rewriter_is_never_consulted(tmp_path: Path) -> None:
    rewriter = FakeImportRewriter(installed=False, findings=(RewriteFinding("x.py", 1, "x"),))
    assert run(tmp_path, FakeMigrationLibrary(), rewriter, "--yes", catalog=CLI_CATALOG) == 0
    assert rewriter.applied == []


def test_the_ready_check_still_comes_first(tmp_path: Path) -> None:
    no_home = re.sub(r"\[packages\.home\]\n(?:[a-z_]+ = [^\n]*\n)+", "", RENAMED)
    rewriter = FakeImportRewriter()
    assert run(tmp_path, FakeMigrationLibrary(), rewriter, "--yes", catalog=no_home) == 1
    assert rewriter.applied == []
    assert CLEAN  # keeps the shared fixture import honest
