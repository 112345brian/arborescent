"""The LibCST half of ``driver_libcst_scan`` (#67, #71): pure functions from one file's source to findings or rewritten source.

No file is read or written here; ``driver_libcst_scan`` walks the tree and does that, and is the only importer, so (like
it) this module is only ever loaded in the isolated subprocess and a missing LibCST cannot break the command line. See
``driver_libcst_scan``'s docstring for what each mode finds and why.
"""

from __future__ import annotations

import libcst as cst
import libcst.matchers as m
from libcst.helpers import get_full_name_for_node
from libcst.metadata import MetadataWrapper, PositionProvider

__all__ = ["RenameMap", "findings", "rewrite_source"]

RenameMap = dict[str, str]


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


def findings(mode: str, rel: str, source: bytes, old: frozenset[str], renames: RenameMap) -> list[dict[str, object]]:
    """The ``forms`` or ``remaining`` hits in one file; an unparsable file is itself one finding."""
    try:
        wrapper = MetadataWrapper(cst.parse_module(source), unsafe_skip_copy=True)
    except (cst.ParserSyntaxError, UnicodeDecodeError) as exc:
        return [{"file": rel, "line": 0, "text": f"unparsable: {type(exc).__name__}"}]
    positions = wrapper.resolve(PositionProvider)
    return [
        {"file": rel, "line": positions[node].start.line, "text": " ".join(cst.Module([]).code_for_node(node).split())}
        for node in m.findall(wrapper.module, m.OneOf(m.ImportFrom(), m.Import()))
        if _is_hit(mode, old, node, renames)
    ]


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


def rewrite_source(rel: str, source: bytes, renames: RenameMap) -> tuple[bytes, list[dict[str, object]]] | None:
    """One file's rewritten source and the statements rewritten (none: leave the file alone); None when it does not parse."""
    try:
        wrapper = MetadataWrapper(cst.parse_module(source))
    except (cst.ParserSyntaxError, UnicodeDecodeError):
        return None
    transformer = _FromImports(renames)
    changed = wrapper.visit(transformer)
    done: list[dict[str, object]] = [
        {"file": rel, "line": line, "text": " ".join(cst.Module([]).code_for_node(node).split())} for line, node in transformer.rewritten
    ]
    return changed.bytes, done
