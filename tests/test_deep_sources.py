"""Generated code can chain thousands of terms or ``elif`` arms; extracting its facts must not overflow the stack."""

from __future__ import annotations

import ast

import pytest

from arborescent.definition_facts import name_declarations
from arborescent.facts import extract
from arborescent.import_syntax import raw_imports

# deeper than the interpreter's recursion limit, and still shallow enough for `ast.parse` on every supported Python
_DEPTH = 2000
_DEEP = {
    "expression": "x = " + " + ".join(["a"] * _DEPTH),
    "calls in a function": "def f():\n    return " + " + ".join(["g()"] * _DEPTH),
    "elif ladder": "if a:\n    pass\n" + "elif a:\n    pass\n" * _DEPTH,
}


@pytest.mark.parametrize("source", _DEEP.values(), ids=_DEEP.keys())
def test_a_very_deep_tree_is_extracted(source: str) -> None:
    assert extract(source).imports == ()
    _ = name_declarations(ast.parse(source))  # run by the opt-in `name_collisions` over every source


def test_imports_keep_source_order_and_context_through_every_kind_of_block() -> None:
    source = """
import a
try:
    import b
except ImportError:
    import c
match a:
    case 1:
        import d
if TYPE_CHECKING:
    import e
else:
    import f
class K:
    def m(self):
        with a:
            import g
import h
"""
    found = [(item.module, item.lazy, item.type_only) for item in raw_imports(ast.parse(source))]
    assert found == [
        ("a", False, False), ("b", False, False), ("c", False, False), ("d", False, False), ("e", False, True),
        ("f", False, False), ("g", True, False), ("h", False, False),
    ]
