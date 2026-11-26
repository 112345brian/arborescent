"""On Windows tach 0.35.x walks no files under a forward-slash ``source_roots`` entry such as ``libs/a/src`` (#214), so
``SubprocessToolRunner`` runs it on a temporary copy of the project whose ``tach.toml`` spells them with backslashes."""

from __future__ import annotations

import os
import posixpath
import shutil
import subprocess
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import (
    CHECKERS,
    generate,
    mini_policy,
    services,
    write_files,
)

from arborescent import tool_runner
from arborescent.adapters import LocalFileSystem, SubprocessToolRunner
from arborescent.certification import _tach_external_probe, _tach_layer_probe
from arborescent.domain import ToolResult
from arborescent.engine import Options, run_tool
from arborescent.engines import Tach, TachExternal
from arborescent.facts import ModuleInfo, SourceFacts
from arborescent.generated_config import unread_tach_roots, windows_tach_config
from arborescent.testing import FakeToolRunner

CONFIG = """# a comment with ] and "quotes"
exact = true
source_roots = [
    "libs/a/src",  # trailing ] comment
    'vendor/b/src/',
]
exclude = ["docs/build"]

[[modules]]
path = "one"
depends_on = ["two"]

[[modules]]
path = "two"
depends_on = []
"""
PROJECT = {
    "tach.toml": CONFIG,
    "pyproject.toml": '[project]\nname = "root"\ndependencies = []\n',
    "libs/a/pyproject.toml": '[project]\nname = "a"\ndependencies = []\n',
    "libs/a/src/one/__init__.py": "import two\n",
    "libs/a/src/two/__init__.py": "VALUE = 2\n",
    "vendor/b/pyproject.toml": '[project]\nname = "b"\ndependencies = []\n',
    "vendor/b/src/three/__init__.py": "VALUE = 3\n",
}
# `one` also imports a third-party package: another source root's, which its own pyproject.toml declares.
REAL = {
    **PROJECT,
    "libs/a/pyproject.toml": '[project]\nname = "a"\ndependencies = ["b"]\n',
    "libs/a/src/one/__init__.py": "import three\nimport two\n",
}


def test_forward_slash_source_roots_are_rewritten_and_nothing_else_changes() -> None:
    rewritten = windows_tach_config(CONFIG)
    assert rewritten is not None
    before, after = tomllib.loads(CONFIG), tomllib.loads(rewritten)
    assert after.pop("source_roots") == ["libs\\a\\src", "vendor\\b\\src"]
    before.pop("source_roots")
    assert after == before
    assert rewritten.startswith('# a comment with ] and "quotes"\n')


def test_a_dot_prefix_and_doubled_separators_are_normalised() -> None:
    rewritten = windows_tach_config('source_roots = ["./libs/a/src", "vendor//b/src"]\n')
    assert rewritten is not None
    assert tomllib.loads(rewritten)["source_roots"] == ["libs\\a\\src", "vendor\\b\\src"]


def test_crlf_and_glob_roots_are_handled() -> None:
    rewritten = windows_tach_config('source_roots = ["libs/*/src", "a/b/c"]\r\nexact = true\r\n')
    assert rewritten is not None
    assert tomllib.loads(rewritten) == {"source_roots": ["libs/*/src", "a\\b\\c"], "exact": True}


@pytest.mark.parametrize("text", [
    "",
    "exact = true\n",
    "source_roots = []\n",
    'source_roots = ["."]\n',
    'source_roots = [""]\n',
    'source_roots = ["src"]\n',
    "source_roots = ['libs\\a\\src']\n",
    'source_roots = ["libs/*/src"]\n',
    'source_roots = ["../a/src"]\n',  # outside the folder that is copied
    'source_roots = ["/abs/src"]\n',
    'source_roots = ["C:/abs/src"]\n',
    'source_roots = "libs/a/src"\n',
    "source_roots = [1]\n",
    "source_roots = [\n",
    '[table]\nsource_roots = ["libs/a/src"]\n',
    '"source_roots" = ["libs/a/src"]\n',  # a spelling the rewrite does not recognise stays as it is
    "source_roots = [\"a\x00/b\"]\n",
])
def test_a_config_tach_reads_as_it_is_or_the_rewrite_cannot_follow_is_left_alone(text: str) -> None:
    assert windows_tach_config(text) is None


