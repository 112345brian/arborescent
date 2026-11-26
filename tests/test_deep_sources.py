"""Generated code can chain thousands of terms or ``elif`` arms; extracting its facts must not overflow the stack."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from arborescent.call_facts import dotted_name
from arborescent.cli import main
from arborescent.command_imports import _registers
from arborescent.definition_facts import name_declarations
from arborescent.facts import extract
from arborescent.import_syntax import raw_imports
from arborescent.ratchet_measure import _call_count

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


def test_a_very_long_attribute_chain_is_named_without_recursion() -> None:
    chain = ".".join(["a"] * _DEPTH)
    assert extract(f"import a\n\ndef f():\n    return {chain}()\n").imports[0].module == "a"
    node = ast.parse(chain, mode="eval").body
    assert dotted_name(node, {}) == chain
    assert dotted_name(node, {"a": "pkg.mod"}) == "pkg.mod" + chain[1:]
    assert dotted_name(ast.parse("f()." + chain, mode="eval").body, {}) is None  # not rooted in a name


_CHAIN = ".".join(["os"] * _DEPTH)
_CHAINED = {
    "call returned": f"def f():\n    return {_CHAIN}()\n",
    "name returned": f"def f():\n    return {_CHAIN}\n",
    "base class": f"class A({_CHAIN}):\n    pass\n",
    "class field annotation": f"class A:\n    a: {_CHAIN}\n",
}


@pytest.mark.parametrize("body", _CHAINED.values(), ids=_CHAINED.keys())
def test_check_gets_through_a_very_long_attribute_chain(body: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "m.py").write_text("import os\n\n" + body)
    policy = '[tool.arborescent]\nsources = ["pkg"]\nrings = ["domain"]\n[tool.arborescent.classify]\n"pkg*" = "domain"\n'
    (tmp_path / "pyproject.toml").write_text(policy)
    assert main(["--config", str(tmp_path / "pyproject.toml"), "check", "--no-tools", "--no-cache"]) == 1  # `os` is not allowed
    assert "external-import" in "".join(capsys.readouterr())


def test_the_opt_in_passes_name_a_very_long_chain() -> None:
    assert _call_count(_CHAIN, f"{_CHAIN}()\n") == 1
    assert _registers(ast.parse(f"{_CHAIN}.command()", mode="eval").body, {"command"})
