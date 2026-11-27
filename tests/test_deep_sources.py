"""Generated code can chain thousands of terms or ``elif`` arms; extracting its facts must not overflow the stack."""

from __future__ import annotations

import ast
from itertools import pairwise
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.call_facts import dotted_name
from arborescent.cli import main
from arborescent.command_imports import _registers
from arborescent.definition_facts import name_declarations
from arborescent.facts import extract
from arborescent.import_syntax import raw_imports
from arborescent.practices import function_references
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


# Constructs that still recursed in the fact passes: each value holds a use that must survive the long chain around it.
_NONES = " | ".join(["None"] * _DEPTH)
_PATHS = "".join(["/'b'"] * _DEPTH)
_RECURSED = {
    "path division": f"from pathlib import Path\np = Path('a'){_PATHS}\np.read_text()\n",
    "path methods": "from pathlib import Path\np = Path('a')" + ".resolve().parent" * (_DEPTH // 3) + "\np.read_text()\n",
    "union parameter": f"from pathlib import Path\ndef f(p: {_NONES} | Path):\n    p.read_text()\n",
    "union string": f"from pathlib import Path\ndef f(p: '{_NONES} | Path'):\n    p.read_text()\n",
}


@pytest.mark.parametrize("source", _RECURSED.values(), ids=_RECURSED.keys())
def test_a_use_behind_a_very_long_chain_is_still_found(source: str) -> None:
    assert "pathlib.Path.read_text" in {use.target for use in extract(source).effect_uses}


_UNION = " | ".join(["int"] * _DEPTH)
_SUBSCRIPTS = "int" + "[0]" * _DEPTH
_TOO_DEEP = "**".join(["a"] * 20000)
_UNCRASHED = {
    "return union": f"def f() -> {_UNION}:\n    return 1\n",
    "return subscripts": f"def f() -> {_SUBSCRIPTS}:\n    return 1\n",
    "parameter subscripts": f"def f(a: {_SUBSCRIPTS}):\n    a.save()\n",
    "field union": f"class C:\n    x: {_UNION}\n    def m(self):\n        return self.x\n",
    "protocol return": f"from typing import Protocol\nclass P(Protocol):\n    def m(self) -> {_UNION}: ...\n",
    "seam union": f"import time\ndef f(now: {_UNION} = time.time):\n    return now()\n",
    "seam subscripts": f"import time\ndef f(now: {_SUBSCRIPTS} = time.time):\n    return now()\n",
    "negated test around a seam": "import time\ndef f(now=None):\n    if " + "not " * _DEPTH + "now:\n        time.time()\n",
    "elif ladder at module level": "a = 1\nif a:\n    pass\n" + "elif a:\n    def f():\n        print()\n" * _DEPTH,
    "elif ladder in a class": "class C:\n    if 1:\n        pass\n" + "    elif 1:\n        def f(self):\n            print()\n" * _DEPTH,
    # parses as a string, but is too deep for the parser when it is read again as an annotation
    "unparsable string annotations": f"from pathlib import Path\ndef f(p: '{_TOO_DEEP}') -> '{_TOO_DEEP}':\n    return p\n"
    f"class C:\n    x: '{_TOO_DEEP}'\n",
}


@pytest.mark.parametrize("source", _UNCRASHED.values(), ids=_UNCRASHED.keys())
def test_deep_annotations_tests_and_ladders_are_extracted(source: str) -> None:
    _ = extract(source)
    _ = name_declarations(ast.parse(source))


def test_the_walks_keep_their_order_and_first_match() -> None:
    """The explicit stacks that replaced recursion keep source order."""
    facts = extract(
        "from typing import Optional, Annotated, Protocol\n"
        "def a() -> Optional['FirstResult | int']: ...\n"
        "def b() -> list[SecondResult] | m.ThirdResult: ...\n"
        "class P(Protocol):\n    def m(self) -> int | Optional[Annotated[dict, 'x']] | list: ...\n"
    )
    assert facts.result_defs == ("a", "b")
    assert [why for _, _, why in facts.weak_port_returns] == ["returns a bare dict"]
    ladder = "if x:\n    def a(): print()\nelif y:\n    def b(): print()\nelse:\n    def c(): print()\ndef d(): print()\n"
    assert [name for name, _, _ in extract(ladder).import_time.function_effects] == ["a", "b", "c", "d"]


_LINKS = 1200  # modules in one chain: more than the interpreter's recursion limit


@pytest.mark.parametrize(("link", "expected"), [
    ("from pkg import {then}\n", "effect-via-import"),
    ("from pkg.{then} import thing as inner\n\ndef thing(a):\n    return inner(a)\n", "forwarded-import"),
], ids=["imports", "forwarders"])
def test_check_gets_through_a_very_long_chain_of_modules(
    link: str, expected: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    names = [f"m{index:05d}" for index in range(_LINKS)] + ["last"]
    files = {f"pkg/{here}.py": link.format(then=then) for here, then in pairwise(names)}
    files["pkg/__init__.py"] = ""
    files["pkg/last.py"] = "import os\n\ndef thing(a):\n    os.system(a)\n"
    files["pyproject.toml"] = (
        '[tool.arborescent]\nsources = ["pkg"]\nrings = ["domain"]\n[tool.arborescent.ring.domain]\nexternal = ["os"]\n'
        'forbid = ["process"]\n[tool.arborescent.classify]\n"pkg*" = "domain"\n'
        '[tool.arborescent.module_forbid_imports]\n"pkg.m00000" = ["pkg.last"]\n'
    )
    write_files(tmp_path, files)
    assert main(["--config", str(tmp_path / "pyproject.toml"), "check", "--no-tools", "--no-cache"]) == 1
    assert expected in "".join(capsys.readouterr())


def test_a_test_reference_follows_a_very_long_chain_of_re_exports(tmp_path: Path) -> None:
    names = [f"m{index:05d}" for index in range(_LINKS)] + ["last"]
    files = {f"pkg/{here}.py": f"from pkg.{then} import thing\n" for here, then in pairwise(names)}
    files |= {"pkg/__init__.py": "", "pkg/last.py": "def thing(a):\n    return a\n"}
    files["tests/test_a.py"] = "from pkg.m00000 import thing\n\ndef test_a():\n    thing(1)\n"
    write_files(tmp_path, files)
    scan = FsScanner().scan(mini_policy(sources=["pkg"]), tmp_path)
    found = function_references(LocalFileSystem(), tmp_path, "tests", scan)
    assert [ref.references for ref in found] == [frozenset({("pkg.last", "thing")})]


@pytest.mark.parametrize("deep", [f"p = Path('a'){_PATHS}\n", f"def g(p: {_UNION}):\n    pass\n"], ids=["path", "union"])
def test_check_reports_an_effect_in_a_module_that_also_holds_a_deep_chain(
    deep: str, tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "__init__.py").write_text("")
    (tmp_path / "pkg" / "m.py").write_text("from pathlib import Path\nimport os\n\nos.system('x')\n" + deep)
    policy = (
        '[tool.arborescent]\nsources = ["pkg"]\nrings = ["domain"]\n[tool.arborescent.ring.domain]\n'
        'external = ["os", "pathlib"]\nforbid = ["process"]\n[tool.arborescent.classify]\n"pkg*" = "domain"\n'
    )
    (tmp_path / "pyproject.toml").write_text(policy)
    assert main(["--config", str(tmp_path / "pyproject.toml"), "check", "--no-tools", "--no-cache"]) == 1
    assert "effect-process" in "".join(capsys.readouterr())
