"""#55: annotations survive a split. Parts are real extractions (#27) kept inside the repository; the origin is the catalog (#52)."""

from __future__ import annotations

from pathlib import Path

import pytest
from arborescent_helpers import MINI, mini_policy, services, write_files

import arborescent.package_gaps as package_gaps
from arborescent.adapters import FsScanner, LocalFileSystem
from arborescent.domain import INFORMATIONAL_RULES, PolicyError, Rule
from arborescent.engine import Options, run_check, run_own
from arborescent.package_gaps import load_package_context
from arborescent.package_model import CatalogError
from arborescent.packages import PackageCatalog, load_extracted_package
from arborescent.selftest import plan, selftest

FS = LocalFileSystem()

PRESERVATION_RULES = {
    Rule.PACKAGE_SEMANTICS_DRIFT, Rule.PACKAGE_MEMBERSHIP_DRIFT, Rule.PACKAGE_RING_DRIFT,
    Rule.PACKAGE_PUBLIC_SYMBOL_DROPPED, Rule.PACKAGE_PRIVATE_IMPORT,
}

ORIGIN = {
    "app/__init__.py": "",
    "app/domain.py": "from __future__ import annotations\n\n\nclass Thing:\n    pass\n\n\ndef make() -> Thing:\n    return Thing()\n",
    "app/ports.py": "from __future__ import annotations\n\nfrom . import domain\n\n\ndef port() -> domain.Thing:\n    return domain.make()\n",
    "app/internal.py": "from __future__ import annotations\n\n\ndef secret() -> int:\n    return 1\n",
    "app/adapters.py": "from __future__ import annotations\n\nfrom . import domain, ports\n\n\ndef both() -> tuple[object, object]:\n    return domain.make(), ports.port()\n",
    "app/main.py": "from __future__ import annotations\n\nfrom . import adapters\n\nBOTH = adapters.both()\n",
}

PART_POLICY = {
    "core": 'sources = ["app"]\nrings = ["domain", "ports"]\n[ring.domain]\nexternal = ["@pure"]\n[ring.ports]\nmay_import = ["domain"]\n'
            'external = ["@pure"]\n[groups]\npure = ["__future__", "dataclasses", "typing", "collections"]\n'
            '[classify]\n"app" = "domain"\n"app.domain" = "domain"\n"app.internal" = "domain"\n"app.ports" = "ports"\n',
    "shell": 'sources = ["app"]\nrings = ["adapters", "composition"]\n[ring.adapters]\nexternal = ["@pure"]\n'
             '[ring.composition]\nmay_import = ["*"]\nexternal = ["@pure"]\n[groups]\n'
             'pure = ["__future__", "dataclasses", "typing", "collections"]\n[classify]\n"app.adapters" = "adapters"\n"app.main" = "composition"\n',
}


def _package(pid: str, name: str, files: list[str], *, public: list[str] | None, depends: list[str] | None = None) -> str:
    lines = [f'[[packages]]\nid = "{pid}"\nname = "{name}"\nkind = "library"\nrepository = "example/repo"\nroot = "app"\nsummary = "{name} part"']
    if public is not None:
        lines.append(f"public = {public!r}".replace("'", '"'))
    if depends:
        lines.append(f"depends_on = {depends!r}".replace("'", '"'))
    lines.extend(
        f'[[packages.paths]]\npath = "{path}"\nrole = "source"\ndestination = "{path}"\nmetadata = {{ layer = "{pid}" }}'
        for path in files
    )
    lines.append(f'[[packages.paths]]\npath = "split-policy/{pid}.toml"\nrole = "config"\ndestination = "arborescent.toml"')
    return "\n".join(lines) + "\n"


def catalog_text(core_public: list[str] | None = None, shell_public: list[str] | None = None) -> str:
    core_public = ["app", "app.domain", "app.ports"] if core_public is None else core_public
    return (
        _package("core", "Core", ["app/__init__.py", "app/domain.py", "app/ports.py", "app/internal.py"], public=core_public)
        + "\n"
        + _package("shell", "Shell", ["app/adapters.py", "app/main.py"], public=shell_public, depends=["core"])
    )


