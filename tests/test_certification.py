from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from arborescent_helpers import MINI_FILES, mini_policy, write_files
from test_engine import services

from arborescent.adapters import FsScanner, LocalFileSystem, SubprocessToolRunner
from arborescent.certification import (
    _native_minimum,
    _tach_canary_modules,
    _tach_canary_modules_by_scope,
    _tach_probes,
    tach_multi_root_note,
)
from arborescent.config import policy_from_mapping
from arborescent.engine import Options, run_check
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


def test_profile_canaries_cover_each_scope(tmp_path: Path) -> None:
    write_files(tmp_path, {
        "libs/alpha/src/alpha/core.py": "VALUE = 1\n",
        "libs/alpha/src/alpha/outer.py": "VALUE = 2\n",
        "libs/beta/src/beta/core.py": "VALUE = 3\n",
        "libs/beta/src/beta/outer.py": "VALUE = 4\n",
    })
    policy = policy_from_mapping({
        "scopes": {
            "alpha": {
                "sources": ["libs/alpha/src/alpha"], "source_roots": ["libs/alpha/src"],
                "rings": ["domain", "composition"],
                "classify": {"alpha.core": "domain", "alpha.outer": "composition"},
            },
            "beta": {
                "sources": ["libs/beta/src/beta"], "source_roots": ["libs/beta/src"],
                "rings": ["domain", "adapters"],
                "classify": {"beta.core": "domain", "beta.outer": "adapters"},
            },
        },
    })
    scan = FsScanner().scan(policy, tmp_path)

    canaries = _tach_canary_modules_by_scope(policy, scan)

    assert set(canaries) == {"alpha", "beta"}
    assert canaries["alpha"][0].name == "alpha.core"
    assert canaries["alpha"][1] == ("alpha.outer",)
    assert canaries["beta"][0].name == "beta.core"
    assert canaries["beta"][1] == ("beta.outer",)


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


def test_tach_multi_root_note_covers_multi_root_and_multi_scope_policies() -> None:
    assert tach_multi_root_note(mini_policy()) is None
    assert tach_multi_root_note(mini_policy(source_roots=["src", "src/"])) is None
    note = tach_multi_root_note(mini_policy(source_roots=["packages/a/src", "packages/b/src"]))
    assert note is not None and "#41" in note
    scoped = policy_from_mapping({
        "scopes": {
            name: {"sources": [f"{name}/src/{name}"], "source_roots": [f"{name}/src"], "rings": ["domain"]}
            for name in ("alpha", "beta")
        },
    })
    assert tach_multi_root_note(scoped) is not None


def test_check_reports_multi_root_tach_limitation_as_advisory_drift(repo: Path) -> None:
    policy = mini_policy(source_roots=[".", "app"])
    report = run_check(policy, repo, services(), Options(tools=True))
    assert any(line.startswith("tach: ") and "#41" in line for line in report.drift)
    single = run_check(mini_policy(), repo, services(), Options(tools=True))
    assert not any("#41" in line for line in single.drift)
