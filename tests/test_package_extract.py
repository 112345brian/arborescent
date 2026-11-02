"""``arborescent packages extract`` is a wrapper: a migration library extracts, Arborescent writes the manifest.

Copying files, history and import rewriting are other tools' jobs (git filter-repo, LibCST). What stays Arborescent's is the
``.arborescent-package.toml`` that records what was extracted, because the preservation checks and
``packages validate --extracted`` read it. So ``extract`` is the same pipeline as ``migrate`` plus that manifest.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from arborescent_helpers import services
from test_migration_export import CLI_CATALOG, _repo

from arborescent.adapters import LocalFileSystem
from arborescent.cli import main
from arborescent.driver_request import MigrationOutcome
from arborescent.package_manifest import load_extracted_package
from arborescent.packages import PackageCatalog
from arborescent.testing import FakeMigrationLibrary, FakeToolRunner

FS = LocalFileSystem()
MANIFEST = ".arborescent-package.toml"


def run(tmp_path: Path, action: str, *argv: str, library: FakeMigrationLibrary | None = None, catalog: str = CLI_CATALOG) -> int:
    _repo(tmp_path, catalog)
    for flag in ("--output", "--target"):
        if flag in argv and "--yes" in argv:  # the fake library migrates nothing, so the tree the manifest is checked against is made here
            (Path(argv[argv.index(flag) + 1]) / "app").mkdir(parents=True, exist_ok=True)
    base = ["--config", str(tmp_path / "arborescent.toml"), "packages", action, "core", *argv]
    return main(base, services(FakeToolRunner()), library=library or FakeMigrationLibrary(), environ={})


def test_extract_is_the_migrate_pipeline_with_the_same_supply_request(tmp_path: Path) -> None:
    via_migrate, via_extract = FakeMigrationLibrary(), FakeMigrationLibrary()
    assert run(tmp_path / "a", "migrate", "--target", str(tmp_path / "a" / "out"), "--yes", library=via_migrate) == 0
    assert run(tmp_path / "b", "extract", "--target", str(tmp_path / "b" / "out"), "--yes", library=via_extract) == 0
    assert len(via_migrate.supplied) == len(via_extract.supplied) == 1
    one, two = via_migrate.supplied[0], via_extract.supplied[0]
    assert (one.package, one.branch, one.path_renames) == (two.package, two.branch, two.path_renames)


def test_output_is_another_name_for_target(tmp_path: Path) -> None:
    library = FakeMigrationLibrary()
    assert run(tmp_path, "extract", "--output", str(tmp_path / "out"), "--yes", library=library) == 0
    assert library.supplied[0].target == tmp_path / "out"


def test_a_dry_run_supplies_nothing_and_writes_no_manifest(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    library = FakeMigrationLibrary()
    assert run(tmp_path, "extract", "--output", str(tmp_path / "out"), library=library) == 0
    assert library.supplied == [] and not (tmp_path / "out").exists()
    assert "dry run" in capsys.readouterr().out


def test_after_the_library_ran_a_valid_manifest_records_what_was_extracted(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run(tmp_path, "extract", "--output", str(out), "--yes", library=FakeMigrationLibrary(outcome=MigrationOutcome(True, "ok"))) == 0
    extracted = load_extracted_package(FS, out)
    assert extracted.semantics.id == "core"
    assert extracted.origin_repository == "example/mono"
    assert extracted.current_repository == "example/core"  # where the package now lives: its home repository
    assert [(p.origin_path, p.current_path) for p in extracted.paths] == [("app", "app")]


def test_the_repository_flag_overrides_where_the_manifest_says_it_lives(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert run(tmp_path, "extract", "--output", str(out), "--repository", "org/elsewhere", "--yes") == 0
    assert load_extracted_package(FS, out).current_repository == "org/elsewhere"


def test_a_library_failure_writes_no_manifest(tmp_path: Path) -> None:
    out = tmp_path / "out"
    library = FakeMigrationLibrary(outcome=MigrationOutcome(False, "it broke"))
    assert run(tmp_path, "extract", "--output", str(out), "--yes", library=library) == 1
    assert not (out / MANIFEST).exists()


def test_with_dependencies_is_refused_with_the_reason(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as stop:
        run(tmp_path, "extract", "--output", str(tmp_path / "out"), "--with-dependencies")
    assert stop.value.code == 2
    assert "separate package" in capsys.readouterr().err


def test_arborescent_no_longer_copies_files_itself() -> None:
    assert not hasattr(PackageCatalog, "extract")
    assert importlib.util.find_spec("arborescent.package_copy") is None
