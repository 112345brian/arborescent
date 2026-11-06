"""The strict preset also installs a pre-push hook, so a push that would fail CI's ``arborescent check`` is blocked first."""

from __future__ import annotations

import ast
import shlex
import subprocess
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy

from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError, ToolResult
from arborescent.filesystem_adapter import LocalFileSystem
from arborescent.git_hook import (
    HOOK_MARKER,
    SKIP_VARIABLE,
    ensure_git_hook,
    render_hook,
)
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
        assert not fs.exists(HOOK)


def test_git_hook_true_installs_under_a_preset_that_normally_would_not() -> None:
    fs, runner = repo_fs(), git_says()
    outcome = ensure_git_hook(mini_policy(preset="relaxed", git_hook=True), ROOT, fs, runner)
    assert outcome.status == "installed" and HOOK_MARKER in fs.read_text(HOOK)


def test_git_hook_false_removes_a_hook_the_strict_preset_had_installed() -> None:
    fs, runner = repo_fs(), git_says()
    _ = ensure_git_hook(mini_policy(), ROOT, fs, runner)
    assert fs.is_file(HOOK)
    outcome = ensure_git_hook(mini_policy(git_hook=False), ROOT, fs, runner)
    assert outcome.status == "removed" and outcome.path == HOOK
    assert not fs.exists(HOOK)


def test_git_hook_false_with_no_existing_hook_does_nothing() -> None:
    fs, runner = repo_fs(), git_says()
    outcome = ensure_git_hook(mini_policy(git_hook=False), ROOT, fs, runner)
    assert outcome.status == "not-strict" and not fs.exists(HOOK)


def test_git_hook_false_never_removes_a_hook_we_did_not_write() -> None:
    fs, runner = repo_fs(), git_says()
    fs.mkdir(HOOK.parent)
    fs.write_text(HOOK, "#!/bin/sh\necho mine\n")
    outcome = ensure_git_hook(mini_policy(git_hook=False), ROOT, fs, runner)
    assert outcome.status == "foreign" and fs.read_text(HOOK) == "#!/bin/sh\necho mine\n"


def test_git_hook_is_reported_as_a_customization() -> None:
    assert "git hook" in mini_policy(git_hook=False).customization_labels
    assert "git hook" not in mini_policy().customization_labels


def test_git_hook_rejects_non_boolean_values() -> None:
    with pytest.raises(PolicyError, match="git_hook"):
        mini_policy(git_hook="yes")


def test_git_hook_cannot_be_combined_with_scopes_at_the_root() -> None:
    scope = {"rings": ["domain"], "classify": {}, "sources": ["a"], "source_roots": ["a"]}
    with pytest.raises(PolicyError, match="git_hook"):
        policy_from_mapping({"git_hook": False, "scopes": {"a": scope}})


def test_git_hook_is_set_per_scope() -> None:
    scope = {"rings": ["domain"], "classify": {}, "preset": "relaxed"}
    raw = {
        "scopes": {
            "a": {**scope, "sources": ["a/src"], "source_roots": ["a/src"], "git_hook": True},
            "b": {**scope, "sources": ["b/src"], "source_roots": ["b/src"]},
        }
    }
    policy = policy_from_mapping(raw)

    assert policy.scope("a").policy.preset.git_hook
    assert not policy.scope("b").policy.preset.git_hook


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


def _run_hook(
    tmp_path: Path, interpreter: str, *, venv: str | None = None, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run the rendered hook under ``sh`` with fake interpreters; return its exit code, stderr and what the interpreters were asked."""
    log = tmp_path / "calls.log"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("python", "python3"):
        _fake_python(bin_dir / name, interpreter, log, "path")
    if venv is not None:
        (tmp_path / ".venv" / "bin").mkdir(parents=True)
        _fake_python(tmp_path / ".venv" / "bin" / "python", venv, log, "venv")
    script = tmp_path / "pre-push"
    script.write_text(render_hook(""), encoding="utf-8")
    done = subprocess.run(
        ["sh", str(script)], cwd=tmp_path, env={"PATH": f"{bin_dir}:/usr/bin:/bin", **(env or {})}, capture_output=True, text=True, check=False
    )
    return done.returncode, done.stderr, log.read_text(encoding="utf-8") if log.exists() else ""


def _fake_python(path: Path, behaviour: str, log: Path, label: str) -> None:
    """``behaviour``: "ok" has arborescent installed and the check passes, "violations" fails the check, "missing" has no arborescent.

    Every call is logged as ``<label>:<arguments>``, so a test can tell which interpreter the hook chose."""
    path.write_text(
        "#!/bin/sh\n"
        f'echo "{label}:$*" >> {shlex.quote(str(log))}\n'
        'case "$1" in\n'
        f'  -c) [ "{behaviour}" = missing ] && exit 1; exit 0;;\n'
        f'  -m) [ "{behaviour}" = violations ] && exit 1; exit 0;;\n'
        "esac\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
def test_a_python_without_arborescent_does_not_block_the_push(tmp_path: Path) -> None:
    """The hook stopped every push with "No module named arborescent" when the PATH python was not the project's."""
    code, stderr, calls = _run_hook(tmp_path, "missing")
    assert code == 0 and "no arborescent installed" in stderr and "skipping" in stderr
    assert "-m arborescent" not in calls


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
def test_an_installed_arborescent_that_fails_the_check_still_blocks_the_push(tmp_path: Path) -> None:
    """Only an absent arborescent is skipped; one that is installed (even broken) runs the check and its failure blocks."""
    code, stderr, calls = _run_hook(tmp_path, "violations")
    assert code == 1 and "skipping" not in stderr
    assert "path:-m arborescent check --require-tools --require-profile" in calls


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
def test_the_project_virtualenv_is_preferred_over_the_path_python(tmp_path: Path) -> None:
    code, _, calls = _run_hook(tmp_path, "missing", venv="ok")
    assert code == 0 and "venv:-m arborescent check" in calls
    assert not any(line.startswith("path:") for line in calls.splitlines())  # the PATH python was never even asked


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
def test_the_path_python_is_used_when_there_is_no_virtualenv(tmp_path: Path) -> None:
    code, _, calls = _run_hook(tmp_path, "ok")
    assert code == 0 and "path:-m arborescent check" in calls


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="needs a POSIX shell")
def test_the_skip_variable_bypasses_this_push_without_running_any_python(tmp_path: Path) -> None:
    """``ARBORESCENT_SKIP_HOOK=1 git push`` skips only this check, unlike ``--no-verify`` which skips every hook."""
    code, stderr, calls = _run_hook(tmp_path, "violations", env={SKIP_VARIABLE: "1"})
    assert code == 0 and SKIP_VARIABLE in stderr and "skipping" in stderr
    assert calls == ""  # no interpreter was even looked for
