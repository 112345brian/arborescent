"""Every walker over a project's folders prunes while walking: installed environments and nested checkouts (clones,
linked worktrees such as ``.claude/worktrees/*``, submodules) are not entered, rather than listed and filtered after."""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, services, write_files
from fs_helpers import KINDS, make_fs, put

from arborescent import tool_runner
from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.domain import ToolResult
from arborescent.engine import _reference_sources
from arborescent.package_gaps import asset_paths
from arborescent.ports import FileSystem
from arborescent.practices import function_references
from arborescent.selftest import selftest
from arborescent.testing import FakeToolRunner

FS = LocalFileSystem()
LINK = "gitdir: /elsewhere/.git/worktrees/x\n"


@pytest.fixture
def entered(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Every folder listed through ``os.scandir`` (``os.walk`` and ``Path.rglob`` on 3.11 and 3.12 both use it)."""
    seen: list[str] = []
    scandir = os.scandir

    def spy(path: object = ".") -> object:
        seen.append(str(path).replace("\\", "/"))
        return scandir(path)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "scandir", spy)
    yield seen


def below(seen: list[str], marker: str) -> list[str]:
    return [path for path in seen if f"/{marker}/" in path + "/"]


# --- the port ---------------------------------------------------------------------------------------------------------


@pytest.fixture(params=KINDS)
def env(request: pytest.FixtureRequest, tmp_path: Path) -> tuple[FileSystem, Path]:
    fs, root = make_fs(request.param, tmp_path)
    fs.mkdir(root)
    return fs, root


TREE = {
    "a.py": "", ".venv/lib/b.py": "", "venv/__init__.py": "", "venv/c.py": "", "venv/sub/.venv/d.py": "",
    "wt/linked/.git": LINK, "wt/linked/e.py": "", "clone/.git/HEAD": "ref", "clone/deep/f.py": "",
}


def listed(found: list[Path], root: Path) -> list[str]:
    return [path.relative_to(root).as_posix() for path in found]


def test_walk_files_can_enter_nested_checkouts_while_still_pruning_names(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    put(fs, root, TREE)
    prune = {".venv", "venv", ".git"}
    assert listed(fs.walk_files(root, prune), root) == ["a.py"]
    assert listed(fs.walk_files(root, prune, enter_checkouts=True), root) == [
        "a.py", "clone/deep/f.py", "wt/linked/.git", "wt/linked/e.py",
    ]


def test_walk_files_can_enter_a_pruned_name_that_is_a_python_package(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    put(fs, root, TREE)
    found = listed(fs.walk_files(root, {".venv", "venv", ".git"}, enter_packages=True), root)
    assert found == ["a.py", "venv/__init__.py", "venv/c.py"]  # venv/sub/.venv is no package, so still pruned


def test_copy_tree_can_leave_nested_checkouts_behind(env: tuple[FileSystem, Path]) -> None:
    fs, root = env
    put(fs, root, {f"s/{name}": text for name, text in TREE.items()} | {"s/.git/HEAD": "ref"})
    fs.copy_tree(root / "s", root / "d", ignore=[".git", ".venv"], skip_checkouts=True)
    assert listed(fs.walk_files(root / "d", (), enter_checkouts=True), root / "d") == ["a.py", "venv/__init__.py", "venv/c.py"]
    assert not fs.exists(root / "d" / "wt" / "linked") and not fs.exists(root / "d" / "clone")
    fs.copy_tree(root / "s" / "clone", root / "asked", ignore=[".git"], skip_checkouts=True)
    assert fs.is_file(root / "asked" / "deep" / "f.py")  # a checkout asked for directly is copied


# --- package assets ---------------------------------------------------------------------------------------------------


def test_asset_paths_does_not_enter_environments_or_nested_checkouts(tmp_path: Path, entered: list[str]) -> None:
    write_files(tmp_path, {
        "app/data.json": "{}", "app/venv/__init__.py": "", "app/venv/activate.txt": "x", "app/build/out.bin": "x",
        "app/.venv/lib/pkg/data.txt": "x", "app/node_modules/m/index.js": "x",
        "app/.claude/worktrees/w/.git": LINK, "app/.claude/worktrees/w/page.html": "x",
        "app/vendored_clone/.git/HEAD": "ref", "app/vendored_clone/readme.md": "x",
    })
    assert asset_paths(mini_policy(), tmp_path, FS) == ("app/data.json", "app/venv/activate.txt")
    assert not below(entered, ".venv") and not below(entered, "node_modules") and not below(entered, "build")
    assert not below(entered, "w/page.html")


# --- the tests folder and the reference paths ------------------------------------------------------------------------


def test_function_references_do_not_enter_environments_or_nested_checkouts(tmp_path: Path, entered: list[str]) -> None:
    test = "from app.domain import parse\n\ndef test_p():\n    parse(1)\n"
    write_files(tmp_path, {
        "app/__init__.py": "", "app/domain.py": "def parse(x):\n    return x\n",
        "tests/test_a.py": test, "tests/.venv/lib/test_b.py": test,
        "tests/wt/.git": LINK, "tests/wt/test_c.py": test,
    })
    scan = FsScanner().scan(mini_policy(), tmp_path)
    entered.clear()
    assert [ref.path for ref in function_references(FS, tmp_path, "tests", scan)] == ["tests/test_a.py"]
    assert not below(entered, ".venv")


def test_reference_paths_skip_environments_while_walking_but_keep_a_vendored_submodule(
    tmp_path: Path, entered: list[str],
) -> None:
    """A reference path is named to be compared against, so a submodule below it is part of what was asked for."""
    write_files(tmp_path, {
        "vendor/a.py": "A = 1\n", "vendor/.venv/lib/b.py": "", "vendor/node_modules/c.py": "",
        "vendor/kit/.git": "gitdir: ../../.git/modules/kit\n", "vendor/kit/d.py": "D = 1\n",
    })
    assert sorted(_reference_sources(tmp_path, FS, "vendor")) == ["vendor/a.py", "vendor/kit/d.py"]
    assert not below(entered, ".venv") and not below(entered, "node_modules")
    assert list(_reference_sources(tmp_path, FS, "vendor/a.py")) == ["vendor/a.py"]
    assert _reference_sources(tmp_path, FS, "missing") == {}


# --- the scanner without git, and the selftest copy -------------------------------------------------------------------

NESTED = {
    "app/__init__.py": "", "app/domain.py": "X = 1\n",
    "app/wt/.git": LINK, "app/wt/other.py": "Y = 1\n",
    ".claude/worktrees/w/.git": LINK, ".claude/worktrees/w/app/domain.py": "Z = 1\n",
}


class _NoGit(FakeToolRunner):
    def available(self, executable: str) -> bool:
        return executable != "git" and super().available(executable)


def test_the_scan_without_git_leaves_nested_checkouts_alone_as_git_does(tmp_path: Path, entered: list[str]) -> None:
    write_files(tmp_path, NESTED)
    scan = FsScanner(runner=_NoGit()).scan(mini_policy(sources=["."], source_roots=["."]), tmp_path)
    assert sorted(scan.modules) == ["app", "app.domain"]
    assert not below(entered, "w/app")


class _Copies(LocalFileSystem):
    def __init__(self) -> None:
        self.copied: list[list[str]] = []

    def copy_tree(self, src: Path, dst: Path, ignore: object = (), *, skip_checkouts: bool = False) -> None:
        super().copy_tree(src, dst, ignore, skip_checkouts=skip_checkouts)  # type: ignore[arg-type]
        self.copied.append(sorted(path.relative_to(dst).as_posix() for path in dst.rglob("*.py")))


def test_the_selftest_copy_holds_no_nested_checkout(tmp_path: Path) -> None:
    write_files(tmp_path, NESTED)
    fs = _Copies()
    bound = services()
    report = selftest(mini_policy(), tmp_path, type(bound)(**{**vars(bound), "fs": fs}))
    assert report.killed
    assert fs.copied and all(copy == ["app/__init__.py", "app/domain.py"] for copy in fs.copied), fs.copied


# --- the migration driver's rewriter ----------------------------------------------------------------------------------


def test_the_rewriter_never_touches_environments_or_nested_checkouts(tmp_path: Path, entered: list[str]) -> None:
    pytest.importorskip("libcst")
    driver_libcst_scan = pytest.importorskip("arborescent.driver_libcst_scan")  # imports libcst

    write_files(tmp_path, {
        "src/a.py": "", "src/.venv/b.py": "", "src/node_modules/c.py": "",
        "src/wt/.git": LINK, "src/wt/d.py": "", "one.py": "",
    })
    found = driver_libcst_scan._files(tmp_path, ["src", "one.py"])
    assert listed(found, tmp_path) == ["one.py", "src/a.py"]
    assert not below(entered, ".venv") and not below(entered, "node_modules")


# --- the Windows tach copy --------------------------------------------------------------------------------------------


def tach_copy(tmp_path: Path, roots: str) -> list[str]:
    project, copy = tmp_path / "project", tmp_path / "copy"
    write_files(project, {
        "tach.toml": f"source_roots = {roots}\n", "src/a.py": "",
        "src/dep/.git": LINK, "src/dep/b.py": "",
        "libs/sub/.git": LINK, "libs/sub/src/c.py": "",
        ".claude/worktrees/w/.git": LINK, ".claude/worktrees/w/src/d.py": "",
        "other_clone/.git/HEAD": "ref", "other_clone/e.py": "",
    })
    copy.mkdir()
    tool_runner._copy_for_tach(project, copy)
    return sorted(path.relative_to(copy).as_posix() for path in copy.rglob("*.py"))


def test_the_tach_copy_leaves_out_nested_checkouts_that_no_source_root_names(tmp_path: Path) -> None:
    assert tach_copy(tmp_path, '["."]') == ["src/a.py"]


@pytest.mark.parametrize(("roots", "expected"), [
    ('["src", "libs/sub/src"]', ["libs/sub/src/c.py", "src/a.py", "src/dep/b.py"]),  # inside a root, and holding one
    ('["./src/", "libs/*/src"]', ["libs/sub/src/c.py", "src/a.py", "src/dep/b.py"]),  # other spellings, and a glob
])
def test_the_tach_copy_keeps_a_checkout_that_a_source_root_reaches(tmp_path: Path, roots: str, expected: list[str]) -> None:
    """Leaving out a submodule tach was told to read would let it pass without checking it."""
    assert tach_copy(tmp_path, roots) == expected


@pytest.mark.parametrize("config", [None, "source_roots = [", "source_roots = 3\n", "source_roots = []\n"])
def test_the_tach_copy_without_readable_source_roots_still_copies_the_project(tmp_path: Path, config: str | None) -> None:
    project, copy = tmp_path / "project", tmp_path / "copy"
    write_files(project, {"a.py": "", "wt/.git": LINK, "wt/b.py": "", **({} if config is None else {"tach.toml": config})})
    copy.mkdir()
    tool_runner._copy_for_tach(project, copy)
    assert sorted(path.name for path in copy.rglob("*.py")) == ["a.py"]


@pytest.mark.skipif(shutil.which("tach") is None, reason="Tach is an optional install extra")
def test_real_tach_gives_the_same_verdict_on_the_copy_of_a_project_with_a_submodule_root(tmp_path: Path) -> None:
    project, copy = tmp_path / "project", tmp_path / "copy"
    write_files(project, {
        "tach.toml": 'source_roots = ["src", "libs/sub/src"]\n[[modules]]\npath = "one"\ndepends_on = []\n'
                     '[[modules]]\npath = "two"\ndepends_on = []\n',
        "pyproject.toml": '[project]\nname = "root"\ndependencies = []\n',
        "src/one/__init__.py": "import two\n", "libs/sub/.git": LINK, "libs/sub/src/two/__init__.py": "",
    })
    copy.mkdir()
    tool_runner._copy_for_tach(project, copy)
    runner = tool_runner.SubprocessToolRunner(windows=False)
    original: ToolResult = runner.run(["tach", "check", "--dependencies"], project)
    copied = runner.run(["tach", "check", "--dependencies"], copy)
    assert original.returncode == 1 and (copied.returncode, copied.stderr) == (original.returncode, original.stderr)
