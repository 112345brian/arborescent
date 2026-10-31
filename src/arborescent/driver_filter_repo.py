"""Adapter: git filter-repo behind the migration-library port (#67).

Chosen because it keeps history, takes explicit path selection and renames, reads one repository and writes another
(``--source``/``--target``, so the monorepo is never modified) and installs from PyPI. It does not rewrite imports:
module renames are reported as not applied, never silently dropped. Reached through the ``ToolRunner`` port.
"""

from __future__ import annotations

from .driver_request import MODULE_RENAMES, MigrationOutcome, MigrationRequest
from .ports import FileSystem, ToolRunner

__all__ = ["EXECUTABLE", "REQUIREMENT", "FilterRepo"]

EXECUTABLE = "git-filter-repo"
REQUIREMENT = "git-filter-repo>=2.38"


class FilterRepo:
    name = "git filter-repo"
    executable = EXECUTABLE
    requirement = REQUIREMENT

    def __init__(self, runner: ToolRunner, fs: FileSystem) -> None:
        self._runner = runner
        self._fs = fs

    def available(self) -> bool:
        return self._runner.available(EXECUTABLE) and self._runner.available("git")

    def unsupported(self, request: MigrationRequest) -> tuple[str, ...]:
        return (f"{MODULE_RENAMES} (git filter-repo does not rewrite imports)",) if request.module_renames else ()

    def arguments(self, request: MigrationRequest) -> tuple[str, ...]:
        out: list[str] = [EXECUTABLE, "--source", str(request.source), "--target", str(request.target), "--refs", request.branch]
        for rename in request.path_renames:
            # filter-repo wants both names to be directories (trailing slash) or neither; the root is an empty new name.
            old, new = (rename.source + "/", "") if rename.target == "." else (rename.source, rename.target)
            out += ["--path", rename.source, "--path-rename", f"{old}:{new}"]
        return tuple(out)

    def supply(self, request: MigrationRequest) -> MigrationOutcome:
        if self._fs.exists(request.target):
            return MigrationOutcome(False, f"{request.target} exists; the new repository must not")
        made = self._runner.run(["git", "init", "--quiet", "--initial-branch", request.branch, str(request.target)], request.source)
        if made.returncode != 0:
            return MigrationOutcome(False, f"git init failed: {_message(made.stderr or made.stdout)}")
        done = self._runner.run(list(self.arguments(request)), request.source)
        if done.returncode != 0:
            return MigrationOutcome(False, f"git filter-repo failed: {_message(done.stderr or done.stdout)}")
        return MigrationOutcome(True, f"history of {request.package} written to {request.target}")


def _message(text: str) -> str:
    return text.strip().splitlines()[-1] if text.strip() else "no output"
