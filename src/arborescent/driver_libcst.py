"""Adapter: LibCST's rename codemod behind the ``ImportRewriter`` port (#67).

One ``RenameCommand`` run per renamed module. That command is LibCST's, not ours; this adapter only chooses its inputs and
refuses what it cannot do safely. Reached through the ``ToolRunner`` port and an isolated (``-I``) subprocess, so a module planted in the tree cannot shadow one; so importing this module never
imports LibCST (the extra is optional).

Rewritten (checked on real code): ``import M``, ``import M as a``, ``from M import name``, ``from M import *``. Refused
before any file is touched: ``from <old> import <module>``, because the output of LibCST's rename can then still parse
while naming something no longer bound. Checked afterwards: any import that still names an old module is a failure.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .driver_request import MigrationOutcome, RewriteFinding, RewriteRequest
from .ports import ToolRunner

__all__ = ["REQUIREMENT", "LibCstRename"]

REQUIREMENT = "libcst>=1.0"
_RENAME = "libcst.codemod.commands.rename.RenameCommand"
_SCAN = "arborescent.driver_libcst_scan"


class LibCstRename:
    name = "LibCST rename"
    executable = "libcst"
    requirement = REQUIREMENT

    def __init__(self, runner: ToolRunner) -> None:
        self._runner = runner

    def available(self) -> bool:
        return self._runner.run([sys.executable, "-I", "-c", "import libcst"], Path()).returncode == 0

    def unsupported(self, request: RewriteRequest) -> tuple[RewriteFinding, ...]:
        return self._scan("forms", request)

    def commands(self, request: RewriteRequest) -> tuple[tuple[str, ...], ...]:
        paths = [str(request.root / item) for item in request.paths]
        return tuple(
            (sys.executable, "-I", "-m", "libcst.tool", "codemod", "--external", "--hide-progress", _RENAME, *paths,
             "--old_name", rename.source, "--new_name", rename.target)
            for rename in request.renames
        )

    def apply(self, request: RewriteRequest) -> MigrationOutcome:
        refused = self.unsupported(request)
        if refused:
            return MigrationOutcome(
                False,
                f"refused: {len(refused)} import statement(s) LibCST's rename cannot rewrite safely; no file was modified:\n{_listing(refused)}",
            )
        for command in self.commands(request):
            done = self._runner.run(list(command), request.root)
            failed = re.search(r"Failed to codemod (\d+)", done.stdout + done.stderr)
            if done.returncode != 0 or (failed is not None and int(failed.group(1)) > 0):
                old = command[command.index("--old_name") + 1]
                files = [_relative(name, request.root) for name in re.findall(r"^Failed to codemod (\S.*)$", done.stdout + done.stderr, re.MULTILINE)]
                where = f" in {', '.join(files)}" if files else ""
                return MigrationOutcome(False, f"LibCST failed renaming {old}{where} ({_cause(done.stdout + done.stderr)}); files renamed so far stay changed")
        left = self._scan("remaining", request)
        if left:
            return MigrationOutcome(False, f"{len(left)} import(s) naming an old module were left unrewritten (the files already changed stay changed):\n{_listing(left)}")
        return MigrationOutcome(True, f"{len(request.renames)} module rename(s) applied below {request.root}")

    def _scan(self, mode: str, request: RewriteRequest) -> tuple[RewriteFinding, ...]:
        argv = [sys.executable, "-I", "-m", _SCAN, mode, str(request.root)]
        for item in request.paths:
            argv += ["--under", item]
        argv += sorted(rename.source for rename in request.renames)
        done = self._runner.run(argv, request.root)
        try:
            if done.returncode != 0:
                raise ValueError(done.stderr.strip() or "the scanner failed")
            items = json.loads(done.stdout)
            return tuple(RewriteFinding(str(item["file"]), int(item["line"]), str(item["text"])) for item in items)
        except (ValueError, KeyError, TypeError) as exc:
            return (RewriteFinding("", 0, f"could not check the import statements ({exc}); nothing was rewritten"),)  # fail closed


def _cause(output: str) -> str:
    """The last lines LibCST printed before it named the failing file: the exception, which its summary line leaves out."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    marker = next((i for i, line in enumerate(lines) if line.startswith("Failed to codemod ")), len(lines))
    return " | ".join(lines[max(0, marker - 4):marker]) or "no output"


def _relative(name: str, root: Path) -> str:
    try:
        return Path(name).relative_to(root).as_posix()
    except ValueError:
        return name


def _listing(findings: tuple[RewriteFinding, ...]) -> str:
    return "\n".join(f"  {item.file}:{item.line}: {item.text}" if item.file else f"  {item.text}" for item in findings)
