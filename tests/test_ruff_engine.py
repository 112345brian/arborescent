from __future__ import annotations

import hashlib
import json
import shutil
import tomllib
from pathlib import Path

import pytest
from arborescent_helpers import mini_policy, write_files

from arborescent.adapters import LocalFileSystem, SubprocessToolRunner
from arborescent.domain import ToolResult
from arborescent.engines import Ruff, TachExternal, default_engines
from arborescent.generate import generate
from arborescent.typecheckers import by_name


def test_generate_writes_a_ring_aware_ruff_config_and_excludes_vendor_trees() -> None:
    policy = mini_policy()
    generated = generate(policy, ["app.domain", "app.ports", "app.adapters", "app.main"], by_name(["pyright"]))

    config = tomllib.loads(generated["ruff.toml"])
    assert config["lint"]["preview"] is True
    assert config["lint"]["select"] == [
        "PLC0415", "BLE001", "E722", "C901", "PLR0912", "PLR0913", "PLR0915", "TID252", "PLW1514",
        "A", "ANN", "ASYNC", "C4", "DTZ", "I", "PERF", "PIE", "PTH", "RET", "RUF", "S", "SIM", "UP",
        "YTT", "T20", "F", "B",
    ]
    assert "PIE807" in config["lint"]["ignore"]
    assert any("app/main" in pattern and "T20" in rules for pattern, rules in config["lint"]["per-file-ignores"].items())
    for name in ("vendor", "vendored", "third_party", "site-packages"):
        assert any(name in pattern for pattern in config["exclude"])


def test_ruff_command_uses_native_discovery_and_ignores_suppressions() -> None:
    engine = Ruff(config_file="cfg/ruff.toml")
    assert engine.command(["app/domain.py"]) == [
        "ruff", "check", "--ignore-noqa", "--exit-zero", "--output-format", "json", "app/domain.py"
    ]


def test_ruff_findings_use_normalized_line_fingerprints_and_duplicate_suffixes(tmp_path: Path) -> None:
    fs = LocalFileSystem()
    source = "try:\n    pass\nexcept   Exception:\n    pass\nexcept Exception:\n    pass\n"
    write_files(tmp_path, {"app/domain.py": source})
    diagnostics = [
        {"code": "BLE001", "message": "Do not catch blind exception", "filename": "app/domain.py", "location": {"row": row}}
        for row in (3, 5)
    ]

    violations = Ruff().violations(ToolResult(0, json.dumps(diagnostics), ""), tmp_path, fs)

    fingerprint = hashlib.blake2b(b"exceptException:", digest_size=4).hexdigest()
    assert [v.key for v in violations] == [
        f"ruff-BLE001|app/domain.py|Do not catch blind exception|{fingerprint}",
        f"ruff-BLE001|app/domain.py|Do not catch blind exception|{fingerprint}#2",
    ]


@pytest.mark.parametrize(
    "stdout",
    [
        "[{\"code\": \"BLE001\"}",
        '[{"code":null,"message":"syntax error","filename":"app/domain.py","location":{"row":1}}]',
        '[{"code":"E902","message":"could not read","filename":"app/domain.py","location":{"row":1}}]',
    ],
)
def test_ruff_invalid_or_unbaselinable_output_fails_closed(tmp_path: Path, stdout: str) -> None:
    with pytest.raises(ValueError):
        Ruff().violations(ToolResult(0, stdout, ""), tmp_path, LocalFileSystem())


def test_engine_commands_use_project_root_native_discovery() -> None:
    engines = default_engines(output_dir="cfg")
    assert [engine.command([]) for engine in engines] == [
        ["lint-imports"], ["tach", "check", "--dependencies"], ["tach", "check-external"],
        ["ruff", "check", "--ignore-noqa", "--exit-zero", "--output-format", "json"],
    ]


def test_tach_external_engine_is_registered_and_uses_check_external() -> None:
    assert isinstance(TachExternal(), TachExternal)
    assert TachExternal().command([]) == ["tach", "check-external"]
    assert any(engine.name == "tach-external" for engine in default_engines())


def test_type_checker_commands_use_native_discovery() -> None:
    checkers = by_name(["pyright", "ty", "pyrefly"], output_dir="cfg")
    commands = {checker.name: " ".join(checker.command(["src/app.py"])) for checker in checkers}
    assert commands == {"pyright": "pyright src/app.py", "ty": "ty check src/app.py", "pyrefly": "pyrefly check src/app.py"}


def test_type_checker_config_paths_are_relative_to_output_directory() -> None:
    checkers = by_name(["pyright", "ty", "pyrefly"], output_dir="cfg")
    configs = {checker.name: checker.render_config(mini_policy(output_dir="cfg"), ["src/app"]) for checker in checkers}

    assert json.loads(configs["pyright"])["include"] == ["../src/app"]
    assert tomllib.loads(configs["ty"])["src"]["include"] == ["src/app"]
    assert tomllib.loads(configs["pyrefly"])["project-includes"] == ["../src/app"]


def test_ruff_ignore_noqa_contract_and_nested_config(tmp_path: Path) -> None:
    executable = shutil.which("ruff")
    if executable is None:
        pytest.skip("Ruff is installed by the dev extra")

    files = {
        "ruff.toml": generate(mini_policy(), ["app.domain"], by_name(["pyright"]))["ruff.toml"],
        "one.py": "try:\n    pass\nexcept Exception:  # noqa: BLE001\n    pass\n",
        "two.py": "# ruff: noqa: BLE001\ntry:\n    pass\nexcept Exception:\n    pass\n",
        "three.py": "# ruff: ignore[BLE001]\ntry:\n    pass\nexcept Exception:\n    pass\n",
        "four.py": "# ruff: file-ignore[BLE001]\ntry:\n    pass\nexcept Exception:\n    pass\n",
        "five.py": "# ruff: disable[BLE001]\ntry:\n    pass\nexcept Exception:\n    pass\n# ruff: enable[BLE001]\n",
        "six.py": "# flake8: noqa\ntry:\n    pass\nexcept Exception:\n    pass\n",
        "seven.py": "# flake8: noqa: BLE001\ntry:\n    pass\nexcept Exception:\n    pass\n",
        "nested/pyproject.toml": "[tool.ruff]\n[tool.ruff.lint]\nselect = []\n",
        "nested/eight.py": "try:\n    pass\nexcept Exception:\n    pass\n",
    }
    write_files(tmp_path, files)
    engine = Ruff(config_file="ruff.toml", executable=executable)
    result = SubprocessToolRunner().run(engine.command(sorted(path for path in files if path.endswith(".py"))), tmp_path)

    violations = engine.violations(result, tmp_path, LocalFileSystem())
    blind_except = [violation for violation in violations if violation.rule == "ruff-BLE001"]
    assert len(blind_except) == 7  # nested config is authoritative and disables BLE001 for nested files
    assert {violation.subject for violation in blind_except} == {
        "one.py", "two.py", "three.py", "four.py", "five.py", "six.py", "seven.py"
    }
