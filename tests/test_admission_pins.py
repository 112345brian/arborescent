"""#161 stage 1, pinning tests written before the change: how every existing caller uses ``fits_ring``, ``suggest_ring`` and the legal ``__arbor__`` keys.

They must pass unchanged on the code before and after admission lands.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from arborescent_helpers import MINI, mini_policy, write_files

from arborescent.adapters import FsScanner
from arborescent.classify_audit import suggest_ring
from arborescent.facts import ModuleInfo, extract
from arborescent.rules import RuleContext, fits_ring

SRC = Path(__file__).resolve().parents[1] / "src" / "arborescent"
HEADER = "from __future__ import annotations\n\n"


def _setup(root: Path, files: dict[str, str]) -> tuple[RuleContext, dict[str, ModuleInfo], list[str]]:
    write_files(root, {"app/__init__.py": "", "app/domain.py": "VALUE: int = 1\n", "app/adapters.py": "SEP: str = '/'\n", **files})
    policy = mini_policy()
    scan = FsScanner().scan(policy, root)
    return RuleContext(policy, scan.arbor_index, scan.scope_by_module), dict(scan.modules), sorted(scan.known)


def _fits(root: Path, source: str, ring: str) -> bool:
    context, modules, known = _setup(root, {"app/mod.py": source})
    return fits_ring(context, "app.mod", modules["app.mod"], ring, known)


def _suggest(root: Path, source: str) -> str:
    context, modules, known = _setup(root, {"app/mod.py": source})
    return suggest_ring(context, "app.mod", modules["app.mod"], known)


# -- fits_ring: current behaviour ---------------------------------------------------------------------------------------------

def test_fits_ring_passes_a_module_whose_imports_are_allowed(tmp_path: Path) -> None:
    assert _fits(tmp_path, HEADER + "from . import domain\n\nX: int = domain.VALUE\n", "ports")


def test_fits_ring_counts_a_function_local_import(tmp_path: Path) -> None:
    source = HEADER + "def f() -> int:\n    from . import adapters\n    return len(adapters.SEP)\n"
    assert not _fits(tmp_path, source, "domain")


def test_fits_ring_counts_a_type_checking_import(tmp_path: Path) -> None:
    source = HEADER + "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    from . import adapters\n"
    assert not _fits(tmp_path, source, "domain")


def test_fits_ring_refuses_an_unresolvable_import(tmp_path: Path) -> None:
    assert not _fits(tmp_path, HEADER + "from .... import nothing\n", "composition")


def test_fits_ring_refuses_a_forbidden_external_and_an_effect(tmp_path: Path) -> None:
    assert not _fits(tmp_path, HEADER + "import os\n\nX = os.sep\n", "domain")
    assert _fits(tmp_path, HEADER + "import os\n\nX = os.sep\n", "adapters")


def test_fits_ring_does_not_follow_a_dynamic_import_edge(tmp_path: Path) -> None:
    """Pinned as the CURRENT gap that admission closes on its own: ``fits_ring`` itself keeps ignoring it."""
    source = HEADER + 'import importlib\n\nM = importlib.import_module("app.adapters")\n'
    assert not _fits(tmp_path, source, "domain")  # `importlib` is not pure, and the effect is what refuses it


# -- suggest_ring: current output ---------------------------------------------------------------------------------------------

def test_suggest_ring_names_the_innermost_fitting_ring_with_evidence(tmp_path: Path) -> None:
    text = _suggest(tmp_path, HEADER + "X: int = 1\n")
    assert text == "tightest ring: domain (0 project import(s), externals ['__future__'], effects none)"


def test_suggest_ring_says_when_no_ring_passes(tmp_path: Path) -> None:
    text = _suggest(tmp_path, HEADER + "from .... import nothing\n")
    assert text.startswith("no ring passes every rule as written (")


# -- callers ------------------------------------------------------------------------------------------------------------------

def _names_used(path: Path, name: str) -> int:
    return sum(1 for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))) if isinstance(node, ast.Name) and node.id == name)


def test_fits_ring_is_called_only_from_classify_audit() -> None:
    users = sorted(p.name for p in SRC.glob("*.py") if p.name != "rules.py" and _names_used(p, "fits_ring"))
    assert users == ["classify_audit.py"]
    assert _names_used(SRC / "classify_audit.py", "fits_ring") >= 2  # the import and the call sites


def test_suggest_ring_is_called_only_from_the_discover_command() -> None:
    users = sorted(p.name for p in SRC.glob("*.py") if p.name != "classify_audit.py" and _names_used(p, "suggest_ring"))
    assert users == ["cli.py"]


# -- the legal __arbor__ keys ------------------------------------------------------------------------------------------------

def test_arbor_ring_and_role_are_legal_and_anything_else_is_an_error() -> None:
    assert extract('__arbor__ = {"ring": "domain"}\n').arbor_error is None
    assert extract('__arbor__ = {"role": "dev_tool"}\n').arbor_role == "dev_tool"
    assert extract('__arbor__ = {"ring": "domain", "role": "dev_tool"}\n').arbor_error is None
    error = extract('__arbor__ = {"ring": "domain", "colour": "red"}\n').arbor_error
    assert error == "__arbor__ has unknown key(s): 'colour'"


@pytest.mark.parametrize("name", ["_ARBOR_LEGAL_KEYS"])
def test_the_legal_key_set_is_read_only_by_the_facts_module(name: str) -> None:
    users = sorted(p.name for p in SRC.glob("*.py") if _names_used(p, name) or name in p.read_text(encoding="utf-8"))
    assert users == ["facts.py"]


def test_the_mini_policy_pins_four_rings_innermost_first() -> None:
    assert [r.name for r in mini_policy().rings] == MINI["rings"]
