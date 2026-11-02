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

import libcst as cst
import libcst.matchers as m
from libcst.helpers import get_full_name_for_node
from libcst.metadata import MetadataWrapper, PositionProvider

RenameMap = dict[str, str]

SKIP = {".git", ".venv", "venv", "node_modules", "__pycache__"}


def _names_old(old: frozenset[str], dotted: str) -> bool:
    return any(dotted == name or dotted.startswith(name + ".") for name in old)


def _is_hit(mode: str, old: frozenset[str], node: cst.CSTNode, renames: RenameMap) -> bool:
    toplevel = {new for new in renames.values() if "." not in new}
    if isinstance(node, cst.Import):
        if mode == "forms":
            return any(alias.asname is not None and get_full_name_for_node(alias.name) in toplevel for alias in node.names)
        return mode == "remaining" and any(_names_old(old, get_full_name_for_node(alias.name) or "") for alias in node.names)
    if not isinstance(node, cst.ImportFrom):
        return False
    module = get_full_name_for_node(node.module) if node.module is not None else None
    if node.relative or module is None:
        return False
    if mode == "remaining":
        return _names_old(old, module)
    return _refused(old, module, node) or _aliased_toplevel(renames, module, node)


def _renamed(old: frozenset[str], module: str, alias: cst.ImportAlias) -> bool:
    return f"{module}.{get_full_name_for_node(alias.name)}" in old


def _refused(old: frozenset[str], module: str, node: cst.ImportFrom) -> bool:
    """A renamed module imported together with a name that is not one, from a package that is itself renamed."""
    if isinstance(node.names, cst.ImportStar) or module not in old:
        return False
    flags = [_renamed(old, module, alias) for alias in node.names]
    return any(flags) and not all(flags)


def _aliased_toplevel(renames: RenameMap, module: str, node: cst.ImportFrom) -> bool:
    """``from <old> import <m> as x`` where <m> is renamed to a top-level name: it would become ``import <new> as x``."""
    if isinstance(node.names, cst.ImportStar):
        return False
    return any(
        alias.asname is not None and "." not in (renames.get(f"{module}.{get_full_name_for_node(alias.name)}") or ".")
        for alias in node.names
    )


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
        rel = path.relative_to(root).as_posix()
        try:
            wrapper = MetadataWrapper(cst.parse_module(path.read_bytes()), unsafe_skip_copy=True)
        except (cst.ParserSyntaxError, UnicodeDecodeError) as exc:
            found.append({"file": rel, "line": 0, "text": f"unparsable: {type(exc).__name__}"})
            continue
        positions = wrapper.resolve(PositionProvider)
        for node in m.findall(wrapper.module, m.OneOf(m.ImportFrom(), m.Import())):
            if _is_hit(mode, old, node, renames or {}):
                text = " ".join(cst.Module([]).code_for_node(node).split())
                found.append({"file": rel, "line": positions[node].start.line, "text": text})
    return found


def _expression(dotted: str) -> cst.Name | cst.Attribute:
    node = cst.parse_expression(dotted)
    if not isinstance(node, (cst.Name, cst.Attribute)):
        raise ValueError(f"not a dotted module name: {dotted!r}")
    return node