def build(root: Path, *, core_public: list[str] | None = None, split: bool = True, origin: dict[str, str] | None = None) -> None:
    """The origin repository, and (unless ``split`` is false) both parts extracted into ``parts/``."""
    write_files(root, {
        **ORIGIN, **(origin or {}), "packages.toml": catalog_text(core_public),
        "split-policy/core.toml": PART_POLICY["core"], "split-policy/shell.toml": PART_POLICY["shell"],
    })
    if split:
        catalog = PackageCatalog.load(FS, root, "packages.toml")
        for pid in ("core", "shell"):
            catalog.extract(pid, root / "parts" / pid)


def policy(**overrides: object):
    return mini_policy(package_catalog="packages.toml", package_parts=["parts/core", "parts/shell"],
                       classify={**MINI["classify"], "app.internal": "domain"}, **overrides)


def findings(root: Path, **overrides: object) -> list[tuple[str, str]]:
    found, _ = run_own(policy(**overrides), root, services())
    return sorted((v.rule, v.subject) for v in found if v.rule in PRESERVATION_RULES)


def details(root: Path, rule: Rule) -> list[str]:
    found, _ = run_own(policy(), root, services())
    return [v.detail for v in found if v.rule == rule]


def edit(root: Path, rel: str, old: str, new: str) -> None:
    path = root / rel
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{old!r} not in {rel}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")


def drop(root: Path, part: str, rel: str) -> None:
    """Remove a file from a part the way a real split would: the file and its manifest entry, so the manifest stays valid."""
    (root / "parts" / part / rel).unlink()
    manifest = root / "parts" / part / ".arborescent-package.toml"
    head, *blocks = manifest.read_text(encoding="utf-8").split("[[paths]]\n")
    kept = [block for block in blocks if f'current_path = "{rel}"' not in block]
    assert len(kept) == len(blocks) - 1, rel
    manifest.write_text("[[paths]]\n".join([head, *kept]), encoding="utf-8", newline="\n")


def test_a_faithful_split_has_no_preservation_findings(tmp_path: Path) -> None:
    build(tmp_path)
    assert findings(tmp_path) == []


def test_the_fixture_parts_are_real_extractions(tmp_path: Path) -> None:
    build(tmp_path)
    extracted = load_extracted_package(FS, tmp_path / "parts" / "core")
    assert extracted.semantics.public == ("app", "app.domain", "app.ports")


# --- policy key ---------------------------------------------------------------------------------------------------

def test_the_parts_key_needs_a_catalog() -> None:
    with pytest.raises(PolicyError, match="package_catalog"):
        mini_policy(package_parts=["parts/core"])


def test_the_parts_key_needs_the_strict_preset() -> None:
    with pytest.raises(PolicyError, match="strict"):
        mini_policy(package_catalog="packages.toml", package_parts=["parts/core"], preset="relaxed")


@pytest.mark.parametrize("value", [["../core"], ["/abs"], [""], ["a", "a"], "parts/core", [5]])
def test_the_parts_key_must_list_distinct_relative_paths(value: object) -> None:
    with pytest.raises(PolicyError, match="package_parts"):
        mini_policy(package_catalog="packages.toml", package_parts=value)


def test_nothing_is_checked_without_parts_or_public_surfaces(tmp_path: Path) -> None:
    build(tmp_path, split=False)
    found, _ = run_own(mini_policy(package_catalog="packages.toml", classify={**MINI["classify"], "app.internal": "domain"}), tmp_path, services())
    assert not {v.rule for v in found} & PRESERVATION_RULES


def test_a_package_without_a_declared_surface_is_open(tmp_path: Path) -> None:
    open_adapters = ORIGIN["app/adapters.py"] + "\nfrom . import internal\n"
    build(tmp_path, core_public=[], origin={"app/adapters.py": open_adapters})
    assert findings(tmp_path) == []  # public = []: no surface declared, so nothing is private


def test_a_part_that_is_not_an_extraction_is_an_error_not_a_pass(tmp_path: Path) -> None:
    build(tmp_path, split=False)
    (tmp_path / "parts" / "core").mkdir(parents=True)
    with pytest.raises(CatalogError, match=r"cannot read .*\.arborescent-package\.toml"):
        run_own(policy(), tmp_path, services())


def test_a_part_without_a_policy_cannot_stand_alone(tmp_path: Path) -> None:
    build(tmp_path)
    drop(tmp_path, "core", "arborescent.toml")
    with pytest.raises(CatalogError, match="policy"):
        run_own(policy(), tmp_path, services())


# --- package-ring-drift -------------------------------------------------------------------------------------------

