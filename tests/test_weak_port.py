from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import mini_policy, ring_with, write_files

from arborescent.adapters import FsScanner
from arborescent.domain import Rule
from arborescent.facts import extract
from arborescent.rules import check_modules

PROTOCOL_HEADER = "from typing import Any, Protocol\n\n"


def weak(source: str) -> list[str]:
    """Names (``Class.method``) the extractor reports as weakly typed protocol returns."""
    return [name for _, name, _ in extract(PROTOCOL_HEADER + source).weak_port_returns]


@pytest.mark.parametrize(
    "annotation",
    [
        "Any", "object", "dict", "list", "tuple", "set", "frozenset", "typing.Any", "'Any'", "dict[str, Any]",
        "Mapping[str, Any]", "typing.Mapping[str, typing.Any]", "Any | None", "Optional[object]", "'dict' | None",
        "typing.Dict", "typing.List", "Annotated[dict, 'x']",
    ],
)
def test_weak_return_annotations_are_flagged(annotation: str) -> None:
    assert weak(f"class Port(Protocol):\n    def get(self) -> {annotation}: ...\n") == ["Port.get"]


def test_missing_return_annotation_is_flagged() -> None:
    assert weak("class Port(Protocol):\n    def get(self): ...\n") == ["Port.get"]


def test_async_methods_are_checked() -> None:
    assert weak("class Port(Protocol):\n    async def get(self) -> Any: ...\n") == ["Port.get"]


@pytest.mark.parametrize(
    "annotation",
    [
        "None", "int", "str | None", "dict[str, int]", "list[str]", "tuple[int, ...]", "Mapping[str, int]", "Result",
        "Iterator[Event]", "Iterator[Created | Deleted]", "'Result'", "Optional[Result]", "Event", "bool",
    ],
)
def test_concrete_return_annotations_pass(annotation: str) -> None:
    assert weak(f"class Port(Protocol):\n    def get(self) -> {annotation}: ...\n") == []


def test_init_and_non_protocol_classes_are_ignored() -> None:
    source = (
        "class Port(Protocol):\n    def __init__(self, x: int): ...\n\n"
        "class Plain:\n    def get(self) -> Any: ...\n"
    )
    assert weak(source) == []


def test_protocol_variants_and_generic_protocols_are_found() -> None:
    source = (
        "import typing\nimport typing_extensions as te\nfrom typing import Generic, TypeVar\n\nT = TypeVar('T')\n\n"
        "class A(typing.Protocol):\n    def f(self) -> Any: ...\n\n"
        "class B(te.Protocol):\n    def f(self) -> Any: ...\n\n"
        "class C(Protocol[T]):\n    def f(self) -> Any: ...\n\n"
        "class D(Generic[T], Protocol):\n    def f(self) -> Any: ...\n"
    )
    assert weak(source) == ["A.f", "B.f", "C.f", "D.f"]


def test_nested_functions_and_non_method_statements_are_ignored() -> None:
    source = "class Port(Protocol):\n    x: Any\n    def f(self) -> int:\n        def inner(): ...\n        return 1\n"
    assert weak(source) == []


def test_empty_protocol_and_syntactically_odd_string_annotation_do_not_crash() -> None:
    assert weak("class Port(Protocol): ...\n") == []
    assert weak("class Port(Protocol):\n    def f(self) -> '[[': ...\n") == []


def run(root: Path, **overrides: Any) -> list[Any]:
    policy = mini_policy(**overrides)
    scan = FsScanner().scan(policy, root)
    return check_modules(policy, scan.modules, scan.known, scan.entry_deps, scan.arbor_index)


def weak_findings(found: list[Any]) -> list[Any]:
    return [v for v in found if v.rule == Rule.WEAKLY_TYPED_PORT]


SOURCE = PROTOCOL_HEADER + "class Port(Protocol):\n    def save(self) -> Any: ...\n    def ok(self) -> bool: ...\n"


def test_off_by_default(repo: Path) -> None:
    write_files(repo, {"app/ports.py": SOURCE})

    assert weak_findings(run(repo)) == []


def test_ring_that_requires_typed_ports_reports_each_weak_method(repo: Path) -> None:
    write_files(repo, {"app/ports.py": SOURCE})
    rings = ring_with(ports={"may_import": ["domain"], "external": ["typing"], "require_typed_ports": True})

    found = weak_findings(run(repo, ring=rings))

    assert [(v.subject, v.line) for v in found] == [("app.ports", 4)]
    assert "Port.save" in found[0].detail
    assert "[ports]" in found[0].detail


def test_other_rings_are_not_checked(repo: Path) -> None:
    write_files(repo, {"app/ports.py": SOURCE})
    rings = ring_with(domain={"may_import": [], "external": ["typing"], "require_typed_ports": True})

    assert weak_findings(run(repo, ring=rings)) == []
