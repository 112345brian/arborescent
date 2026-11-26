"""Which function-level imports sit inside CLI command bodies (#215). Pure: source text in, line numbers out.

A command is a function decorated by one of the configured decorators (``@app.command()``, ``@click.group``), registered
by calling one (``app.command("x")(run)``), or handed to argparse as ``set_defaults(func=run)`` in the same module.
The imports of such a function are the ones that become module-level imports once each command has its own module.
"""

from __future__ import annotations

import ast
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass

from .coupling_policy import EagerCommands

__all__ = ["COMMAND_BODY_HINT", "EagerShare", "command_bodies", "eager_share"]

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


def _registered_names(nodes: Sequence[ast.AST], decorators: Collection[str]) -> Iterator[str]:
    """Functions registered by a call rather than a decorator."""
    for node in nodes:
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Call) and _registers(node.func, decorators):
            yield from (arg.id for arg in node.args if isinstance(arg, ast.Name))
        if isinstance(node.func, ast.Attribute) and node.func.attr == "set_defaults":
            yield from (keyword.value.id for keyword in node.keywords if isinstance(keyword.value, ast.Name))


def _import_lines(function: ast.FunctionDef | ast.AsyncFunctionDef) -> frozenset[int]:
    return frozenset(
        node.lineno for statement in function.body for node in ast.walk(statement) if isinstance(node, ast.Import | ast.ImportFrom)
    )


def command_bodies(source: str, decorators: Collection[str]) -> tuple[tuple[str, frozenset[int]], ...]:
    """``(function name, import lines)`` per command that imports something; empty when the source does not parse."""
    try:
        tree = ast.parse(source.removeprefix("\ufeff"))
    except (SyntaxError, ValueError):
        return ()
    nodes = list(ast.walk(tree))
    registered = frozenset(_registered_names(nodes, decorators))
    functions = sorted(
        (
            node for node in nodes
            if isinstance(node, _Function)
            and (node.name in registered or any(_registers(item, decorators) for item in node.decorator_list))
        ),
        key=lambda node: node.lineno,
    )
    return tuple((node.name, lines) for node in functions if (lines := _import_lines(node)))


def eager_share(source: str, lazy_lines: Sequence[int], policy: EagerCommands) -> EagerShare | None:
    """The module's share when it passes every threshold, else None. ``lazy_lines`` holds one line per function-level import."""
    if len(lazy_lines) < policy.min_imports:
        return None
    bodies = command_bodies(source, policy.decorators)
    covered = frozenset(line for _, lines in bodies for line in lines)
    inside = [line for line in lazy_lines if line in covered]
    if len(bodies) < policy.min_commands or len(inside) < max(policy.min_imports, policy.min_share * len(lazy_lines)):
        return None
    return EagerShare(tuple(name for name, _ in bodies), frozenset(inside), len(inside), len(lazy_lines))
