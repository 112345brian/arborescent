"""Machine-specific migration inputs: where a package's new repository is checked out locally.

``home.repository`` is the committed remote identity of a repository. Where it is checked out on this machine is not a
fact about the code, so it lives in a git-ignored local config, found through an environment variable or a ``.env``,
and only ``packages migrate`` reads it.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from arborescent_helpers import services, write_files
from test_migration_export import CLI_CATALOG, _repo

from arborescent.cli import main
from arborescent.driver_request import MigrationOutcome
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.local_targets import (
    CONFIG_VARIABLE,
    ConfigLocation,
    LocalTargetsError,
    choose_target,
    load_local_targets,
    locate_config,
    read_dotenv_value,
)
from arborescent.testing import FakeMigrationLibrary, FakeToolRunner

FS = LocalFileSystem()
SRC = Path(__file__).resolve().parents[1] / "src" / "arborescent"


# -- .env: one variable, nothing else is read or kept ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{CONFIG_VARIABLE}=cfg/local.toml\n", "cfg/local.toml"),
        (f'{CONFIG_VARIABLE}="cfg/local.toml"\n', "cfg/local.toml"),
        (f"{CONFIG_VARIABLE}='cfg/local.toml'\n", "cfg/local.toml"),
        (f"export {CONFIG_VARIABLE}=cfg/local.toml\n", "cfg/local.toml"),
        (f"# a comment\n\n  {CONFIG_VARIABLE} = cfg/local.toml  # trailing\n", "cfg/local.toml"),
        (f"{CONFIG_VARIABLE}=first\n{CONFIG_VARIABLE}=second\n", "second"),  # the last assignment wins, as in a shell
        (f"OTHER_{CONFIG_VARIABLE}=nope\n", None),
        ("TOKEN=abc\n", None),
        ("", None),
        (f"{CONFIG_VARIABLE}=\n", None),
    ],
)
def test_dotenv_reading_takes_only_the_one_variable(text: str, expected: str | None) -> None:
    assert read_dotenv_value(text, CONFIG_VARIABLE) == expected


# -- where the config is -------------------------------------------------------------------------------------------


def test_the_environment_variable_beats_dotenv_beats_the_default(tmp_path: Path) -> None:
    write_files(tmp_path, {".env": f"{CONFIG_VARIABLE}=from-dotenv.toml\n"})
    env = locate_config(tmp_path, {CONFIG_VARIABLE: "from-env.toml"}, FS)
    assert (env.path, env.explicit) == (tmp_path / "from-env.toml", True)
    assert "environment variable" in env.via
    dotenv = locate_config(tmp_path, {}, FS)
    assert (dotenv.path, dotenv.explicit) == (tmp_path / "from-dotenv.toml", True)
    assert ".env" in dotenv.via
    (tmp_path / ".env").unlink()
    default = locate_config(tmp_path, {}, FS)
    assert (default.path, default.explicit) == (tmp_path / ".arborescent" / "local.toml", False)


def test_an_absolute_path_outside_the_worktree_is_kept_as_given(tmp_path: Path) -> None:
    outside = tmp_path / "outside" / "targets.toml"
    location = locate_config(tmp_path / "repo", {CONFIG_VARIABLE: str(outside)}, FS)
    assert location.path == outside  # survives branch switches, clean checkouts and linked worktrees


def test_a_blank_environment_variable_falls_through(tmp_path: Path) -> None:
    assert not locate_config(tmp_path, {CONFIG_VARIABLE: "   "}, FS).explicit


# -- the file -------------------------------------------------------------------------------------------------------


def write_config(tmp_path: Path, text: str, name: str = "local.toml") -> ConfigLocation:
    write_files(tmp_path, {name: text})
    return ConfigLocation(tmp_path / name, "test", True)


def test_relative_directories_resolve_against_the_config_files_directory(tmp_path: Path) -> None:
    absolute = tmp_path / "elsewhere" / "web"  # absolute on every platform: "/abs/web" has no drive, so it is relative on Windows
    location = write_config(tmp_path / "cfg", f'[targets]\ncore = "../checkouts/core"\nweb = {json.dumps(str(absolute))}\n')
    targets = load_local_targets(FS, location, ("core", "web"))
    assert targets.directories["core"] == tmp_path / "cfg" / ".." / "checkouts" / "core"
    assert targets.directories["web"] == absolute


def test_a_missing_default_file_is_simply_no_config(tmp_path: Path) -> None:
    targets = load_local_targets(FS, ConfigLocation(tmp_path / ".arborescent" / "local.toml", "default", False), ("core",))
    assert targets.directories == {}


def test_a_missing_explicit_file_is_an_error_naming_it_and_how_it_was_found(tmp_path: Path) -> None:
    with pytest.raises(LocalTargetsError, match=re.escape(str(tmp_path / "gone.toml"))) as info:
        load_local_targets(FS, ConfigLocation(tmp_path / "gone.toml", "the ARBORESCENT_LOCAL_CONFIG environment variable", True), ("core",))
    assert "ARBORESCENT_LOCAL_CONFIG" in str(info.value)


@pytest.mark.parametrize(
    ("text", "fragment"),
    [
        ("[targets\n", "not valid TOML"),
        ('targets = "x"\n', "`targets` must be a table"),
        ("[targets]\ncore = 5\n", "`core`"),
        ('[targets]\ncore = ""\n', "`core`"),
        ('[targets]\nghost = "x"\n', "unknown package 'ghost'"),
        ('[target]\ncore = "x"\n', "unknown key"),  # a typo must not make the file silently empty
    ],
)
def test_a_malformed_config_is_a_named_error_never_ignored(tmp_path: Path, text: str, fragment: str) -> None:
    location = write_config(tmp_path, text)
    with pytest.raises(LocalTargetsError, match=re.escape(fragment)) as info:
        load_local_targets(FS, location, ("core", "web"))
    assert str(location.path) in str(info.value)


def test_an_unknown_package_lists_the_known_ones(tmp_path: Path) -> None:
    with pytest.raises(LocalTargetsError, match="core, web"):
        load_local_targets(FS, write_config(tmp_path, '[targets]\nghost = "x"\n'), ("core", "web"))


# -- choosing -------------------------------------------------------------------------------------------------------


def test_the_flag_beats_the_config_and_says_so(tmp_path: Path) -> None:
    targets = load_local_targets(FS, write_config(tmp_path, '[targets]\ncore = "cfg-core"\n'), ("core",))
    chosen = choose_target("core", Path("flag-core"), targets)
    assert (chosen.path, chosen.source) == (Path("flag-core"), "--target")


def test_the_config_supplies_the_target_and_names_its_file_and_how_it_was_found(tmp_path: Path) -> None:
    targets = load_local_targets(FS, write_config(tmp_path, '[targets]\ncore = "cfg-core"\n'), ("core",))
    chosen = choose_target("core", None, targets)
    assert chosen is not None
    assert chosen.path == tmp_path / "cfg-core"
    assert str(tmp_path / "local.toml") in chosen.source and "test" in chosen.source


def test_nothing_configured_is_none_so_the_caller_can_explain_after_the_plan(tmp_path: Path) -> None:
    targets = load_local_targets(FS, write_config(tmp_path, '[targets]\nweb = "w"\n'), ("core", "web"))
    assert choose_target("core", None, targets) is None


# -- the command ---------------------------------------------------------------------------------------------------


def migrate(tmp_path: Path, *argv: str, environ: dict[str, str] | None = None, library: FakeMigrationLibrary | None = None) -> int:
    _repo(tmp_path, CLI_CATALOG)
    base = ["--config", str(tmp_path / "arborescent.toml"), "packages", "migrate", "core", *argv]
    return main(base, services(FakeToolRunner()), library=library or FakeMigrationLibrary(), environ=environ if environ is not None else {})


def test_the_driver_prints_that_the_flag_supplied_the_target(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert migrate(tmp_path, "--target", str(tmp_path / "out")) == 0
    assert "target from: --target" in capsys.readouterr().out


def test_the_driver_takes_the_target_from_the_local_config_and_prints_which_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {".arborescent/local.toml": '[targets]\ncore = "../elsewhere/core"\n'})
    library = FakeMigrationLibrary(outcome=MigrationOutcome(True, "done"))
    assert migrate(tmp_path, "--yes", library=library) == 0
    out = capsys.readouterr().out
    assert library.supplied[0].target == tmp_path / ".arborescent" / ".." / "elsewhere" / "core"
    assert f"target from: local config {tmp_path / '.arborescent' / 'local.toml'}" in out
    assert "default location" in out


def test_the_config_found_through_the_environment_variable_and_through_dotenv(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {"elsewhere/t.toml": '[targets]\ncore = "dest"\n', ".env": f"{CONFIG_VARIABLE}=elsewhere/t.toml\nTOKEN=hunter2\n"})
    assert migrate(tmp_path, environ={}) == 0
    out = capsys.readouterr().out
    assert ".env" in out and str(tmp_path / "elsewhere" / "t.toml") in out
    assert "hunter2" not in out  # nothing but the one variable is read, and nothing of the file is echoed
    assert migrate(tmp_path, environ={CONFIG_VARIABLE: str(tmp_path / "elsewhere" / "t.toml")}) == 0
    assert "environment variable" in capsys.readouterr().out


def test_without_any_target_it_exits_one_naming_both_ways_and_the_path_it_looked_at(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert migrate(tmp_path) == 1
    out = capsys.readouterr().out
    assert "--target" in out and "[targets]" in out and str(tmp_path / ".arborescent" / "local.toml") in out and CONFIG_VARIABLE in out


def test_the_plan_is_still_explained_when_no_target_was_given_but_the_package_is_blocked(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(tmp_path, CLI_CATALOG.replace('history = "preserve"', ""))  # a missing policy is an open error gap
    argv = ["--config", str(tmp_path / "arborescent.toml"), "packages", "migrate", "core"]
    assert main(argv, services(FakeToolRunner()), library=FakeMigrationLibrary(), environ={}) == 1
    out = capsys.readouterr().out
    assert "blocked" in out and "release-policy-missing" in out and "no target" not in out


def test_a_malformed_config_is_an_error_even_though_the_command_has_a_target_to_find(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_files(tmp_path, {".arborescent/local.toml": '[targets]\nghost = "x"\n'})
    assert migrate(tmp_path) == 2
    assert "unknown package 'ghost'" in capsys.readouterr().err


def test_a_malformed_config_is_not_read_when_the_flag_already_supplies_the_target(tmp_path: Path) -> None:
    write_files(tmp_path, {".arborescent/local.toml": "[targets\n"})
    assert migrate(tmp_path, "--target", str(tmp_path / "out")) == 0


# -- the machine-specific path stays out of everything that is committed ------------------------------------------


def run_text(tmp_path: Path, *argv: str, capsys: pytest.CaptureFixture[str]) -> str:
    assert main(["--config", str(tmp_path / "arborescent.toml"), *argv], services(FakeToolRunner()), environ={}) == 0
    return capsys.readouterr().out


def test_the_export_is_byte_identical_with_and_without_a_local_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(tmp_path, CLI_CATALOG)
    without = run_text(tmp_path, "packages", "export", capsys=capsys)
    write_files(tmp_path, {".arborescent/local.toml": '[targets]\ncore = "/machine/specific/core"\n', ".env": f"{CONFIG_VARIABLE}=.arborescent/local.toml\n"})
    with_config = run_text(tmp_path, "packages", "export", capsys=capsys)
    assert with_config == without
    assert "machine" not in with_config


def test_only_the_driver_command_and_its_own_module_may_use_the_local_config() -> None:
    allowed = {"local_targets.py", "cli_migrate.py", "cli.py"}
    readers = []
    for path in sorted(SRC.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 1 for a in n.names}
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module and n.level == 1}
        if "local_targets" in modules or {"CONFIG_VARIABLE", "locate_config", "load_local_targets"} & names:
            readers.append(path.name)
    assert set(readers) <= allowed, readers
    text = "".join((SRC / name).read_text(encoding="utf-8") for name in ("generate.py", "migration_export.py", "migration_json.py", "package_pyproject.py"))
    assert not [word for word in (CONFIG_VARIABLE, "local.toml", "local_targets") if word in text]


def test_the_repository_ignores_the_local_config_and_dotenv() -> None:
    lines = {line.strip() for line in (SRC.parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()}
    assert ".arborescent/" in lines and ".env" in lines