def test_a_changed_ring_assignment_is_reported(tmp_path: Path) -> None:
    build(tmp_path)
    edit(tmp_path, "parts/core/arborescent.toml", '"app.ports" = "ports"', '"app.ports" = "domain"')
    assert findings(tmp_path) == [(Rule.PACKAGE_RING_DRIFT, "app.ports")]
    assert "ports" in details(tmp_path, Rule.PACKAGE_RING_DRIFT)[0] and "domain" in details(tmp_path, Rule.PACKAGE_RING_DRIFT)[0]


def test_an_arbor_annotation_that_moves_a_module_is_ring_drift(tmp_path: Path) -> None:
    build(tmp_path)
    path = tmp_path / "parts/core/app/ports.py"
    path.write_text('__arbor__ = {"ring": "domain"}\n' + path.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
    assert (Rule.PACKAGE_RING_DRIFT, "app.ports") in findings(tmp_path)


# --- package-public-symbol-dropped --------------------------------------------------------------------------------

def test_a_dropped_public_symbol_is_reported(tmp_path: Path) -> None:
    build(tmp_path)
    edit(tmp_path, "parts/core/app/domain.py", "def make() -> Thing:\n    return Thing()\n", "")
    assert findings(tmp_path) == [(Rule.PACKAGE_PUBLIC_SYMBOL_DROPPED, "app.domain")]
    assert "make" in details(tmp_path, Rule.PACKAGE_PUBLIC_SYMBOL_DROPPED)[0]


def test_a_dropped_private_symbol_is_not_a_public_surface_change(tmp_path: Path) -> None:
    build(tmp_path)
    edit(tmp_path, "parts/core/app/internal.py", "def secret() -> int:\n    return 1\n", "")
    assert findings(tmp_path) == []  # app.internal is not in core's public surface


def test_added_symbols_are_not_drift(tmp_path: Path) -> None:
    build(tmp_path)
    path = tmp_path / "parts/core/app/domain.py"
    path.write_text(path.read_text(encoding="utf-8") + "\n\ndef extra() -> int:\n    return 2\n", encoding="utf-8", newline="\n")
    assert findings(tmp_path) == []


# --- package-membership-drift -------------------------------------------------------------------------------------

def test_a_module_missing_from_its_part_is_reported(tmp_path: Path) -> None:
    build(tmp_path)
    drop(tmp_path, "core", "app/ports.py")
    assert (Rule.PACKAGE_MEMBERSHIP_DRIFT, "app.ports") in findings(tmp_path)
    assert (Rule.PACKAGE_SEMANTICS_DRIFT, "core") in findings(tmp_path)  # the path is gone from the manifest too
    assert "core" in details(tmp_path, Rule.PACKAGE_MEMBERSHIP_DRIFT)[0]


def test_a_module_in_a_part_that_the_catalog_gives_elsewhere_is_reported(tmp_path: Path) -> None:
    build(tmp_path)
    write_files(tmp_path, {"parts/core/app/adapters.py": ORIGIN["app/adapters.py"]})  # the catalog gives it to shell
    found = findings(tmp_path)
    assert (Rule.PACKAGE_MEMBERSHIP_DRIFT, "app.adapters") in found
    assert "shell" in details(tmp_path, Rule.PACKAGE_MEMBERSHIP_DRIFT)[0]


# --- package-semantics-drift --------------------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('summary = "Core part"', 'summary = "Core, changed"'),
        ('public = ["app", "app.domain", "app.ports"]', 'public = ["app"]'),
        ('metadata = { "layer" = "core" }', 'metadata = { "layer" = "other" }'),
    ],
)
def test_changed_semantic_metadata_is_reported(tmp_path: Path, old: str, new: str) -> None:
    build(tmp_path)
    edit(tmp_path, "parts/core/.arborescent-package.toml", old, new)
    assert (Rule.PACKAGE_SEMANTICS_DRIFT, "core") in findings(tmp_path)


def test_a_part_naming_a_package_the_catalog_lacks_is_semantic_drift(tmp_path: Path) -> None:
    build(tmp_path)
    edit(tmp_path, "packages.toml", 'id = "shell"', 'id = "renamed"')
    assert (Rule.PACKAGE_SEMANTICS_DRIFT, "shell") in findings(tmp_path)


def test_moved_paths_are_coordinates_not_drift(tmp_path: Path) -> None:
    """The invariant is semantic metadata unchanged, coordinates rebased (#27): a different current location is fine."""
    build(tmp_path)
    assert findings(tmp_path) == []
    extracted = load_extracted_package(FS, tmp_path / "parts" / "shell")
    assert all(item.origin_path == item.current_path or item.role == "config" for item in extracted.paths)