@dataclass
class Spy:
    """Stands in for ``subprocess.run`` and records what the tool would have seen in its working directory."""

    result: ToolResult = field(default_factory=lambda: ToolResult(3, "out", "err"))
    cwds: list[Path] = field(default_factory=lambda: [])
    files: list[set[str]] = field(default_factory=lambda: [])
    configs: list[str] = field(default_factory=lambda: [])

    def __call__(self, argv: Sequence[str], *, cwd: Path, **_: Any) -> subprocess.CompletedProcess[str]:
        self.cwds.append(Path(cwd))
        self.files.append({path.relative_to(cwd).as_posix() for path in Path(cwd).rglob("*")})
        config = Path(cwd) / "tach.toml"
        self.configs.append(config.read_text(encoding="utf-8") if config.is_file() else "")
        assert self.result.returncode is not None
        return subprocess.CompletedProcess(argv, self.result.returncode, self.result.stdout, self.result.stderr)


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    recorder = Spy()
    monkeypatch.setattr(tool_runner, "resolve_executable", lambda executable, **_: executable)
    monkeypatch.setattr(subprocess, "run", recorder)
    return recorder


@pytest.mark.parametrize("command", [["tach", "check", "--dependencies"], ["tach", "check-external"]])
def test_on_windows_tach_runs_on_a_rewritten_copy_that_is_removed_afterwards(tmp_path: Path, spy: Spy, command: list[str]) -> None:
    write_files(tmp_path, {
        **PROJECT, ".gitignore": "ignored/\n", "libs/a/src/one/data.bin": "x", "libs/a/src/one/stub.pyi": "",
        "node_modules/pkg/x.py": "", "env/pyvenv.cfg": "", "env/lib/x.py": "", ".git/info/exclude": "x\n", ".git/HEAD": "ref\n",
    })
    result = SubprocessToolRunner(windows=True).run(command, tmp_path)

    assert (result.returncode, result.stdout, result.stderr) == (3, "out", "err")
    assert spy.cwds[0] != tmp_path and not spy.cwds[0].exists()
    assert tomllib.loads(spy.configs[0])["source_roots"] == ["libs\\a\\src", "vendor\\b\\src"]
    assert {*PROJECT, ".gitignore", "libs/a/src/one/stub.pyi", ".git/info/exclude"} <= spy.files[0]
    assert not {"libs/a/src/one/data.bin", "node_modules/pkg/x.py", "env/lib/x.py", ".git/HEAD"} & spy.files[0]
    assert (tmp_path / "tach.toml").read_text(encoding="utf-8") == CONFIG


@pytest.mark.parametrize(("windows", "command", "config"), [
    (False, ["tach", "check", "--dependencies"], CONFIG),
    (True, ["tach", "--version"], CONFIG),
    (True, ["tach"], CONFIG),
    (True, ["ruff", "check"], CONFIG),
    (True, ["tach", "check", "--dependencies"], 'source_roots = ["src"]\n'),
    (True, ["tach", "check", "--dependencies"], None),
])
def test_everything_else_runs_in_the_project_itself(
    tmp_path: Path, spy: Spy, windows: bool, command: list[str], config: str | None
) -> None:
    if config is not None:
        write_files(tmp_path, {"tach.toml": config})
    _ = SubprocessToolRunner(windows=windows).run(command, tmp_path)
    assert spy.cwds == [tmp_path]


@pytest.mark.parametrize("engine", [Tach(), TachExternal()])
def test_a_copy_that_cannot_be_made_fails_the_engine_instead_of_running_tach_blind(
    tmp_path: Path, spy: Spy, monkeypatch: pytest.MonkeyPatch, engine: Tach | TachExternal
) -> None:
    write_files(tmp_path, PROJECT)

    def refuse(*_: object, **__: object) -> None:
        raise PermissionError("locked")

    monkeypatch.setattr(shutil, "copyfile", refuse)
    runner = SubprocessToolRunner(windows=True)
    assert runner.run(engine.command([]), tmp_path).returncode == -1
    run = run_tool(services(runner), engine, [], tmp_path, Options())
    assert run.status == "fail" and "locked" in run.lines[-1]
    assert spy.cwds == []


