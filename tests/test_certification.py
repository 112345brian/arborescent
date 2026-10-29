from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, write_files

from arborescent.adapters import FsScanner, LocalFileSystem, SubprocessToolRunner
from arborescent.certification import (
    _native_minimum,
    _tach_canary_modules,
    _tach_probes,
)
from arborescent.generate import tach
from arborescent.installer_adapter import _toml_array_close


def test_toml_array_scanner_skips_brackets_inside_strings_and_comments() -> None:
    text = 'deps = ["package[extra]", # comment with ]\n { include-group = "lint" }]'
    opening = text.index("[")

    assert text[_toml_array_close(text, opening)] == "]"


def test_profile_requires_circular_import_enforcement(tmp_path: Path) -> None:
    write_files(tmp_path, MINI_FILES)
    scan = FsScanner().scan(mini_policy(), tmp_path)

    assert any("forbid_circular_imports" in failure for failure in _native_minimum(mini_policy(forbid_circular_imports=False), scan))


def test_tach_canary_modules_follow_annotations_over_classify_globs(tmp_path: Path) -> None:
    write_files(tmp_path, {
        **MINI_FILES,
        "app/domain.py": '__arbor__ = {"ring": "composition"}\n',
        "app/main.py": '__arbor__ = {"ring": "domain"}\n',
    })
    policy = mini_policy()
    scan = FsScanner().scan(policy, tmp_path)

    source, targets = _tach_canary_modules(policy, scan)

    assert source is not None
    assert source.name == "app.main"
    assert targets == ("app.domain",)


def test_profile_requires_annotations_without_changing_other_failures(tmp_path: Path) -> None:
    write_files(tmp_path, MINI_FILES)
    policy = mini_policy(forbid_circular_imports=False)
    scan = FsScanner().scan(policy, tmp_path)
    enabled_failures = _native_minimum(policy, scan)
    disabled_policy = mini_policy(forbid_circular_imports=False, disabled_tools=["arbor-annotations"])

    assert enabled_failures == ["minimum profile requires `forbid_circular_imports = true`"]
    assert _native_minimum(disabled_policy, scan) == [
        *enabled_failures,
        "minimum profile requires `__arbor__` annotations to be enabled",
    ]
    assert _native_minimum(mini_policy(), scan) == []


def _tach_project(root: Path) -> tuple[object, object]:
    write_files(root, MINI_FILES)
    policy = mini_policy()
    scan = FsScanner().scan(policy, root)
    write_files(root, {"tach.toml": tach(policy, sorted(scan.known)), "pyproject.toml": "[project]\ndependencies = []\n"})
    return policy, scan


@pytest.mark.skipif(shutil.which("tach") is None, reason="Tach is an optional install extra")
def test_tach_canaries_accept_enforced_profile_and_catch_disabled_checks(tmp_path: Path) -> None:
    policy, scan = _tach_project(tmp_path)
    fs = LocalFileSystem()
    runner = SubprocessToolRunner(tmp_path)

    assert _tach_probes(tmp_path, policy, scan, runner, fs) == []

    config = (tmp_path / "tach.toml").read_text(encoding="utf-8")
    weakened = config
    for layer in ("composition", "adapters", "ports"):
        weakened = weakened.replace(f'layer = "{layer}"', 'layer = "domain"')
    (tmp_path / "tach.toml").write_text(
        weakened.replace("forbid_circular_dependencies = true", "forbid_circular_dependencies = false")
        .replace("exclude = []", 'exclude = ["__arborescent_profile_missing_external_7f0f6c19__"]'),
        encoding="utf-8",
    )
    failures = _tach_probes(tmp_path, policy, scan, runner, fs)

    assert any("Tach layer canary" in failure for failure in failures)
    assert any("Tach external-dependency canary" in failure for failure in failures)
    assert (tmp_path / "app/domain.py").read_text(encoding="utf-8") == MINI_FILES["app/domain.py"]