def _from_import(node: cst.ImportFrom, renames: RenameMap) -> list[cst.BaseSmallStatement]:
    """The statements that replace ``node`` (``node`` itself when it is not the form); see the module docstring."""
    module = get_full_name_for_node(node.module) if node.module is not None else None
    if node.relative or module is None:
        return [node]
    if isinstance(node.names, cst.ImportStar):
        return [node.with_changes(module=_expression(renames[module])) if module in renames else node]
    aliases = list(node.names)
    hits = [renames.get(f"{module}.{get_full_name_for_node(alias.name)}") for alias in aliases]
    if not any(hits) or (module in renames and not all(hits)):
        return [node]  # the second case was refused by the ``forms`` scan before this runs
    targets = [hit for hit in hits if hit is not None]
    parents = {target.rpartition(".")[0] for target in targets}
    same_leaf = all(hit.rpartition(".")[2] == get_full_name_for_node(alias.name) for hit, alias in zip(hits, aliases, strict=True) if hit is not None)
    if len(targets) == len(aliases) and len(parents) == 1 and "" not in parents and same_leaf:
        return [node.with_changes(module=_expression(parents.pop()))]  # names, `as`, parentheses and comments stay
    pieces: list[cst.BaseSmallStatement] = []
    rest: list[cst.ImportAlias] = []
    for alias, hit in zip(aliases, hits, strict=True):
        if hit is None:
            rest.append(alias)
        else:
            pieces.append(_statement(hit, alias, node if len(aliases) == 1 else None))
    if rest:
        rest[-1] = rest[-1].with_changes(comma=cst.MaybeSentinel.DEFAULT)
        pieces.append(node.with_changes(names=rest, semicolon=cst.MaybeSentinel.DEFAULT))
    pieces[-1] = pieces[-1].with_changes(semicolon=node.semicolon)
    return pieces


def _statement(new: str, alias: cst.ImportAlias, whole: cst.ImportFrom | None) -> cst.BaseSmallStatement:
    """The import that binds the same name as ``alias`` did, now to the module called ``new``."""
    bound = alias.asname.name.value if alias.asname is not None and isinstance(alias.asname.name, cst.Name) else str(get_full_name_for_node(alias.name))
    parent, _, leaf = new.rpartition(".")
    imported = cst.ImportAlias(name=cst.Name(leaf), asname=None if bound == leaf else cst.AsName(name=cst.Name(bound)))
    if not parent:
        return cst.Import(names=[imported])
    base = whole if whole is not None else cst.ImportFrom(module=_expression(parent), names=[imported])
    return base.with_changes(module=_expression(parent), names=[imported], semicolon=cst.MaybeSentinel.DEFAULT)


class _FromImports(m.MatcherDecoratableTransformer):
    """Rewrite ``from <old> import <module>`` so that each name keeps its binding."""

    METADATA_DEPENDENCIES = (PositionProvider,)

    def __init__(self, renames: RenameMap) -> None:
        super().__init__()
        self.renames = renames
        self.rewritten: list[tuple[int, cst.ImportFrom]] = []

    @m.leave(m.SimpleStatementLine())
    def rewrite_line(self, original_node: cst.SimpleStatementLine, updated_node: cst.SimpleStatementLine) -> cst.SimpleStatementLine:
        body: list[cst.BaseSmallStatement] = []
        for before, now in zip(original_node.body, updated_node.body, strict=True):
            out = _from_import(now, self.renames) if isinstance(now, cst.ImportFrom) else [now]
            if isinstance(before, cst.ImportFrom) and (len(out) != 1 or out[0] is not now):
                self.rewritten.append((self.get_metadata(PositionProvider, before).start.line, before))
            body += out
        return updated_node.with_changes(body=body)


def rewrite(root: Path, under: list[str], renames: RenameMap) -> tuple[list[dict[str, object]], bool]:
    """Apply the rewrite to every file below ``under``; the second value is whether a file could not be parsed."""
    done: list[dict[str, object]] = []
    failed = False
    for path in _files(root, under):
        try:
            wrapper = MetadataWrapper(cst.parse_module(path.read_bytes()))
        except (cst.ParserSyntaxError, UnicodeDecodeError):
            failed = True
            continue
        transformer = _FromImports(renames)
        changed = wrapper.visit(transformer)
        if not transformer.rewritten:
            continue
        _ = path.write_bytes(changed.bytes)
        rel = path.relative_to(root).as_posix()
        done += [{"file": rel, "line": line, "text": " ".join(cst.Module([]).code_for_node(node).split())} for line, node in transformer.rewritten]
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
