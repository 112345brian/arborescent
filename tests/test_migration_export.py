"""#54: the versioned, read-only JSON export of the migration arguments, its published schema, and the command."""

from __future__ import annotations

import copy
import json
import tomllib
from pathlib import Path
from typing import Any

import pytest
from arborescent_helpers import MINI_FILES, write_files
from jsonschema import Draft202012Validator, ValidationError
from test_migration_arguments import CATALOG, FACTS

from arborescent.cli import main
from arborescent.migration_arguments import build_migration_arguments
from arborescent.migration_json import (
    EXPORT_VERSION,
    SCHEMA,
    export_document,
    render_export,
    render_schema,
)
from arborescent.package_parse import parse_catalog

ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = Draft202012Validator(SCHEMA)


def document(catalog: str = CATALOG) -> dict[str, Any]:
    return export_document(build_migration_arguments(parse_catalog(tomllib.loads(catalog)), FACTS), "packages.toml")


def test_the_schema_is_itself_a_valid_json_schema() -> None:
    Draft202012Validator.check_schema(SCHEMA)


def test_the_export_validates_against_the_schema() -> None:
    VALIDATOR.validate(document())
    VALIDATOR.validate(document(CATALOG.replace("public = [\"app.core.api\"]\n", "")))  # a document with gaps is still a valid document


def test_the_published_schema_is_the_one_the_code_validates_with() -> None:
    published = (ROOT / "docs" / "schemas" / f"migration-export-v{EXPORT_VERSION}.schema.json").read_text(encoding="utf-8")
    assert published == render_schema()


def test_the_export_is_deterministic_and_stable_text() -> None:
    first = render_export(document())
    assert first == render_export(document())
    assert first.endswith("\n")
    assert list(json.loads(first)) == sorted(json.loads(first))  # keys are sorted: diffs between runs mean data changed


def test_the_export_is_versioned_and_names_what_it_was_read_from() -> None:
    data = document()
    assert (data["schema"], data["version"], data["catalog"]) == ("arborescent-migration-export", EXPORT_VERSION, "packages.toml")


def test_open_gaps_are_named_entries_and_flip_ready() -> None:
    clean = document()
    assert clean["ready"] is True and clean["gaps"] == [{"code": "package-spans-rings", "detail": clean["gaps"][0]["detail"], "package": None, "severity": "info", "subject": "web"}]
    broken = document(CATALOG.replace('[packages.home]\nkind = "repository"\nrepository = "example/core"\n', ""))
    assert broken["ready"] is False
    assert any(g["code"] == "package-undeclared-home" and g["package"] == "core" and g["severity"] == "error" for g in broken["gaps"])


def test_a_workspace_member_exports_no_history_or_pin_information() -> None:
    data = document()
    web = next(p for p in data["packages"] if p["id"] == "web")
    assert web["home"] == {"kind": "workspace-member", "path": "packages/web", "repository": None}
    assert web["policy"] is None
    tampered = copy.deepcopy(data)
    next(p for p in tampered["packages"] if p["id"] == "web")["policy"] = {"history": "keep"}
    with pytest.raises(ValidationError):
        VALIDATOR.validate(tampered)
    own = copy.deepcopy(data)
    next(p for p in own["packages"] if p["id"] == "core")["policy"] = {"history": "keep"}
    VALIDATOR.validate(own)  # a package with its own repository may carry policy (#56 fills it in)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(version=2),
        lambda d: d.pop("gaps"),
        lambda d: d["packages"][0].update(unknown=1),
        lambda d: d["gaps"][0].update(severity="warning"),
        lambda d: d["edges"].append({"source": "a", "target": "b"}),
        lambda d: d["packages"][0]["home"].update(kind="elsewhere"),
    ],
)
def test_the_schema_rejects_malformed_documents(mutate: Any) -> None:
    data = document()
    mutate(data)
    with pytest.raises(ValidationError):
        VALIDATOR.validate(data)


