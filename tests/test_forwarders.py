"""Forwarding-function laundering (#138): a pass-through wrapper must not satisfy `module_forbid_imports`."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from arborescent_helpers import write_files

from arborescent.adapters import FsScanner
from arborescent.config import policy_from_mapping
from arborescent.domain import Rule, Violation
from arborescent.forwarder_facts import forwarders
from arborescent.rules import RuleContext, check_modules


def _names(source: str) -> list[str]:
    return [f.name for f in forwarders(ast.parse(source))]


HEAD = "from . import core\nimport os.path as osp\n"


@pytest.mark.parametrize("body", [
    "def f(a, b):\n    return core.f(a, b)\n",
    "def f(a, *args, k, **kw):\n    return core.f(a, *args, k=k, **kw)\n",
    "def f(a):\n    '''doc'''\n    core.f(a)\n",
    "async def f(a):\n    return await core.f(a)\n",
    "def f():\n    return core.f()\n",
])
def test_exact_pass_through_is_a_forwarder(body: str) -> None:
    assert _names(HEAD + body) == ["f"]


@pytest.mark.parametrize("body", [
    "def f(a, b):\n    return core.f(b, a)\n",  # reordered
    "def f(a, b=1):\n    return core.f(a, b)\n",  # adds a default
    "def f(a):\n    return core.f(a, 1)\n",  # extra argument
    "def f(a):\n    return core.f(str(a))\n",  # conversion
    "def f(a):\n    return core.f(x=a)\n",  # renamed keyword
    "def f(a):\n    try:\n        return core.f(a)\n    except ValueError:\n        return None\n",  # translates an exception
    "def f(a):\n    x = 1\n    return core.f(a)\n",  # more than one statement
    "@cache\ndef f(a):\n    return core.f(a)\n",  # decorated
    "def f(core):\n    return core.f()\n",  # parameter shadows the import
    "def f(a):\n    return local(a)\n",  # callee is not imported
    "def f(a):\n    return core.f(a).g()\n",  # work after the call
    "def f(a):\n    return core.f(*a)\n",  # starred non-parameter
    "def f(a, *, k):\n    return core.f(a)\n",  # drops a keyword-only parameter
    "",
    "def f(a):\n    pass\n",
])
def test_added_logic_is_not_a_forwarder(body: str) -> None:
    assert _names(HEAD + body) == []


def test_forwarder_records_the_imported_callee() -> None:
    found = forwarders(ast.parse("import os.path as osp\ndef f(a):\n    return osp.join(a)\n"))
    assert [(w.module, w.imported, w.member) for w in found] == [("os.path", "", "join")]
    found = forwarders(ast.parse("from ..pkg import fn\ndef f(a):\n    return fn(a)\n"))
    assert [(w.level, w.module, w.imported, w.member) for w in found] == [(2, "pkg", "fn", "")]


def _check(root: Path, files: dict[str, str]) -> list[Violation]:
    write_files(root, files)
    policy = policy_from_mapping({
        "sources": ["app"], "rings": ["domain"], "classify": {"app*": "domain"},
        "module_forbid_imports": {"app.web": ["app.core"]},
    })
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, RuleContext(policy, scan.arbor_index, scan.scope_by_module))


BASE = {"app/__init__.py": "", "app/core.py": "def f(a):\n    return a\n"}


def _forwarded(found: list[Violation]) -> list[Violation]:
    return [v for v in found if v.rule == Rule.FORWARDED_IMPORT]


def test_importing_a_forwarder_of_a_forbidden_module_is_reported(tmp_path: Path) -> None:
    found = _check(tmp_path, {
        **BASE,
        "app/glue.py": "from . import core\n\ndef f(a):\n    return core.f(a)\n",
        "app/web.py": "from .glue import f\n",
    })
    [v] = _forwarded(found)
    assert (v.subject, v.target, v.line) == ("app.web", "app.core", 1)
    assert "forwards to app.core" in v.detail


def test_a_forwarder_chain_is_followed(tmp_path: Path) -> None:
    found = _check(tmp_path, {
        **BASE,
        "app/glue.py": "from . import core\n\ndef f(a):\n    return core.f(a)\n",
        "app/glue2.py": "from . import glue\n\ndef g(a):\n    return glue.f(a)\n",
        "app/web.py": "from .glue2 import g\n",
    })
    assert [(v.subject, v.target) for v in _forwarded(found)] == [("app.web", "app.core")]


def test_a_forwarder_cycle_terminates(tmp_path: Path) -> None:
    found = _check(tmp_path, {
        **BASE,
        "app/a.py": "from . import b\n\ndef f(x):\n    return b.f(x)\n",
        "app/b.py": "from . import a\n\ndef f(x):\n    return a.f(x)\n",
        "app/web.py": "from .a import f\n",
    })
    assert _forwarded(found) == []


@pytest.mark.parametrize("glue", [
    "from . import core\n\ndef f(a, b=None):\n    return core.f(a, b)\n",  # adds a default: a real boundary
    "from . import core\n\ndef f(a):\n    return core.f(a) or 0\n",
    "from . import core\n\ndef other(a):\n    return core.f(a)\n\ndef f(a):\n    return a\n",  # a different name
    "def f(a):\n    return a\n",
])
def test_real_boundaries_are_not_reported(tmp_path: Path, glue: str) -> None:
    found = _check(tmp_path, {**BASE, "app/glue.py": glue, "app/web.py": "from .glue import f\n"})
    assert _forwarded(found) == []


def test_unrelated_importer_is_not_reported(tmp_path: Path) -> None:
    found = _check(tmp_path, {
        **BASE,
        "app/glue.py": "from . import core\n\ndef f(a):\n    return core.f(a)\n",
        "app/other.py": "from .glue import f\n",
    })
    assert _forwarded(found) == []
