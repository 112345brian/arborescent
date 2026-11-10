"""``resolve_targets`` answers a ring check (``from pkg import sub`` is ``pkg.sub``); ``resolve_runtime_targets`` answers what runs
(importing ``pkg.sub`` also runs ``pkg/__init__``). A caller picks one on purpose, so a new caller must be added here."""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).parents[1] / "src" / "arborescent"

RING_QUESTION = {"classify_audit.py", "coupling.py", "package_gaps.py", "practices.py", "rules.py"}
RUNTIME_QUESTION = {"package_parts.py"}


def _calls(name: str) -> dict[str, list[ast.Call]]:
    """Every call of ``name`` in the package, by file, other than its own definition module."""
    found: dict[str, list[ast.Call]] = {}
    for path in sorted(SRC.glob("*.py")):
        if path.name == "facts.py":
            continue
        calls = [
            node for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        ]
        if calls:
            found[path.name] = calls
    return found


def test_each_caller_of_resolve_targets_is_a_known_ring_question() -> None:
    assert set(_calls("resolve_targets")) == RING_QUESTION, (
        "a new caller of resolve_targets: decide whether it asks the ring question (resolve_targets) or the runtime one "
        "(resolve_runtime_targets) and list it in this file"
    )


def test_each_caller_of_resolve_runtime_targets_is_a_known_runtime_question() -> None:
    assert set(_calls("resolve_runtime_targets")) == RUNTIME_QUESTION


def _discarding_files(name: str) -> list[str]:
    """Files holding a bare statement ``name(...)``, whose result nobody reads."""
    return [
        file for file in _calls(name)
        for node in ast.walk(ast.parse((SRC / file).read_text(encoding="utf-8")))
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call) and isinstance(node.value.func, ast.Name) and node.value.func.id == name
    ]


def test_no_caller_discards_the_result_of_either_resolver() -> None:
    assert _discarding_files("resolve_targets") == [] and _discarding_files("resolve_runtime_targets") == []


def test_the_discard_scan_would_notice_a_discarded_call(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Guard against the scan going vacuous: point it at a file that does discard the result."""
    (tmp_path / "bad.py").write_text("from arborescent.facts import resolve_targets\n\nresolve_targets('m', False, None, [], [])\n", encoding="utf-8")
    monkeypatch.setattr(__import__(__name__), "SRC", tmp_path)
    assert _discarding_files("resolve_targets") == ["bad.py"]
