from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from arborescent_helpers import (
    MINI_FILES,
    mini_policy,
    ring_with,
    services,
    tach,
    write_files,
)

from arborescent.adapters import FsScanner, LocalFileSystem, SubprocessToolRunner
from arborescent.certification import (
    _native_minimum,
    _tach_canary_modules,
    _tach_canary_modules_by_scope,
    _tach_probes,
    certify,
    tach_multi_root_note,
    version_warnings,
)
from arborescent.check_runner import run_check
from arborescent.cli import main
from arborescent.config import policy_from_mapping
from arborescent.domain import ToolResult
from arborescent.engine import Options
from arborescent.installer_adapter import _toml_array_close
from arborescent.presets import CATEGORY_TRAITS, DEFAULT_FORBID_CATEGORIES
from arborescent.testing import FakeToolRunner


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


def test_minimum_profile_accepts_disabled_tach_only_when_import_linter_stays_on(tmp_path: Path) -> None:
    write_files(tmp_path, MINI_FILES)
    scan = FsScanner().scan(mini_policy(), tmp_path)

    only_tach = _native_minimum(mini_policy(disabled_tools=["tach"]), scan)
    assert not any("tach" in failure for failure in only_tach)
    both = _native_minimum(mini_policy(disabled_tools=["tach", "import-linter"]), scan)
    assert "required profile tool tach is disabled by policy" in both


def test_disabled_tach_requires_installed_and_configured_import_linter(tmp_path: Path) -> None:
    write_files(tmp_path, MINI_FILES)
    policy = mini_policy(disabled_tools=["tach", "ruff"])
    scan = FsScanner().scan(policy, tmp_path)
    fs = LocalFileSystem()

    missing = certify(tmp_path, policy, scan, FakeToolRunner(set()), fs)
    assert "import-linter is not installed; it is required when tach is disabled" in missing
    assert any("no contracts configured" in failure for failure in missing)

    write_files(tmp_path, {"pyproject.toml": "[tool.importlinter]\nroot_packages = ['app']\n"})
    ready = certify(tmp_path, policy, scan, FakeToolRunner({"lint-imports"}), fs)
    assert not any("import-linter" in failure for failure in ready)
    write_files(tmp_path, {"pyproject.toml": "[project]\nname = 'x'\n"})
    unconfigured = certify(tmp_path, policy, scan, FakeToolRunner({"lint-imports"}), fs)
    assert any("no contracts configured" in failure for failure in unconfigured)


def test_other_tool_versions_warn_but_never_fail(tmp_path: Path) -> None:
    newer = FakeToolRunner(
        {"ruff", "tach"}, {"ruff": ToolResult(0, "ruff 0.17.3", ""), "tach": ToolResult(0, "tach 0.35.3", "")}
    )
    warnings = version_warnings(tmp_path, newer)
    assert len(warnings) == 1
    assert "ruff version 0.17.3" in warnings[0]
    assert "verified with 0.17.2" in warnings[0]
    assert version_warnings(tmp_path, newer, ("ruff",)) == ()
    # A missing tool is still a failure, never merely a warning.
    assert version_warnings(tmp_path, FakeToolRunner(set())) == ()
    write_files(tmp_path, MINI_FILES)
    policy = mini_policy()
    scan = FsScanner().scan(policy, tmp_path)
    failures = certify(tmp_path, policy, scan, newer, LocalFileSystem())
    assert not any("requires 0.17.2" in failure or "version 0.17.3" in failure for failure in failures)
    assert any("not installed" in failure for failure in certify(tmp_path, policy, scan, FakeToolRunner(set()), LocalFileSystem()))


# The innermost ring (per scope) keeps its positional effect default; every other ring may be loosened.

_ALL_DEFAULT_TRAITS = ", ".join(CATEGORY_TRAITS[category] for category in sorted(DEFAULT_FORBID_CATEGORIES))


def _traits(categories: object) -> list[str]:
    return [CATEGORY_TRAITS[category] for category in sorted(categories)]  # type: ignore[call-overload]


