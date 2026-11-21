"""Run as ``python -m arborescent.driver_libcst_scan`` (#67); never imported by the command line, so a missing LibCST cannot break it.

Parses Python with LibCST's parser (never regular expressions) and prints findings as JSON:

- ``forms``: what ``rewrite`` cannot do provably correctly, to be run before anything is touched: a file that does not parse,
  and a ``from <old> import a, b`` that names a renamed module together with a name that is not one while ``<old>`` is itself
  renamed (that name could be an attribute of the package or a module the map does not know), and an ``import <new> as x``
  (existing, or what ``rewrite`` would write) where ``<new>`` is the top-level name a module is renamed to: LibCST's rename
  then adds a second ``import <new>`` or drops the alias, which changes what is bound.
- ``rewrite``: ``from <old> import <module>`` where ``<old>.<module>`` is a module being renamed (#71), and ``from <old> import *``
  where ``<old>`` is. LibCST's per-module rename turns the first into code that still parses but names something no longer
  bound, and leaves the second alone, so the adapter rewrites them itself. Every name the statement bound stays bound to the
  same module under the same name, so no use is rewritten. Prints the statements it rewrote.
- ``remaining``: any import that still names an old module, to be run after the rename.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .driver_libcst_forms import RenameMap, findings, rewrite_source

__all__ = ["main", "rewrite", "scan"]

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def _read(path: Path) -> bytes:
    return path.read_bytes()


def _write(path: Path, data: bytes) -> None:
    _ = path.write_bytes(data)


def _files(root: Path, under: list[str]) -> list[Path]:
    out: set[Path] = set()
    for item in under:
        path = root / item
        if path.is_file():
            out.add(path)
        else:
            out.update(p for p in path.rglob("*.py") if not SKIP & set(p.relative_to(root).parts))
    return sorted(out)


def scan(mode: str, root: Path, under: list[str], old: frozenset[str], renames: RenameMap | None = None) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in _files(root, under):
        found += findings(mode, path.relative_to(root).as_posix(), _read(path), old, renames or {})
    return found


def rewrite(root: Path, under: list[str], renames: RenameMap) -> tuple[list[dict[str, object]], bool]:
    """Apply the rewrite to every file below ``under``; the second value is whether a file could not be parsed."""
    done: list[dict[str, object]] = []
    failed = False
    for path in _files(root, under):
        result = rewrite_source(path.relative_to(root).as_posix(), _read(path), renames)
        if result is None:
            failed = True
            continue
        changed, rewritten = result
        if rewritten:
            _write(path, changed)
            done += rewritten
    return done, failed


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] not in ("forms", "remaining", "rewrite"):
        _ = sys.stderr.write("usage: python -m arborescent.driver_libcst_scan forms ROOT [--under PATH]... OLD_MODULE=NEW_MODULE...\n       python -m arborescent.driver_libcst_scan remaining ROOT [--under PATH]... OLD_MODULE...\n"
                             "       python -m arborescent.driver_libcst_scan rewrite ROOT [--under PATH]... OLD_MODULE=NEW_MODULE...\n")
        return 2
    mode, root, rest = args[0], Path(args[1]), args[2:]
    under: list[str] = []
    while len(rest) >= 2 and rest[0] == "--under":
        under.append(rest[1])
        rest = rest[2:]
    if not rest:
        _ = sys.stderr.write("no old module names given\n")
        return 2
    if mode == "rewrite":
        renames = {old: new for old, sep, new in (item.partition("=") for item in rest) if sep}
        done, failed = rewrite(root, under or ["."], renames)
        _ = sys.stdout.write(json.dumps(done))
        return 1 if failed or len(renames) != len(rest) else 0
    pairs = {old: new for old, _, new in (item.partition("=") for item in rest)}  # forms: OLD=NEW; remaining: OLD
    _ = sys.stdout.write(json.dumps(scan(mode, root, under or ["."], frozenset(pairs), {k: v for k, v in pairs.items() if v})))
    return 0


if __name__ == "__main__":
    sys.exit(main())