def test_the_export_carries_every_field_a_migration_tool_is_given() -> None:
    core = next(p for p in document()["packages"] if p["id"] == "core")
    assert set(core) == {"id", "name", "kind", "summary", "domain", "capabilities", "home", "owned", "surface", "dependencies", "policy", "pyproject"}
    assert set(core["owned"]) == {"modules", "assets", "path_renames", "module_renames"}
    assert core["owned"]["modules"][0] == {"module": "app.core.api", "path": "src/app/core/api.py", "target_path": "api.py", "target_module": "core.api"}
    assert core["surface"] == {"public": ["app.core.api"], "bypasses": [{"module": "app.web.routes", "package": "web", "imports": "app.core.internal"}]}
    assert core["dependencies"] == {"packages": [], "external": ["pydantic"]}


def test_policy_and_pyproject_are_reserved_slots_that_stay_null_until_their_issues_land() -> None:
    assert all(p["policy"] is None and p["pyproject"] is None for p in document()["packages"])


# -- the command -------------------------------------------------------------------------------------------------

def _repo(root: Path, catalog: str) -> None:
    files = {
        "app/__init__.py": "", "app/domain.py": "import pydantic\n", "app/main.py": "from app import domain\n", "app/data.json": "{}\n",
        "packages.toml": catalog,
        "arborescent.toml": 'package_catalog = "packages.toml"\nsources = ["app"]\nsource_roots = ["."]\nrings = ["domain"]\npreset = "strict"\n'
                            '[ring.domain]\nexternal = ["*"]\n[classify]\n"app*" = "domain"\n',
    }
    write_files(root, files)


CLI_CATALOG = """
[[packages]]
id = "core"
name = "Core"
kind = "library"
repository = "example/mono"
root = "app"
summary = "everything"
public = ["app", "app.domain"]
externals = ["pydantic"]
[packages.home]
kind = "repository"
repository = "example/core"
[[packages.paths]]
path = "app"
role = "source"
"""


def test_the_export_command_writes_the_validated_document(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(tmp_path, CLI_CATALOG)
    out = tmp_path / "export.json"
    assert main(["--config", str(tmp_path / "arborescent.toml"), "packages", "export", "--output", str(out)]) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    VALIDATOR.validate(data)
    core = data["packages"][0]
    assert [m["module"] for m in core["owned"]["modules"]] == ["app", "app.domain", "app.main"]
    assert [a["path"] for a in core["owned"]["assets"]] == ["app/data.json"]
    assert data["ready"] is True


def test_the_export_command_prints_to_stdout_without_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _repo(tmp_path, CLI_CATALOG)
    assert main(["--config", str(tmp_path / "arborescent.toml"), "packages", "export"]) == 0
    VALIDATOR.validate(json.loads(capsys.readouterr().out))


def test_require_ready_refuses_while_an_error_gap_is_open(tmp_path: Path) -> None:
    _repo(tmp_path, CLI_CATALOG.replace('[packages.home]\nkind = "repository"\nrepository = "example/core"\n', ""))
    out = tmp_path / "export.json"
    args = ["--config", str(tmp_path / "arborescent.toml"), "packages", "export", "--output", str(out)]
    assert main(args) == 0  # the export itself is always written: the gaps are its content
    assert json.loads(out.read_text(encoding="utf-8"))["ready"] is False
    assert main([*args, "--require-ready"]) == 1


def test_the_export_requires_the_package_catalog_key(tmp_path: Path) -> None:
    write_files(tmp_path, {**MINI_FILES, "packages.toml": CLI_CATALOG})
    write_files(tmp_path, {"arborescent.toml": 'sources = ["app"]\nrings = ["domain"]\n[ring.domain]\nexternal = ["*"]\n[classify]\n"app*" = "domain"\n'})
    assert main(["--config", str(tmp_path / "arborescent.toml"), "packages", "export"]) == 2


def test_the_schema_command_prints_the_published_schema(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["packages", "schema"]) == 0
    assert capsys.readouterr().out == render_schema()
