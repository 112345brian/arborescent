"""Pass-through functions (#138): ``def f(a, b): return other.f(a, b)`` with nothing added.

A module that is forbidden from importing ``other`` can "decouple" by importing this forwarder instead: the same call, the same
arguments, one hop further away. The detection is purely syntactic. A function is a forwarder only when its whole body (past a
docstring) is one call to an imported name, the arguments are exactly its parameters in order, and nothing else happens: a
default value, a decorator, a conversion, a rename or a second statement makes it a real boundary.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

__all__ = ["Forwarder", "forwarders"]


@dataclass(frozen=True)
class Forwarder:
    """Module-level function ``name`` that only calls ``member`` of an imported module.

    ``(level, module, imported)`` rebuild the import that binds the callee (``imported`` is ``""`` for ``import a.b``)."""

    name: str
    line: int
    level: int
    module: str
    imported: str
    member: str


# local name -> (level, module, imported name, or "" for a plain ``import``; whether the binding is an alias that already names the module)
_Binding = tuple[int, str, str, bool]


def _bindings(tree: ast.Module) -> dict[str, _Binding]:
    out: dict[str, _Binding] = {}
    for node in tree.body:
        if isinstance(node, ast.Import):
            for item in node.names:
                if item.asname is not None:
                    out[item.asname] = (0, item.name, "", True)
                else:
                    root = item.name.split(".")[0]
                    out[root] = (0, root, "", False)
        elif isinstance(node, ast.ImportFrom) and node.module != "__future__":
            for item in node.names:
                if item.name != "*":
                    out[item.asname or item.name] = (node.level, node.module or "", item.name, True)
    return out


def _path(node: ast.expr) -> list[str] | None:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        parent = _path(node.value)
        return None if parent is None else [*parent, node.attr]
    return None


def _parameters(args: ast.arguments) -> tuple[list[str], str | None, list[str], str | None] | None:
    """(positional names, *args name, keyword-only names, **kwargs name); None when any parameter has a default (a default is added logic)."""
    if args.defaults or any(default is not None for default in args.kw_defaults):
        return None
    positional = [a.arg for a in (*args.posonlyargs, *args.args)]
    return positional, args.vararg.arg if args.vararg is not None else None, [a.arg for a in args.kwonlyargs], args.kwarg.arg if args.kwarg is not None else None


def _passes_through(call: ast.Call, args: ast.arguments) -> bool:
    """True when the call's arguments are exactly the parameters, same order and names."""
    params = _parameters(args)
    if params is None:
        return False
    positional, vararg, kwonly, kwarg = params
    expected_args: list[str] = [*positional, *(["*" + vararg] if vararg is not None else [])]
    actual_args = [f"*{a.value.id}" if isinstance(a, ast.Starred) and isinstance(a.value, ast.Name) else a.id if isinstance(a, ast.Name) else "?" for a in call.args]
    if actual_args != expected_args:
        return False
    # keyword-only parameters are passed by their own name; ``**kwargs`` is passed as ``**kwargs``
    passed: list[str] = []
    for kw in call.keywords:
        if kw.arg is None:
            passed.append("**" + kw.value.id if isinstance(kw.value, ast.Name) else "?")
        elif isinstance(kw.value, ast.Name) and kw.value.id == kw.arg:
            passed.append(kw.arg)
        else:
            return False
    return passed == [*kwonly, *(["**" + kwarg] if kwarg is not None else [])]


def _forwarded_call(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Call | None:
    body = fn.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) != 1 or fn.decorator_list:
        return None
    stmt = body[0]
    value = stmt.value if isinstance(stmt, (ast.Return, ast.Expr)) else None
    if isinstance(value, ast.Await):
        value = value.value
    return value if isinstance(value, ast.Call) else None


def forwarders(tree: ast.Module) -> tuple[Forwarder, ...]:
    bindings = _bindings(tree)
    out: list[Forwarder] = []
    for fn in tree.body:
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        call = _forwarded_call(fn)
        path = _path(call.func) if call is not None else None
        if call is None or path is None or path[0] not in bindings or not _passes_through(call, fn.args):
            continue
        params = {a.arg for a in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)}
        params |= {a.arg for a in (fn.args.vararg, fn.args.kwarg) if a is not None}
        if path[0] in params:  # the parameter shadows the import
            continue
        level, module, imported, _ = bindings[path[0]]
        rest = path[1:]
        if imported:  # ``from m import x``: ``x(...)`` calls x itself, ``x.y(...)`` calls a member of x
            out.append(Forwarder(fn.name, fn.lineno, level, module, imported, ".".join(rest)))
        elif rest:  # ``import a.b [as c]``: everything before the last attribute is the module
            out.append(Forwarder(fn.name, fn.lineno, 0, ".".join([module, *rest[:-1]]), "", rest[-1]))
    return tuple(out)
