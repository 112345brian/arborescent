"""Run as ``python -m arborescent.driver_libcst_scan`` (#67); never imported by the command line, so a missing LibCST cannot break it.

Parses Python with LibCST's parser (never regular expressions) and prints findings as JSON:

- ``forms``: ``from <old> import <module>`` where ``<old>.<module>`` is a module being renamed. LibCST's per-module rename
  can turn that form into code that still parses but names something no longer bound, so it is refused up front.
- ``remaining``: any import that still names an old module, to be run after the rename.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import libcst as cst
import libcst.matchers as m
from libcst.helpers import get_full_name_for_node
from libcst.metadata import MetadataWrapper, PositionProvider

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def _names_old(old: frozenset[str], dotted: str) -> bool:
    return any(dotted == name or dotted.startswith(name + ".") for name in old)


def _is_hit(mode: str, old: frozenset[str], node: cst.CSTNode) -> bool:
    if isinstance(node, cst.Import):
        return mode == "remaining" and any(_names_old(old, get_full_name_for_node(alias.name) or "") for alias in node.names)
    if not isinstance(node, cst.ImportFrom):
        return False
    module = get_full_name_for_node(node.module) if node.module is not None else None
    if node.relative or module is None:
        return False
    if mode == "remaining":
        return _names_old(old, module)
    return not isinstance(node.names, cst.ImportStar) and any(f"{module}.{get_full_name_for_node(alias.name)}" in old for alias in node.names)


def _files(root: Path, under: list[str]) -> list[Path]:
    out: set[Path] = set()
    for item in under:
        path = root / item
        if path.is_file():
            out.add(path)
        else:
            out.update(p for p in path.rglob("*.py") if not SKIP & set(p.relative_to(root).parts))
    return sorted(out)


def scan(mode: str, root: Path, under: list[str], old: frozenset[str]) -> list[dict[str, object]]:
    found: list[dict[str, object]] = []
    for path in _files(root, under):
        rel = path.relative_to(root).as_posix()
        try:
            wrapper = MetadataWrapper(cst.parse_module(path.read_bytes()), unsafe_skip_copy=True)
        except (cst.ParserSyntaxError, UnicodeDecodeError) as exc:
            found.append({"file": rel, "line": 0, "text": f"unparsable: {type(exc).__name__}"})
            continue
        positions = wrapper.resolve(PositionProvider)
        for node in m.findall(wrapper.module, m.OneOf(m.ImportFrom(), m.Import())):
            if _is_hit(mode, old, node):
                text = " ".join(cst.Module([]).code_for_node(node).split())
                found.append({"file": rel, "line": positions[node].start.line, "text": text})
    return found


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 3 or args[0] not in ("forms", "remaining"):
        _ = sys.stderr.write("usage: python -m arborescent.driver_libcst_scan forms|remaining ROOT [--under PATH]... OLD_MODULE...\n")
        return 2
    mode, root, rest = args[0], Path(args[1]), args[2:]
    under: list[str] = []
    while len(rest) >= 2 and rest[0] == "--under":
        under.append(rest[1])
        rest = rest[2:]
    if not rest:
        _ = sys.stderr.write("no old module names given\n")
        return 2
    _ = sys.stdout.write(json.dumps(scan(mode, root, under or ["."], frozenset(rest))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