def _weakened(**overrides: object) -> list[str]:
    return [failure for failure in _native_minimum(mini_policy(**overrides), object()) if "weakens" in failure]


def test_an_innermost_ring_that_drops_categories_names_them_and_the_fix() -> None:
    kept = sorted(DEFAULT_FORBID_CATEGORIES - {"clock", "env"})

    assert _weakened(ring=ring_with(domain={"traits": _traits(kept)})) == [
        "ring domain weakens positional effect defaults: its `traits` is missing no-clock, no-env; "
        "add them to `traits` in [ring.domain], or remove its effect traits to take the default"
    ]


def test_an_innermost_ring_that_permits_effects_is_missing_every_default() -> None:
    (failure,) = _weakened(ring=ring_with(domain={"traits": ["permits-effects"]}))

    assert f"is missing {_ALL_DEFAULT_TRAITS};" in failure


@pytest.mark.parametrize(
    "forbid",
    [
        None,
        sorted(DEFAULT_FORBID_CATEGORIES),
        sorted(DEFAULT_FORBID_CATEGORIES - {"dynamic-import"}),  # added to any ring that forbids something
        sorted(DEFAULT_FORBID_CATEGORIES | {"state", "cache"}),
    ],
)
def test_an_innermost_ring_that_forbids_at_least_the_default_passes(forbid: list[str] | None) -> None:
    domain = {} if forbid is None else {"traits": _traits(forbid)}

    assert _weakened(ring=ring_with(domain=domain)) == []


def test_no_effects_covers_every_default() -> None:
    assert _weakened(ring=ring_with(domain={"traits": ["no-effects"]})) == []


@pytest.mark.parametrize("ring", ["ports", "adapters", "composition"])
def test_rings_outside_the_innermost_may_allow_effects(ring: str) -> None:
    assert _weakened(ring=ring_with(**{ring: {"traits": ["permits-effects"]}})) == []
    assert _weakened(ring=ring_with(**{ring: {"traits": ["no-env"]}})) == []


def test_a_single_ring_has_no_positional_default_to_weaken() -> None:
    assert _weakened(rings=["domain"], ring={"domain": {"traits": ["permits-effects"]}}, classify={"app": "domain"}) == []


def test_every_scope_keeps_its_own_innermost_default() -> None:
    def scope(folder: str, forbid: list[str] | None) -> dict[str, object]:
        table = {} if forbid is None else {"traits": _traits(forbid)}
        return {"sources": [folder], "source_roots": ["."], "rings": ["core", "edge"], "ring": {"core": table, "edge": {"traits": ["permits-effects"]}}}

    policy = policy_from_mapping({"scopes": {
        "alpha": scope("alpha", None),
        "beta": scope("beta", sorted(DEFAULT_FORBID_CATEGORIES - {"network"})),
    }})

    assert [failure for failure in _native_minimum(policy, object()) if "weakens" in failure] == [
        "scope beta ring core weakens positional effect defaults: its `traits` is missing no-network; "
        "add them to `traits` in [scopes.beta.ring.core], or remove its effect traits to take the default"
    ]


# The callers of `certify`: the check report and the command line.


def test_require_profile_fails_the_check_and_prints_the_missing_categories(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = mini_policy(ring=ring_with(domain={"external": ["@pure"], "traits": ["permits-effects"]}))
    report = run_check(policy, repo, services(), Options(require_profile=True))

    assert any(_ALL_DEFAULT_TRAITS in failure for failure in report.profile_failures)
    assert not report.ok

    write_files(repo, {"arborescent.toml": (
        'sources = ["app"]\nrings = ["domain", "composition"]\n[ring.domain]\ntraits = ["no-dynamic-import"]\n'
        '[classify]\n"app" = "domain"\n"app.*" = "composition"\n'
    )})

    assert main(["--config", str(repo / "arborescent.toml"), "check", "--require-profile"]) == 1
    out = capsys.readouterr().out
    assert "PROFILE FAIL ring domain weakens positional effect defaults: its `traits` is missing no-clock," in out
    assert "CERTIFIED" not in out