# --- package-private-import ---------------------------------------------------------------------------------------

def test_a_monorepo_module_that_bypasses_a_public_surface_is_reported(tmp_path: Path) -> None:
    build(tmp_path, core_public=["app", "app.ports"])  # domain is no longer public, adapters imports it
    assert (Rule.PACKAGE_PRIVATE_IMPORT, "app.adapters") in findings(tmp_path)


def test_a_part_importing_a_private_module_of_another_part_is_reported(tmp_path: Path) -> None:
    build(tmp_path)
    path = tmp_path / "parts/shell/app/adapters.py"
    path.write_text(path.read_text(encoding="utf-8") + "\nfrom . import internal\n", encoding="utf-8", newline="\n")
    found = findings(tmp_path)
    assert (Rule.PACKAGE_PRIVATE_IMPORT, "app.adapters") in found
    assert "parts/shell" in details(tmp_path, Rule.PACKAGE_PRIVATE_IMPORT)[0]


def test_imports_through_the_public_surface_pass(tmp_path: Path) -> None:
    build(tmp_path)
    assert not any(rule == Rule.PACKAGE_PRIVATE_IMPORT for rule, _ in findings(tmp_path))


def test_a_package_importing_its_own_private_module_is_fine(tmp_path: Path) -> None:
    build(tmp_path)
    path = tmp_path / "parts/core/app/ports.py"
    path.write_text(path.read_text(encoding="utf-8") + "\nfrom . import internal\n", encoding="utf-8", newline="\n")
    assert not any(rule == Rule.PACKAGE_PRIVATE_IMPORT for rule, _ in findings(tmp_path))


# --- check integration --------------------------------------------------------------------------------------------

def test_preservation_findings_fail_the_check_and_are_never_informational(tmp_path: Path) -> None:
    build(tmp_path)
    drop(tmp_path, "core", "app/ports.py")
    report = run_check(policy(), tmp_path, services(), Options(tools=False, use_baseline=False))
    assert Rule.PACKAGE_MEMBERSHIP_DRIFT in {v.rule for v in report.violations}
    assert not PRESERVATION_RULES & INFORMATIONAL_RULES
    assert not report.ok


# --- selftest probes ----------------------------------------------------------------------------------------------

def planned(root: Path):
    pol = policy()
    scan = FsScanner().scan(pol, root)
    context = load_package_context(pol, root, FS, scan, scanner=FsScanner())
    return plan(pol, scan, context)


def test_every_preservation_rule_has_a_selftest_mutation(tmp_path: Path) -> None:
    build(tmp_path)
    mutations, skipped = planned(tmp_path)
    assert {m.rule for m in mutations} >= PRESERVATION_RULES, skipped


def test_no_preservation_mutation_is_planned_without_parts(tmp_path: Path) -> None:
    build(tmp_path)
    pol = mini_policy(package_catalog="packages.toml", classify={**MINI["classify"], "app.internal": "domain"})
    scan = FsScanner().scan(pol, tmp_path)
    rules = {m.rule for m in plan(pol, scan, load_package_context(pol, tmp_path, FS, scan))[0]}
    assert Rule.PACKAGE_PRIVATE_IMPORT in rules  # needs only the catalog's public surfaces
    assert not rules & (PRESERVATION_RULES - {Rule.PACKAGE_PRIVATE_IMPORT})  # the drift rules need parts to compare with


def test_selftest_kills_every_preservation_mutation(tmp_path: Path) -> None:
    build(tmp_path)
    report = selftest(policy(), tmp_path, services())
    assert report.survived == []
    for rule in PRESERVATION_RULES:
        assert any(entry.startswith(f"[{rule}]") for entry in report.killed), (rule, report.skipped)


@pytest.mark.parametrize("rule", sorted(PRESERVATION_RULES))
def test_selftest_reports_a_survivor_when_a_rule_is_removed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rule: Rule) -> None:
    """The guard against rule removal: drop one rule's findings and exactly its probe must survive."""
    build(tmp_path)
    real = package_gaps.preservation_violations
    monkeypatch.setattr(package_gaps, "preservation_violations", lambda *a, **k: [v for v in real(*a, **k) if v.rule != rule])
    report = selftest(policy(), tmp_path, services())
    assert {entry.split("]")[0].lstrip("[") for entry in report.survived} == {rule}
