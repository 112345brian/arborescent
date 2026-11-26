"""Which function-level imports sit inside CLI command bodies (#215). Pure: source text in, line numbers out.

A command is a function decorated by one of the configured decorators (``@app.command()``, ``@click.group``), registered
by calling one (``app.command("x")(run)``), or handed to argparse as ``set_defaults(func=run)``, in its own module or in
one that imports it. The imports of such a function are the ones that become module-level imports once each command has
its own module. ``command_references`` says what each command reaches, for the heavy-import report (#216).
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass

from .coupling_policy import EagerCommands

__all__ = [
    "COMMAND_BODY_HINT", "EagerShare", "command_bodies", "command_references", "eager_share", "registered_targets",
    "registrations_by_module",
]

# Marks a `lazy-import` finding in a command body of a reported module: the report prints those as one line per module.
COMMAND_BODY_HINT = " [command body: see entrypoint-eager-commands]"

_Function = ast.FunctionDef | ast.AsyncFunctionDef


@dataclass(frozen=True)
class EagerShare:
    """How much of one module's function-level importing happens inside command bodies."""

    commands: tuple[str, ...]  # command functions that import something, in source order
    lines: frozenset[int]  # lines of the imports inside them
    inside: int
    total: int


def _dotted(node: ast.expr) -> str:
    attributes: list[str] = []
    while isinstance(node, ast.Call | ast.Attribute):
        if isinstance(node, ast.Attribute):
            attributes.append(node.attr)
            node = node.value
        else:
            node = node.func
    return ".".join([node.id if isinstance(node, ast.Name) else "", *reversed(attributes)])


def _registers(node: ast.expr, decorators: Collection[str]) -> bool:
    name = _dotted(node)
    return name in decorators or name.rsplit(".", 1)[-1] in decorators


def _registered_values(nodes: Sequence[ast.AST], decorators: Collection[str]) -> Iterator[ast.expr]:
    """The expressions registered by a call rather than a decorator."""
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Call) and _registers(node.func, decorators):
            yield from node.args
        if isinstance(node.func, ast.Attribute) and node.func.attr == "set_defaults":
            yield from (keyword.value for keyword in node.keywords)


def _parsed(source: str) -> ast.Module | None:
    try:
        return ast.parse(source.removeprefix("\ufeff"))
    except (SyntaxError, ValueError, RecursionError):
        return None


def _imported(nodes: Sequence[ast.AST], module: str, is_package: bool) -> dict[str, str]:
    """Local name -> the dotted name it imports, relative imports resolved; a function-level import counts too."""
    package = module.split(".") if is_package else module.split(".")[:-1]
    found: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for item in node.names:
                top = item.name.split(".")[0]
                found[item.asname or top] = item.name if item.asname is not None else top
        elif isinstance(node, ast.ImportFrom) and node.level <= len(package) + 1:
            base = package[: len(package) - (node.level - 1)] if node.level else []
            base += node.module.split(".") if node.module is not None else []
            for item in node.names:
                if item.name != "*":
                    found[item.asname or item.name] = ".".join([*base, item.name])
    return found


def _reached(node: ast.expr, imported: Mapping[str, str]) -> str | None:
    """``a.b.c`` through an imported root name as its full dotted name, else None."""
    attributes: list[str] = []
    while isinstance(node, ast.Attribute):
        attributes.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name) or node.id not in imported:
        return None
    return ".".join([imported[node.id], *reversed(attributes)])


def registered_targets(source: str, module: str, is_package: bool, decorators: Collection[str]) -> frozenset[str]:
    """Dotted names of the imported functions this module registers as commands (``set_defaults(func=handlers.run)``)."""
    tree = _parsed(source)
    if tree is None:
        return frozenset[str]()
    nodes = list(ast.walk(tree))
    imported = _imported(nodes, module, is_package)
    return frozenset(target for value in _registered_values(nodes, decorators) if (target := _reached(value, imported)) is not None)


def registrations_by_module(
    modules: Mapping[str, tuple[str, bool]], read: Callable[[str], str | None], decorators: Collection[str]
) -> dict[str, frozenset[str]]:
    """Module -> its function names that any module of ``modules`` (name -> (path, is_package)) registers through an import."""
    markers = ("set_defaults", *(name.rsplit(".", 1)[-1] for name in decorators))
    found: dict[str, set[str]] = {}
    for name, (path, is_package) in modules.items():
        source = read(path)
        if source is None or not any(marker in source for marker in markers):
            continue
        for target in registered_targets(source, name, is_package, decorators):
            owner, _, function = target.rpartition(".")
            if owner in modules:
                found.setdefault(owner, set()).add(function)
    return {owner: frozenset(functions) for owner, functions in found.items()}


def _import_lines(function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[int]:
    return frozenset(
        node.lineno for statement in function.body for node in ast.walk(statement) if isinstance(node, ast.Import | ast.ImportFrom)
    )


def _commands(tree: ast.Module, decorators: Collection[str], elsewhere: Collection[str]) -> list[_Function]:
    """The command functions, in source order. ``elsewhere`` names module-level functions another module registers."""
    nodes = list(ast.walk(tree))
    registered = {value.id for value in _registered_values(nodes, decorators) if isinstance(value, ast.Name)}
    outer = {id(node) for node in tree.body if isinstance(node, _Function) and node.name in elsewhere}
    return sorted(
        (
            node for node in nodes
            if isinstance(node, _Function)
            and (node.name in registered or id(node) in outer or any(_registers(item, decorators) for item in node.decorator_list))
        ),
        key=lambda node: node.lineno,
    )


def command_bodies(
    source: str, decorators: Collection[str], elsewhere: Collection[str] = ()
) -> tuple[tuple[str, frozenset[int]], ...]:
    """``(function name, import lines)`` per command that imports something; empty when the source does not parse."""
    tree = _parsed(source)
    if tree is None:
        return ()
    return tuple((node.name, lines) for node in _commands(tree, decorators, elsewhere) if (lines := _import_lines(node)))


def command_references(
    source: str, module: str, is_package: bool, decorators: Collection[str], elsewhere: Collection[str] = ()
) -> tuple[tuple[str, frozenset[str]], ...]:
    """``(function name, dotted names)`` per command: what its body imports, and what it reaches through the module's imports.

    A name imported at module level counts only where the body reads it. Empty when the source does not parse."""
    tree = _parsed(source)
    if tree is None:
        return ()
    top = _imported([node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)], module, is_package)
    found: list[tuple[str, frozenset[str]]] = []
    for command in _commands(tree, decorators, elsewhere):
        nodes = [node for statement in command.body for node in ast.walk(statement)]
        local = _imported(nodes, module, is_package)
        imported = {**top, **local}
        reached = {target for node in nodes if isinstance(node, ast.Name | ast.Attribute) and (target := _reached(node, imported)) is not None}
        found.append((command.name, frozenset(reached | set(local.values()))))
    return tuple(found)


def eager_share(source: str, lazy_lines: Sequence[int], policy: EagerCommands, elsewhere: Collection[str] = ()) -> EagerShare | None:
    """The module's share when it passes every threshold, else None. ``lazy_lines`` holds one line per function-level import."""
    if len(lazy_lines) < policy.min_imports:
        return None
    bodies = command_bodies(source, policy.decorators, elsewhere)
    covered = frozenset(line for _, lines in bodies for line in lines)
    inside = [line for line in lazy_lines if line in covered]
    if len(bodies) < policy.min_commands or len(inside) < max(policy.min_imports, policy.min_share * len(lazy_lines)):
        return None
    return EagerShare(tuple(name for name, _ in bodies), frozenset(inside), len(inside), len(lazy_lines))
