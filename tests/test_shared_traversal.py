"""The fact passes share one cached traversal (``import_syntax.child_nodes`` / ``walk_nodes``) instead of ``ast.walk``: it must
visit the same nodes in the same order, leaving out only the context and operator singletons no fact looks for."""

from __future__ import annotations

import ast
from pathlib import Path

from arborescent.import_syntax import child_nodes, walk_nodes

SOURCES = sorted((Path(__file__).resolve().parents[1] / "src" / "arborescent").glob("*.py"))
UNREAD = (ast.expr_context, ast.operator, ast.boolop, ast.unaryop, ast.cmpop)
# Optional and repeated fields that may hold nothing: `**` in a dict, a keyword-only parameter without a default, `match` arms.
EDGES = """
x = {**a, 1: -b}
def f(a, *, b, c=1) -> None:
    global y
    y = a < b <= c and not c
match x:
    case {"k": v, **rest} if v:
        del v
    case C(1, name=_) | None:
        pass
"""


def test_the_shared_walk_visits_what_ast_walk_visits_in_the_same_order() -> None:
    for source in [EDGES, "", *(path.read_text(encoding="utf-8") for path in SOURCES)]:
        tree = ast.parse(source)
        assert walk_nodes(tree) == [node for node in ast.walk(tree) if not isinstance(node, UNREAD)]
        for node in walk_nodes(tree):
            assert list(child_nodes(node)) == [child for child in ast.iter_child_nodes(node) if not isinstance(child, UNREAD)]


def test_the_children_of_a_node_are_worked_out_once() -> None:
    tree = ast.parse("a = b + c")
    assert child_nodes(tree.body[0]) is child_nodes(tree.body[0])
