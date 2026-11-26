"""A class with too many fields or methods is a SUGGEST line (#220): never a violation, never baselined, easy to switch off."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, services, write_files

from arborescent.adapters import LocalFileSystem
from arborescent.check_runner import run_check
from arborescent.cli import main
from arborescent.config import policy_from_mapping
from arborescent.domain import PolicyError
from arborescent.engine import Options, write_generated
from arborescent.suggestions import (
    ClassSuggestion,
    class_size_suggestions,
    file_size_suggestions,
)


def _suggest(root: Path, source: str, max_fields: int = 2, max_methods: int = 2) -> list[ClassSuggestion]:
    _ = (root / "m.py").write_text(source, encoding="utf-8")
    return class_size_suggestions(LocalFileSystem().read_text, root, {"m": "m.py"}, max_fields=max_fields, max_methods=max_methods)


def _counts(root: Path, source: str) -> list[tuple[str, int, int]]:
    return [(s.name, s.fields, s.methods) for s in _suggest(root, source, max_fields=1, max_methods=1)]


def test_a_count_must_exceed_the_threshold(tmp_path: Path) -> None:
    source = "class C:\n    a: int\n    b: int\n\n    def f(self): pass\n    def g(self): pass\n"
    assert _suggest(tmp_path, source) == []
    over = _suggest(tmp_path, source, max_fields=1)
    assert [s.message for s in over] == ["m.py:1: class C has 2 fields (suggestion threshold: 1)"]
    both = _suggest(tmp_path, source, max_fields=1, max_methods=1)
    assert both[0].message == "m.py:1: class C has 2 fields (suggestion threshold: 1) and 2 methods (suggestion threshold: 1)"


def test_zero_switches_one_dimension_off(tmp_path: Path) -> None:
    source = "class C:\n    a: int\n    b: int\n\n    def f(self): pass\n    def g(self): pass\n"
    assert [s.message for s in _suggest(tmp_path, source, max_fields=0, max_methods=1)] == [
        "m.py:1: class C has 2 methods (suggestion threshold: 1)"
    ]
    assert _suggest(tmp_path, source, max_fields=0, max_methods=0) == []


def test_fields_are_distinct_declared_or_self_assigned_names(tmp_path: Path) -> None:
    source = """from typing import ClassVar

class C:
    a: int
    b = 1
    c, d = 1, 2
    LIMIT = 3
    MAX_SIZE: int = 4
    shared: ClassVar[int] = 0
    other: "typing.ClassVar[int]" = 0
    __slots__ = ()
    if True:
        e: int

    def __init__(this, x):
        this.a = x
        this.f: int = x
        this.g, this.h = x
        this.g += 1
        x.not_mine = 1

        class Inner:
            def __init__(self):
                self.inner = 1

    @staticmethod
    def s(self):
        self.not_a_field = 1

    @classmethod
    def k(cls):
        cls.cached = 1
"""
    assert _counts(tmp_path, source) == [("C", 8, 2)]  # a b c d e f g h; methods s, k


def test_methods_are_distinct_non_dunder_names(tmp_path: Path) -> None:
    source = """class C:
    def __init__(self): self._v = 1
    def __eq__(self, other): return True
    @property
    def v(self): return self._v
    @v.setter
    def v(self, value):
        self._v = value
        self.v = value
    async def run(self): pass
"""
    assert _counts(tmp_path, source) == [("C", 1, 2)]  # `_v`; `self.v = ...` goes through the property


def test_enum_members_are_not_fields(tmp_path: Path) -> None:
    source = "import enum\n\nclass R(enum.StrEnum):\n    a = 'a'\n    b = 'b'\n    c = 'c'\n\nclass F(Flag):\n    a = 1\n    b = 2\n    c = 3\n"
    assert _suggest(tmp_path, source) == []


def test_nested_classes_are_counted_on_their_own(tmp_path: Path) -> None:
    source = "class Outer:\n    class Inner:\n        a: int\n        b: int\n        c: int\n"
    assert [(s.name, s.line, s.fields) for s in _suggest(tmp_path, source)] == [("Inner", 2, 3)]


@pytest.mark.parametrize("source", ["", "# class in a comment\n", "x = 'class'\n", "class Empty: pass\n"])
def test_nothing_to_report(tmp_path: Path, source: str) -> None:
    assert _suggest(tmp_path, source) == []


def test_a_byte_order_mark_does_not_crash_either_suggestion(tmp_path: Path) -> None:
    _ = (tmp_path / "m.py").write_bytes(b"\xef\xbb\xbfclass C:\n    a: int\n    b: int\n    c: int\n")
    read = LocalFileSystem().read_text
    assert len(class_size_suggestions(read, tmp_path, {"m": "m.py"}, max_fields=2, max_methods=2)) == 1
    assert len(file_size_suggestions(read, tmp_path, {"m": "m.py"}, threshold=1)) == 1


def test_a_large_class_is_quick(tmp_path: Path) -> None:
    source = "class C:\n" + "".join(f"    f{i}: int\n    def m{i}(self): self.s{i} = 1\n" for i in range(3000))
    assert [(s.fields, s.methods) for s in _suggest(tmp_path, source)] == [(6000, 3000)]


def test_defaults_and_configuration() -> None:
    assert (mini_policy().class_field_threshold, mini_policy().class_method_threshold) == (20, 20)
    policy = mini_policy(class_field_threshold=0, class_method_threshold=40)
    assert (policy.class_field_threshold, policy.class_method_threshold) == (0, 40)
    assert "thresholds" in policy.customization_labels


@pytest.mark.parametrize("key", ["class_field_threshold", "class_method_threshold"])
@pytest.mark.parametrize("value", [-1, True, "20", 1.5, None])
def test_a_bad_threshold_is_a_policy_error(key: str, value: object) -> None:
    with pytest.raises(PolicyError, match=key):
        _ = mini_policy(**{key: value})


def test_a_scoped_policy_takes_the_thresholds_from_the_root() -> None:
    policy = policy_from_mapping({
        "class_field_threshold": 3,
        "class_method_threshold": 4,
        "scopes": {"one": {"sources": ["app"], "source_roots": ["."], "rings": ["domain"]}},
    })
    assert (policy.class_field_threshold, policy.class_method_threshold) == (3, 4)


def test_check_prints_the_suggestion_and_never_fails_or_baselines_it(
    repo: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = mini_policy(class_field_threshold=1)
    write_files(repo, {**MINI_FILES, "app/domain.py": "class Wide:\n    a: int\n    b: int\n"})
    svc = services()
    write_generated(policy, repo, svc)

    report = run_check(policy, repo, svc, Options(tools=False))

    assert report.ratchet is not None and not any("Wide" in v.key for v in report.violations)
    assert [s.message for s in report.suggestions] == ["app/domain.py:1: class Wide has 2 fields (suggestion threshold: 1)"]
    write_files(repo, {"arborescent.toml": (
        'sources = ["app"]\nrings = ["domain"]\nclass_field_threshold = 1\n[classify]\n"app" = "domain"\n"app.*" = "domain"\n'
    )})
    monkeypatch.chdir(repo)
    _ = main(["check", "--no-tools"], svc)
    assert "SUGGEST app/domain.py:1: class Wide has 2 fields" in capsys.readouterr().out
