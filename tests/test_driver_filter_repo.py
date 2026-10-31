"""#67: the git filter-repo adapter behind the migration-library port.

Unit tests drive it through the fake ``ToolRunner`` and ``FileSystem``; one test runs the real tool (skipped when
``git-filter-repo`` is not installed) to prove the argument shapes are the ones filter-repo accepts.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from arborescent.adapters import LocalFileSystem
from arborescent.domain import ToolResult
from arborescent.driver_filter_repo import EXECUTABLE, FilterRepo
from arborescent.driver_port import MigrationLibrary
from arborescent.driver_request import MigrationRequest
from arborescent.migration_arguments import PathRename
from arborescent.package_model import ModuleRename
from arborescent.testing import FakeFileSystem, FakeToolRunner
from arborescent.tool_runner import SubprocessToolRunner

SOURCE, TARGET = Path("/mono"), Path("/out/core")


def request(renames: tuple[PathRename, ...], modules: tuple[ModuleRename, ...] = ()) -> MigrationRequest:
    return MigrationRequest("core", SOURCE, TARGET, "main", renames, modules)


RENAMES = (PathRename("src/app/core/api.py", "api.py"), PathRename("src/app/core/schema", "data/schema"), PathRename("src/app/core", "."))


def adapter(runner: FakeToolRunner | None = None, fs: FakeFileSystem | None = None) -> FilterRepo:
    return FilterRepo(runner or FakeToolRunner(installed={EXECUTABLE, "git"}), fs or FakeFileSystem())


def test_it_satisfies_the_port() -> None:
    library: MigrationLibrary = adapter()
    assert library.executable == EXECUTABLE
    assert library.requirement.startswith("git-filter-repo")


def test_the_exact_arguments_select_each_owned_path_and_rename_it() -> None:
    assert adapter().arguments(request(RENAMES)) == (
        EXECUTABLE, "--source", "/mono", "--target", "/out/core", "--refs", "main",
        "--path", "src/app/core/api.py", "--path-rename", "src/app/core/api.py:api.py",
        "--path", "src/app/core/schema", "--path-rename", "src/app/core/schema:data/schema",
        "--path", "src/app/core", "--path-rename", "src/app/core/:",  # filter-repo spells "to the root" as an empty new name with a slash
    )


def test_module_renames_are_reported_as_not_applied_because_filter_repo_does_not_rewrite_imports() -> None:
    assert adapter().unsupported(request(RENAMES)) == ()
    assert adapter().unsupported(request(RENAMES, (ModuleRename("app.core", "core"),))) == ("module renames (git filter-repo does not rewrite imports)",)


def test_availability_follows_the_tool_runner() -> None:
    assert adapter().available() is True
    assert adapter(FakeToolRunner(installed={"git"})).available() is False


def test_supplying_creates_the_target_repository_and_runs_filter_repo_from_the_source() -> None:
    runner = FakeToolRunner(installed={EXECUTABLE, "git"})
    outcome = adapter(runner).supply(request(RENAMES))
    assert outcome.ok is True
    assert runner.calls == [["git", "init", "--quiet", "--initial-branch", "main", "/out/core"], list(adapter().arguments(request(RENAMES)))]


def test_an_existing_target_is_refused_and_nothing_runs() -> None:
    fs, runner = FakeFileSystem(), FakeToolRunner(installed={EXECUTABLE, "git"})
    fs.add(Path("/out/core/keep.txt"), "x")
    outcome = adapter(runner, fs).supply(request(RENAMES))
    assert outcome.ok is False
    assert "/out/core" in outcome.detail
    assert runner.calls == []


def test_a_filter_repo_failure_is_returned_with_its_message() -> None:
    runner = FakeToolRunner(installed={EXECUTABLE, "git"}, results={EXECUTABLE: ToolResult(1, "", "fatal: bad path")})
    outcome = adapter(runner).supply(request(RENAMES))
    assert outcome.ok is False
    assert "fatal: bad path" in outcome.detail


def test_a_failed_git_init_stops_before_filter_repo() -> None:
    runner = FakeToolRunner(installed={EXECUTABLE, "git"}, results={"git": ToolResult(128, "", "no")})
    outcome = adapter(runner).supply(request(RENAMES))
    assert outcome.ok is False
    assert [call[0] for call in runner.calls] == ["git"]


# -- the real tool -----------------------------------------------------------------------------------------------

def _git(cwd: Path, *argv: str) -> str:
    done = subprocess.run(["git", *argv], cwd=cwd, capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.skipif(not SubprocessToolRunner().available(EXECUTABLE), reason="git-filter-repo is not installed")
def test_the_real_tool_keeps_history_applies_the_renames_and_leaves_the_monorepo_alone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_CONFIG_COUNT", raising=False)  # conftest's secrets guard drops GIT_CONFIG_KEY_*, which would leave git unable to parse its own config
    mono = tmp_path / "mono"
    (mono / "src/app/core").mkdir(parents=True)
    (mono / "other").mkdir()
    _ = _git(tmp_path, "init", "--quiet", "--initial-branch", "main", str(mono))
    _ = _git(mono, "config", "user.email", "a@b.c")
    _ = _git(mono, "config", "user.name", "t")
    _ = (mono / "src/app/core/a.py").write_text("a\n")
    _ = (mono / "other/c.py").write_text("c\n")
    _ = _git(mono, "add", "-A")
    _ = _git(mono, "commit", "--quiet", "-m", "one")
    _ = (mono / "src/app/core/a.py").write_text("a2\n")
    _ = _git(mono, "commit", "--quiet", "-am", "two")
    before = _git(mono, "log", "--format=%H").split()
    target = tmp_path / "out" / "core"
    target.parent.mkdir()
    library = FilterRepo(SubprocessToolRunner(), LocalFileSystem())

    outcome = library.supply(MigrationRequest("core", mono, target, "main", (PathRename("src/app/core", "."),), ()))

    assert outcome.ok, outcome.detail
    assert _git(target, "ls-tree", "-r", "--name-only", "main").split() == ["a.py"]
    assert _git(target, "log", "--format=%s", "main").split() == ["two", "one"]
    assert _git(mono, "log", "--format=%H").split() == before
    assert (mono / "other/c.py").read_text() == "c\n"
