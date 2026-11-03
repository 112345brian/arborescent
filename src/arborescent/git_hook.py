"""The strict preset's git hook: a pre-push hook that runs the same ``check`` CI runs, so a push that would fail CI is
stopped on the developer's machine. Installed when a repository is configured or checked under a preset with ``git_hook``;
a hook somebody else wrote is never touched."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .domain import Policy
from .ports import FileSystem, ToolRunner

__all__ = ["HOOK_MARKER", "HookOutcome", "ensure_git_hook", "render_hook"]

HOOK_NAME = "pre-push"
HOOK_MARKER = "# arborescent-managed git hook: rewritten by `arborescent check` and `arborescent generate`"

HookStatus = Literal["not-strict", "no-git", "installed", "updated", "current", "foreign"]


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
        f"{enter}"
        "PY=python\n"
        'command -v "$PY" >/dev/null 2>&1 || PY=python3\n'
        'exec "$PY" -m arborescent check --require-tools --require-profile\n'
    )


def ensure_git_hook(policy: Policy, root: Path, fs: FileSystem, runner: ToolRunner) -> HookOutcome:
    """Install or refresh the hook when the preset asks for one. Never raises for an unusual repository: the outcome says what happened."""
    if not policy.preset.git_hook:
        return HookOutcome("not-strict")
    located = _locate(root, runner)
    if located is None:
        return HookOutcome("no-git")
    path, prefix = located
    wanted = render_hook(prefix)
    if fs.is_file(path):
        try:
            current = fs.read_text(path)
        except (OSError, UnicodeDecodeError):
            return HookOutcome("foreign", path)
        if HOOK_MARKER not in current:
            return HookOutcome("foreign", path)
        if current == wanted:
            return HookOutcome("current", path)
        status: HookStatus = "updated"
    else:
        status = "installed"
    fs.mkdir(path.parent)
    fs.write_text(path, wanted)
    fs.make_executable(path)
    return HookOutcome(status, path)


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
