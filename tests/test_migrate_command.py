"""#67: ``arborescent packages migrate``: an explicit command, a dry run by default, never a side effect of other commands."""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest
from arborescent_helpers import services, write_files
from test_migration_export import CLI_CATALOG, _repo

from arborescent.cli import main
from arborescent.driver_request import MigrationOutcome
from arborescent.testing import FakeMigrationLibrary, FakeToolRunner


def run(tmp_path: Path, library: FakeMigrationLibrary, *extra: str, catalog: str = CLI_CATALOG) -> int:
    _repo(tmp_path, catalog)
    argv = ["--config", str(tmp_path / "arborescent.toml"), "packages", "migrate", "core", "--target", str(tmp_path / "out"), *extra]
    return main(argv, services(FakeToolRunner()), library=library)


def test_the_default_is_a_dry_run_that_prints_the_exact_arguments_and_supplies_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = FakeMigrationLibrary()
    assert run(tmp_path, library) == 0
    out = capsys.readouterr().out
    assert library.supplied == []
    assert "dry run" in out
    assert shlex.join(["fake-migrate", "core", str(tmp_path / "out")]) in out  # the library's own rendering of the exact arguments
    assert "app -> ." in out  # the path rename that would be supplied
    assert "--yes" in out


def test_yes_supplies_the_library_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = FakeMigrationLibrary(outcome=MigrationOutcome(True, "all good"))
    assert run(tmp_path, library, "--yes") == 0
    assert len(library.supplied) == 1
    request = library.supplied[0]
    assert (request.package, request.target, request.branch) == ("core", tmp_path / "out", "main")
    assert "all good" in capsys.readouterr().out


def test_a_library_failure_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = FakeMigrationLibrary(outcome=MigrationOutcome(False, "it broke"))
    assert run(tmp_path, library, "--yes") == 1
    assert "it broke" in capsys.readouterr().out


def test_arguments_the_library_cannot_apply_are_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, FakeMigrationLibrary(unapplied=("module renames (not a filter-repo job)",))) == 0
    assert "NOT APPLIED  module renames (not a filter-repo job)" in capsys.readouterr().out


def test_open_gaps_refuse_even_with_yes_and_are_listed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = FakeMigrationLibrary()
    no_home = re.sub(r"\[packages\.home\]\n(?:[a-z_]+ = [^\n]*\n)+", "", CLI_CATALOG)  # whatever the home's keys are
    assert "packages.home" not in no_home
    assert run(tmp_path, library, "--yes", catalog=no_home) == 1
    assert library.supplied == []
    assert "package-undeclared-home" in capsys.readouterr().out


def test_a_missing_library_prints_a_setup_line_with_the_install_command(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = FakeMigrationLibrary(installed=False)
    assert run(tmp_path, library, "--yes") == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("SETUP")]
    assert len(lines) == 1
    assert "fake-migrate" in lines[0]
    assert "pip install fake-library>=1" in lines[0] or "fake-library>=1" in lines[0]
    assert library.supplied == []


def test_a_dry_run_with_a_missing_library_still_shows_the_arguments_and_the_setup_line(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run(tmp_path, FakeMigrationLibrary(installed=False)) == 0
    out = capsys.readouterr().out
    assert "SETUP" in out
    assert "fake-migrate core" in out


def test_an_unknown_package_exits_two(tmp_path: Path) -> None:
    _repo(tmp_path, CLI_CATALOG)
    argv = ["--config", str(tmp_path / "arborescent.toml"), "packages", "migrate", "nope", "--target", str(tmp_path / "out")]
    assert main(argv, services(FakeToolRunner()), library=FakeMigrationLibrary()) == 2


def test_check_and_generate_never_call_the_library(tmp_path: Path) -> None:
    library = FakeMigrationLibrary()
    _repo(tmp_path, CLI_CATALOG)
    write_files(tmp_path, {})
    config = str(tmp_path / "arborescent.toml")
    _ = main(["--config", config, "check", "--no-tools"], services(FakeToolRunner()), library=library)
    _ = main(["--config", config, "generate"], services(FakeToolRunner()), library=library)
    assert library.supplied == []


# -- a package whose private modules other packages still import ----------------------------------------------------

TWO_PACKAGES = """
[[packages]]
id = "core"
name = "Core"
kind = "library"
repository = "example/mono"
root = "src/app/core"
summary = "domain"
public = ["app.core.api"]
externals = []
[packages.home]
kind = "repository"
repository = "https://github.com/example/core.git"
[[packages.paths]]
path = "src/app/core"
role = "source"

[[packages]]
id = "web"
name = "Web"
kind = "application"
repository = "example/mono"
root = "src/app/web"
summary = "http"
depends_on = ["core"]
externals = []
[packages.home]
kind = "workspace-member"
path = "packages/web"
[[packages.paths]]
path = "src/app/web"
role = "source"
"""


def two_package_repo(root: Path, web_imports: str) -> None:
    write_files(root, {
        "src/app/core/__init__.py": "", "src/app/core/api.py": "VALUE = 1\n", "src/app/core/internal.py": "HIDDEN = 2\n",
        "src/app/web/__init__.py": "", "src/app/web/views.py": f"{web_imports}\n",
        "packages.toml": TWO_PACKAGES,
        "arborescent.toml": 'package_catalog = "packages.toml"\nsources = ["src/app"]\nsource_roots = ["src"]\nrings = ["domain"]\npreset = "strict"\n'
                            '[ring.domain]\nexternal = ["*"]\n[classify]\n"app*" = "domain"\n',
    })


def migrate_core(root: Path, library: FakeMigrationLibrary) -> int:
    argv = ["--config", str(root / "arborescent.toml"), "packages", "migrate", "core", "--target", str(root / "out"), "--yes"]
    return main(argv, services(FakeToolRunner()), library=library)


def test_a_private_import_by_another_package_refuses_the_split_and_lists_the_edge(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    two_package_repo(tmp_path, "from app.core.internal import HIDDEN")
    library = FakeMigrationLibrary()
    assert migrate_core(tmp_path, library) == 1
    out = capsys.readouterr().out
    assert library.supplied == []
    assert "[private-import] app.web.views (package web) imports app.core.internal" in out


def test_the_same_repository_proceeds_when_only_the_public_surface_is_imported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    two_package_repo(tmp_path, "from app.core.api import VALUE")
    library = FakeMigrationLibrary()
    assert migrate_core(tmp_path, library) == 0, capsys.readouterr().out
    assert len(library.supplied) == 1  # so the refusal above is caused by the private import and nothing else