@pytest.mark.parametrize("returncode", [-1, None])
def test_certification_does_not_read_a_tach_that_did_not_run_as_a_rejected_canary(tmp_path: Path, returncode: int | None) -> None:
    write_files(tmp_path, {"a.py": "VALUE = 1\n"})
    runner = FakeToolRunner({"tach"}, {"tach": ToolResult(returncode, "", "tach: could not run")})
    module = ModuleInfo("a", "a.py", False, SourceFacts((), (), None))
    assert not _tach_external_probe(tmp_path, tmp_path / "a.py", runner, LocalFileSystem())
    assert not _tach_layer_probe(tmp_path, module, ("b",), runner, LocalFileSystem())


@pytest.mark.skipif(shutil.which("tach") is None, reason="Tach is an optional install extra")
@pytest.mark.parametrize("engine", [Tach(), TachExternal()])
def test_real_tach_sees_the_files_under_nested_source_roots(tmp_path: Path, engine: Tach | TachExternal) -> None:
    """The end-to-end proof is the Windows CI leg; elsewhere this only shows the project itself is clean."""
    write_files(tmp_path, REAL)
    result = SubprocessToolRunner().run(engine.command([]), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.skipif(shutil.which("tach") is None, reason="Tach is an optional install extra")
def test_real_tach_still_rejects_a_forbidden_import_under_nested_source_roots(tmp_path: Path) -> None:
    write_files(tmp_path, {**REAL, "libs/a/src/two/__init__.py": "import one\n"})
    result = SubprocessToolRunner().run(Tach().command([]), tmp_path)
    assert result.returncode == 1 and "two" in result.stderr and "No first-party imports" not in result.stderr


@pytest.mark.skipif(shutil.which("tach") is None, reason="Tach is an optional install extra")
@pytest.mark.parametrize("two", ["VALUE = 2\n", "import one\n"])
def test_the_copy_holds_everything_real_tach_reads(tmp_path: Path, two: str) -> None:
    """Run tach on the project and on the copy (without the rewrite, which only Windows can read): same verdicts."""
    project, copy = tmp_path / "project", tmp_path / "copy"
    write_files(project, {**REAL, "libs/a/src/two/__init__.py": two})
    copy.mkdir()
    tool_runner._copy_for_tach(project, copy)
    runner = SubprocessToolRunner(windows=False)
    for engine in (Tach(), TachExternal()):
        original, copied = runner.run(engine.command([]), project), runner.run(engine.command([]), copy)
        assert (copied.returncode, copied.stdout, copied.stderr) == (original.returncode, original.stdout, original.stderr)


# --- spellings tach does not read on macOS and Linux either: reported, never rewritten ----------------------------------------

# What real tach 0.35.3 reads and does not, measured; `test_real_tach_agrees_about_which_spellings_it_reads` re-measures it.
UNREAD = [
    "./a/b", "./a/b/", ".//a/b", "a//b/c", "a/./b/c", "a/c/../b", "a/..", "./libs/*/src", "libs//*/src", "libs/./*/src",
    "libs/*//src", "libs/*/./src", "libs/*/src/", "libs/*/src/.",
]
READ = ["a/b", "./a", "././a", "a//b", "a///b", "a/b//c", "a/./b", "a/b/", "a/b//", "a/b/.", "./src", "src/", "./", ".", "libs/*/src"]
CANONICAL = {"./a/b": "a/b", "a//b/c": "a/b/c", "a/c/../b": "a/b", "./libs/*/src": "libs/*/src", "libs/*/src/": "libs/*/src", "a/..": "."}


def _config(*roots: str) -> str:
    return f"source_roots = [{', '.join(repr(root) for root in roots)}]\n"


@pytest.mark.parametrize("root", UNREAD)
def test_a_spelling_tach_does_not_read_is_named_with_its_canonical_form(root: str) -> None:
    ((written, canonical),) = unread_tach_roots(_config(root))
    assert written == root and canonical == CANONICAL.get(root, canonical) and not unread_tach_roots(_config(canonical))


@pytest.mark.parametrize("root", [*READ, "/abs/./a/b", "../sib/src", ""])
def test_a_spelling_tach_reads_or_a_root_outside_the_project_is_not_reported(root: str) -> None:
    assert unread_tach_roots(_config(root)) == []


@pytest.mark.parametrize("text", [
    "", "exact = true\n", "source_roots = []\n", 'source_roots = "./a/b"\n', "source_roots = [1]\n", "source_roots = [\n",
    '[table]\nsource_roots = ["./a/b"]\n', 'source_roots = ["a\x00/b"]\n',
])
def test_an_unusable_config_reports_nothing_and_leaves_the_error_to_tach(text: str) -> None:
    assert unread_tach_roots(text) == []


def test_the_pyproject_table_is_read_when_asked_for() -> None:
    assert unread_tach_roots('[tool.tach]\nsource_roots = ["./a/b"]\n', table=("tool", "tach")) == [("./a/b", "a/b")]
    assert unread_tach_roots("[tool]\ntach = 1\n", table=("tool", "tach")) == []


@pytest.mark.parametrize("engine", [Tach(), TachExternal()])
@pytest.mark.parametrize("files", [
    {"tach.toml": _config("./a/b", "src")},
    {"pyproject.toml": "[tool.tach]\n" + _config("./a/b", "src")},
])
def test_off_windows_an_unread_source_root_fails_the_engine_and_says_how_to_write_it(
    tmp_path: Path, spy: Spy, engine: Tach | TachExternal, files: dict[str, str]
) -> None:
    write_files(tmp_path, files)
    run = run_tool(services(SubprocessToolRunner(windows=False)), engine, [], tmp_path, Options())  # pyright: ignore[reportArgumentType]
    assert run.status == "fail" and spy.cwds == []
    assert "'./a/b'" in run.lines[-1] and "'a/b'" in run.lines[-1] and "unchecked" in run.lines[-1]


def test_tach_toml_wins_over_the_pyproject_table(tmp_path: Path, spy: Spy) -> None:
    write_files(tmp_path, {"tach.toml": _config("a/b"), "pyproject.toml": "[tool.tach]\n" + _config("./a/b")})
    _ = SubprocessToolRunner(windows=False).run(["tach", "check"], tmp_path)
    assert spy.cwds == [tmp_path]


def test_other_tach_commands_and_other_tools_are_not_stopped(tmp_path: Path, spy: Spy) -> None:
    write_files(tmp_path, {"tach.toml": _config("./a/b")})
    runner = SubprocessToolRunner(windows=False)
    for command in (["tach", "--version"], ["ruff", "check"]):
        assert runner.run(command, tmp_path).returncode == 3
    assert spy.cwds == [tmp_path, tmp_path]


def test_on_windows_the_copy_still_normalises_instead_of_reporting(tmp_path: Path, spy: Spy) -> None:
    write_files(tmp_path, {"tach.toml": _config("./a/b")})
    assert SubprocessToolRunner(windows=True).run(["tach", "check"], tmp_path).returncode == 3
    assert tomllib.loads(spy.configs[0])["source_roots"] == ["a\\b"]


def test_a_config_that_cannot_be_decoded_is_left_for_tach_to_report(tmp_path: Path, spy: Spy) -> None:
    _ = (tmp_path / "pyproject.toml").write_bytes(b"\xff\xfe\x00")
    _ = SubprocessToolRunner(windows=False).run(["tach", "check"], tmp_path)
    assert spy.cwds == [tmp_path]


def test_generated_configs_spell_source_roots_the_way_tach_reads_them() -> None:
    policy = mini_policy(source_roots=["./a/b", "a//c/d", "."])
    out = generate(policy, ["app"], CHECKERS)
    config = next(text for name, text in out.items() if str(name).endswith("tach.toml"))
    assert tomllib.loads(config)["source_roots"] == ["a/b", "a/c/d", "."]


@pytest.mark.skipif(shutil.which("tach") is None or os.name == "nt", reason="needs tach; Windows reads the copy instead")
@pytest.mark.parametrize("root", [*UNREAD, *READ])
def test_real_tach_agrees_about_which_spellings_it_reads(tmp_path: Path, root: str) -> None:
    """Every spelling tach leaves unchecked must be reported (`UNREAD`), and no spelling it reads may be (`READ`)."""
    folder = posixpath.normpath(root.replace("*", "x"))
    write_files(tmp_path, {
        f"{folder}/one/__init__.py": "VALUE = 1\n", f"{folder}/two/__init__.py": "import one\n", "a/c/keep.txt": "", "a/b/keep.txt": "",
        "tach.toml": _config(root) + '[[modules]]\npath = "one"\ndepends_on = []\n[[modules]]\npath = "two"\ndepends_on = []\n',
    })
    executable = tool_runner.resolve_executable("tach")
    assert executable is not None
    done = subprocess.run([executable, "check", "--dependencies"], cwd=tmp_path, capture_output=True, text=True, check=False)
    assert ("Cannot use" in done.stdout + done.stderr) == (root in READ), done.stdout + done.stderr
