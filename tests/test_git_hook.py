"""The strict preset also installs a pre-push hook, so a push that would fail CI's ``arborescent check`` is blocked first."""

from __future__ import annotations

import ast
import shlex
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy

from arborescent.domain import ToolResult
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.git_hook import HOOK_MARKER, ensure_git_hook, render_hook
from arborescent.presets import PRESETS
from arborescent.testing import FakeFileSystem, FakeToolRunner

ROOT = Path("/repo")
HOOK = ROOT / ".git" / "hooks" / "pre-push"


def git_says(path: Path = HOOK, prefix: str = "") -> FakeToolRunner:
    return FakeToolRunner(installed={"git"}, results={"git": ToolResult(0, f"{path.as_posix()}\n{prefix}\n", "")})


def repo_fs() -> FakeFileSystem:
    fs = FakeFileSystem()
    fs.mkdir(ROOT)
    return fs


def test_only_the_strict_preset_installs_a_hook() -> None:
    assert PRESETS["strict"].git_hook
    assert not PRESETS["standard"].git_hook
    assert not PRESETS["relaxed"].git_hook


def test_strict_policy_gets_a_hook_that_runs_the_ci_check() -> None:
    fs, runner = repo_fs(), git_says()
    outcome = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    assert outcome.status == "installed" and outcome.path == HOOK
    text = fs.read_text(HOOK)
    assert text.startswith("#!/bin/sh\n") and HOOK_MARKER in text
    assert "arborescent check --require-tools --require-profile" in text
    assert "\r" not in text
    assert HOOK.as_posix() in fs.executable


def test_standard_and_relaxed_presets_leave_git_alone() -> None:
    for preset in ("standard", "relaxed"):
        fs, runner = repo_fs(), git_says()
        outcome = ensure_git_hook(mini_policy(preset=preset), ROOT, fs, runner)
        assert outcome.status == "not-strict"
        assert not fs.exists(HOOK) and runner.calls == []


def test_a_second_run_changes_nothing() -> None:
    fs, runner = repo_fs(), git_says()
    _ = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    again = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    assert again.status == "current"


def test_a_stale_hook_of_ours_is_rewritten() -> None:
    fs, runner = repo_fs(), git_says()
    fs.mkdir(HOOK.parent)
    fs.write_text(HOOK, f"#!/bin/sh\n{HOOK_MARKER}\nexit 0\n")
    outcome = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    assert outcome.status == "updated" and fs.read_text(HOOK) == render_hook("")


@pytest.mark.parametrize("existing", ["#!/bin/sh\necho mine\n", "", "\x00\x01"])
def test_a_hook_we_did_not_write_is_never_overwritten(existing: str) -> None:
    fs, runner = repo_fs(), git_says()
    fs.mkdir(HOOK.parent)
    fs.write_text(HOOK, existing)
    outcome = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    assert outcome.status == "foreign" and fs.read_text(HOOK) == existing


def test_a_binary_hook_is_foreign_not_a_crash() -> None:
    fs, runner = repo_fs(), git_says()
    fs.mkdir(HOOK.parent)
    fs.write_bytes(HOOK, b"\xff\xfe\x00")
    assert ensure_git_hook(mini_policy(), ROOT, fs, runner).status == "foreign"


def test_outside_a_git_repository_nothing_is_written() -> None:
    fs = repo_fs()
    for runner in (
        FakeToolRunner(),  # git is not installed
        FakeToolRunner(installed={"git"}, results={"git": ToolResult(128, "", "fatal: not a git repository")}),
        FakeToolRunner(installed={"git"}, results={"git": ToolResult(0, "", "")}),  # no output at all
        FakeToolRunner(installed={"git"}, results={"git": ToolResult(0, '{"version":1}', "")}),  # not a hook path
    ):
        assert ensure_git_hook(mini_policy(), ROOT, fs, runner).status == "no-git"
    assert not fs.exists(HOOK)


def test_a_policy_below_the_repository_root_runs_from_its_own_folder() -> None:
    fs, runner = repo_fs(), git_says(prefix="services/api/")
    _ = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    assert 'cd "$(git rev-parse --show-toplevel)"/services/api || exit 1' + chr(10) in fs.read_text(HOOK)


def test_the_hook_folder_is_created_when_git_has_not_made_it() -> None:
    fs, runner = repo_fs(), git_says(path=ROOT / "custom" / "hooks" / "pre-push")
    assert ensure_git_hook(mini_policy(), ROOT, fs, runner).status == "installed"
    assert fs.is_file(ROOT / "custom" / "hooks" / "pre-push")


def test_a_relative_hook_path_is_taken_from_the_policy_root() -> None:
    fs, runner = repo_fs(), git_says(path=Path(".githooks/pre-push"))
    _ = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    assert fs.is_file(ROOT / ".githooks" / "pre-push")


def test_a_prefix_cannot_inject_shell() -> None:
    prefix = 'x"; rm -rf ~; "/'
    fs, runner = repo_fs(), git_says(prefix=prefix)
    _ = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    assert f'cd "$(git rev-parse --show-toplevel)"/{shlex.quote(prefix.rstrip("/"))} || exit 1' + chr(10) in fs.read_text(HOOK)


def test_the_real_file_system_gets_an_executable_hook(tmp_path: Path) -> None:
    hook = tmp_path / ".git" / "hooks" / "pre-push"
    runner = git_says(path=hook)
    assert ensure_git_hook(mini_policy(), tmp_path, LocalFileSystem(), runner).status == "installed"
    assert hook.read_bytes().startswith(b"#!/bin/sh\n") and b"\r" not in hook.read_bytes()
    if hook.stat().st_mode & 0o111 == 0:
        pytest.skip("this file system has no execute bit")


def test_every_caller_of_ensure_git_hook_looks_at_the_outcome() -> None:
    """A new ``foreign`` / ``no-git`` outcome must not be silently dropped: no caller may discard the result."""
    src = Path(__file__).parents[1] / "src" / "arborescent"
    for path in src.glob("*.py"):
        if path.name == "git_hook.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
                func = node.value.func
                assert not (isinstance(func, ast.Name) and func.id == "ensure_git_hook"), f"{path.name} discards the outcome"
