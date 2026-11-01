"""Shared test data and helpers (a plain module so it cannot collide with another suite's conftest)."""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from arborescent.adapters import FsScanner, LocalFileSystem, SystemClock
from arborescent.architecture_contracts import compile_contracts
from arborescent.config import policy_from_mapping
from arborescent.domain import Policy
from arborescent.engine import Services
from arborescent.engines import ImportLinter, Tach, default_engines
from arborescent.generate import GraphFacts
from arborescent.generate import generate as _generate
from arborescent.generate import generate_for_scan as _generate_for_scan
from arborescent.package_checks import AnnotationPackageChecks
from arborescent.testing import FakeToolRunner
from arborescent.typecheckers import all_checkers

PURE = ["__future__", "dataclasses", "typing", "collections"]

RINGS: dict[str, Any] = {
    "domain": {"external": ["@pure"]},
    "ports": {"may_import": ["domain"], "external": ["@pure"]},
    "adapters": {"may_import": ["domain", "ports"], "external": ["@pure", "os", "psutil"], "os_branching": True},
    "composition": {"may_import": ["*"], "external": ["@pure", "sys"]},
}
CLASSIFY: dict[str, str] = {
    "app": "domain",
    "app.domain": "domain",
    "app.ports": "ports",
    "app.adapters": "adapters",
    "app.main": "composition",
}
MINI: dict[str, Any] = {
    "sources": ["app"],
    "rings": ["domain", "ports", "adapters", "composition"],
    "groups": {"pure": PURE},
    "ring": RINGS,
    "classify": CLASSIFY,
}

MINI_FILES = {
    "app/__init__.py": "",
    "app/domain.py": "from __future__ import annotations\n\nVALUE: int = 1\n",
    "app/ports.py": "from __future__ import annotations\n\nfrom . import domain\n\nPORT_VALUE: int = domain.VALUE\n",
    "app/adapters.py": (
        "from __future__ import annotations\n\nimport os\n\nfrom . import domain, ports\n\n"
        "SEP: str = os.sep\nBOTH: tuple[int, int] = (domain.VALUE, ports.PORT_VALUE)\n"
    ),
    "app/main.py": "import sys\n\nfrom . import adapters\n\nARGS: tuple[list[str], str] = (sys.argv, adapters.SEP)\n",
}


def mini_policy(**overrides: Any) -> Policy:
    return policy_from_mapping({**MINI, **overrides})


def ring_with(**changes: Any) -> dict[str, Any]:
    """The mini rings with some ring tables replaced, e.g. ``ring_with(domain={"external": ["sys"]})``."""
    return {**RINGS, **changes}


def write_files(root: Path, files: Mapping[str, str]) -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        _ = path.write_text(text, encoding="utf-8", newline="\n")


CHECKERS = list(all_checkers().values())


def services(runner: FakeToolRunner | None = None, cache: Path | None = None) -> Services:
    return Services(
        FsScanner(cache), runner or FakeToolRunner(), CHECKERS, LocalFileSystem(), SystemClock(), default_engines(), AnnotationPackageChecks()
    )


RENDERERS = [ImportLinter(), Tach()]


def generate(policy, names, checkers, arbor_index=None, scope_by_module=None):  # type: ignore[no-untyped-def]
    """``arborescent.generate.generate`` with import-linter and tach bound, as the entrypoint binds them."""
    return _generate(policy, GraphFacts(names, arbor_index or {}, scope_by_module or {}), checkers, RENDERERS)


def generate_for_scan(policy, scan, checkers, packages=None):  # type: ignore[no-untyped-def]
    return _generate_for_scan(policy, scan, checkers, RENDERERS, packages)


def import_linter(policy, names, arbor_index=None, scope_by_module=None, packages=None) -> str:  # type: ignore[no-untyped-def]
    return ImportLinter().render_contracts(compile_contracts(policy, names, arbor_index or {}, scope_by_module or {}, packages))


def tach(policy, names, arbor_index=None, scope_by_module=None, packages=None) -> str:  # type: ignore[no-untyped-def]
    return Tach().render_contracts(compile_contracts(policy, names, arbor_index or {}, scope_by_module or {}, packages))
