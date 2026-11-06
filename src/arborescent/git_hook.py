"""The strict preset's git hook: a pre-push hook that runs the same ``check`` CI runs, so a push that would fail CI is
stopped on the developer's machine. Installed when a repository is configured or checked under a preset with ``git_hook``
(or an explicit ``git_hook = true`` override), and removed again if ``git_hook`` is later turned off. A hook somebody
else wrote is never touched, in either direction. ``ARBORESCENT_SKIP_HOOK=1 git push`` skips this one check for one push
without touching other hooks; ``git push --no-verify`` is git's own way to skip every hook instead."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .domain import Policy
from .ports import FileSystem, ToolRunner

__all__ = ["HOOK_MARKER", "SKIP_VARIABLE", "HookOutcome", "ensure_git_hook", "render_hook"]

HOOK_NAME = "pre-push"
HOOK_MARKER = "# arborescent-managed git hook: rewritten by `arborescent check` and `arborescent generate`"
SKIP_VARIABLE = "ARBORESCENT_SKIP_HOOK"  # one push, this check only; `git push --no-verify` skips every hook instead

HookStatus = Literal["not-strict", "no-git", "installed", "updated", "current", "foreign", "removed"]


@dataclass(frozen=True)
class HookOutcome:
    status: HookStatus
    path: Path | None = None


def render_hook(prefix: str) -> str:
    """The hook script (LF only). ``prefix`` is the policy folder below the repository root, as ``git rev-parse --show-prefix`` prints it."""
    folder = prefix.strip().rstrip("/")
    enter = f'cd "$(git rev-parse --show-toplevel)"/{shlex.quote(folder)} || exit 1\n' if folder else ""
    return (
        "#!/bin/sh\n"
        f"{HOOK_MARKER}\n"
        # One push, this check only (other hooks still run); `git push --no-verify` is the native way to skip every hook.
        f'if [ -n "${SKIP_VARIABLE}" ]; then\n'
        f'  echo "arborescent pre-push: {SKIP_VARIABLE} is set; skipping the check for this push" >&2\n'
        "  exit 0\n"
        "fi\n"
        f"{enter}"
        # The project's own virtualenv first: the python on PATH is often not the one arborescent is installed in.
        "if [ -x .venv/bin/python ]; then PY=.venv/bin/python\n"
        "elif [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe\n"
        "else PY=python; command -v \"$PY\" >/dev/null 2>&1 || PY=python3; fi\n"
        # An interpreter that does not have arborescent at all says nothing about the code being pushed: say so and let CI decide,
        # rather than blocking every push (including one that fixes the environment) with "No module named arborescent". An
        # arborescent that is installed but broken is not skipped: the check runs and fails loudly.
        "if ! \"$PY\" -c 'import importlib.util, sys; sys.exit(importlib.util.find_spec(\"arborescent\") is None)' >/dev/null 2>&1; then\n"
        '  echo "arborescent pre-push: $PY has no arborescent installed; skipping the check (install it in the project environment, e.g. uv sync)" >&2\n'
        "  exit 0\n"
        "fi\n"
        'exec "$PY" -m arborescent check --require-tools --require-profile\n'
    )


def ensure_git_hook(policy: Policy, root: Path, fs: FileSystem, runner: ToolRunner) -> HookOutcome:
    """Install, refresh or remove the hook to match ``git_hook``. Never raises for an unusual repository: the
    outcome says what happened. A hook somebody else wrote (or one that can't be read) is never touched, whichever
    way ``git_hook`` is set."""
    located = _locate(root, runner)
    if located is None:
        return HookOutcome("no-git")
    path, prefix = located
    managed = _managed_contents(path, fs)
    if not policy.preset.git_hook:
        if managed is None:
            return HookOutcome("foreign", path) if fs.is_file(path) else HookOutcome("not-strict")
        fs.remove_file(path)
        return HookOutcome("removed", path)
    wanted = render_hook(prefix)
    if fs.is_file(path):
        if managed is None:
            return HookOutcome("foreign", path)
        if managed == wanted:
            return HookOutcome("current", path)
        status: HookStatus = "updated"
    else:
        status = "installed"
    fs.mkdir(path.parent)
    fs.write_text(path, wanted)
    fs.make_executable(path)
    return HookOutcome(status, path)


def _managed_contents(path: Path, fs: FileSystem) -> str | None:
    """The hook's current text, if it is Arborescent-managed (carries the marker); None if absent, unreadable or foreign."""
    if not fs.is_file(path):
        return None
    try:
        current = fs.read_text(path)
    except (OSError, UnicodeDecodeError):
        return None
    return current if HOOK_MARKER in current else None


def _locate(root: Path, runner: ToolRunner) -> tuple[Path, str] | None:
    """Where git wants the hook (it honours ``core.hooksPath`` and worktrees) and the policy folder's prefix; None outside a repository."""
    if not runner.available("git"):
        return None
    result = runner.run(["git", "rev-parse", "--git-path", f"hooks/{HOOK_NAME}", "--show-prefix"], root)
    if result.returncode != 0:
        return None
    lines = result.stdout.split("\n")
    where = Path(lines[0].strip())
    if where.name != HOOK_NAME:  # empty or unrelated output (not a repository, a wrapper): write nothing
        return None
    return root / where, lines[1] if len(lines) > 1 else ""
