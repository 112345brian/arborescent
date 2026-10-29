"""Annotation discovery is complete even when enforcement is scoped to changed files."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files

from arborescent.scanner import FsScanner


def test_valid_annotation_brings_an_unclassified_file_into_enforcement(tmp_path: Path) -> None:
    write_files(tmp_path, {"other.py": '__arbor__ = {"ring": "domain"}\nimport os\n'})

    scan = FsScanner().scan(mini_policy(), tmp_path)

    assert scan.arbor_index == {"other": ("domain", None)}
    assert set(scan.modules) == {"other"}
    assert scan.modules["other"].facts.imports[0].module == "os"
    assert scan.unclassified == ()
    assert scan.parsed == 1


def test_invalid_annotations_remain_unclassified(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "unknown.py": '__arbor__ = {"ring": "missing"}\n',
        "malformed.py": '__arbor__ = {"ring": 42}\n',
        "absent.py": "VALUE = 1\n",
    })

    scan = FsScanner().scan(mini_policy(), tmp_path)

    assert scan.arbor_index["unknown"] == ("missing", None)
    assert scan.arbor_index["malformed"][0] is None
    assert scan.arbor_index["malformed"][1] is not None
    assert scan.arbor_index["absent"] == (None, None)
    assert scan.modules == {}
    assert scan.unclassified == (
        ("absent", "absent.py"), ("malformed", "malformed.py"), ("unknown", "unknown.py"),
    )


@pytest.mark.parametrize("warm", [False, True])
def test_partial_scan_resolves_unchanged_annotations_and_limits_reporting(tmp_path: Path, warm: bool) -> None:
    write_files(tmp_path, {
        "app/domain.py": "import other\n",
        "other.py": '__arbor__ = {"ring": "ports"}\n',
        "unclassified.py": "VALUE = 1\n",
    })
    policy = mini_policy()
    scanner = FsScanner(tmp_path / ".arborescent")
    full = scanner.scan(policy, tmp_path) if warm else None

    partial = scanner.scan(policy, tmp_path, only=frozenset({"app/domain.py"}))
    if full is None:
        full = scanner.scan(policy, tmp_path)

    assert partial.arbor_index == full.arbor_index == {
        "app.domain": (None, None), "other": ("ports", None), "unclassified": (None, None),
    }
    assert policy.resolve_ring(partial.arbor_index, "other") == "ports"
    assert policy.resolve_ring(full.arbor_index, "other") == "ports"
    assert set(partial.modules) == {"app.domain"}
    assert partial.unclassified == ()
    assert partial.parsed == (0 if warm else 3)


def test_disabled_annotations_skip_reading_out_of_scope_files(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "app/domain.py": "VALUE = 1\n",
        "classified.py": '__arbor__ = {"ring": "ports"}\n',
        "annotated.py": '__arbor__ = {"ring": "domain"}\n',
    })
    # Decoding either file would fail: neither is in the legacy enforcement scope.
    (tmp_path / "unreadable.py").write_bytes(b"\xff")
    policy = mini_policy(
        classify={"classified": "domain"}, disabled_tools=["arbor-annotations"],
    )

    scan = FsScanner(tmp_path / ".arborescent").scan(policy, tmp_path)

    assert scan.arbor_index == {}
    assert set(scan.modules) == {"app.domain", "classified"}
    assert scan.parsed == 2
    assert scan.unclassified == (("annotated", "annotated.py"), ("unreadable", "unreadable.py"))

    # An unchanged classified file also stays unread during a disabled partial scan.
    (tmp_path / "classified.py").write_bytes(b"\xff")
    partial = FsScanner().scan(policy, tmp_path, only=frozenset({"app/domain.py"}))
    assert partial.arbor_index == {}
    assert set(partial.modules) == {"app.domain"}
    assert partial.parsed == 1


def test_annotation_fields_survive_a_warm_cache_scan(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "app/valid.py": '__arbor__ = {"ring": "domain"}\n',
        "app/invalid.py": '__arbor__ = {"ring": 42}\n',
    })
    scanner = FsScanner(tmp_path / ".arborescent")
    cold = scanner.scan(mini_policy(), tmp_path)
    warm = scanner.scan(mini_policy(), tmp_path)

    assert cold.parsed == 2
    assert warm.parsed == 0
    assert warm.arbor_index == cold.arbor_index
    assert warm.modules["app.valid"].facts.arbor_ring == "domain"
    assert warm.modules["app.invalid"].facts.arbor_error is not None
    assert warm.modules["app.invalid"].facts.arbor_error == cold.modules["app.invalid"].facts.arbor_error


def test_pre_annotation_cache_is_reparsed(tmp_path: Path) -> None:
    source = '__arbor__ = {"ring": "domain"}\n'
    write_files(tmp_path, {"app/domain.py": source})
    cache_dir = tmp_path / ".arborescent"
    cache_dir.mkdir()
    digest = hashlib.blake2b(source.encode(), digest_size=16).hexdigest()
    (cache_dir / "scan.json").write_text(json.dumps({
        "v": 4, "files": {"app/domain.py": [digest, [[], [], None, [], []]]},
    }), encoding="utf-8")

    scan = FsScanner(cache_dir).scan(mini_policy(), tmp_path)

    assert scan.parsed == 1
    assert scan.modules["app.domain"].facts.arbor_ring == "domain"
    assert scan.arbor_index == {"app.domain": ("domain", None)}
